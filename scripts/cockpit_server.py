#!/usr/bin/env python3
"""Cut-Cockpit — lokaler Review-Server fuer den V5-Video-Cutter.

Start:
    .venv312/bin/python scripts/cockpit_server.py <workdir> <segments_json> <qa_json> [port=8766]

Serviert ein self-contained Frontend (scripts/cockpit/index.html), liest die
Cut-Entscheidungen + QA-Report und schreibt Nach-Cut-Overrides atomar zurueck.
Nur Python-Stdlib + numpy/soundfile.
"""
import base64
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time

IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import fcntl
    import pty
    import termios
else:
    # Windows: ConPTY via pywinpty (steht in requirements.txt, nur win32)
    try:
        import winpty  # type: ignore
    except ImportError:  # pragma: no cover
        winpty = None
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "cockpit", "index.html")

# ---------------------------------------------------------------------------
# Globale Konfiguration (aus argv gesetzt in main())
# ---------------------------------------------------------------------------
CFG = {
    "workdir": None,
    "segments_path": None,
    "qa_path": None,
    "overrides_path": None,
    "proxy_path": None,
    "audio_path": None,
    "words_path": None,
    "decisions_path": None,
    "port": 8766,
}

# Lock, damit paralleles Lesen/Schreiben der Overrides sicher ist
_OVR_LOCK = threading.Lock()
# Lock um Render-Job-Check+Start (Doppel-POST-Race, Review-Befund #3)
JOB_LOCK = threading.Lock()

# Audio wird lazy + gecacht geladen (48k mono)
_AUDIO = {"data": None, "sr": None}
_AUDIO_LOCK = threading.Lock()

# Waveform-Peaks (Timeline): einmal berechnen, in-memory + Disk-Cache
_PEAKS = {"data": None, "mtime": None}
_PEAKS_LOCK = threading.Lock()
PEAKS_SR_EFF = 50  # Peak-Paare pro Sekunde


def load_json(path, default=None):
    if not path or not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_audio():
    """Laedt audio48k.wav einmalig als float32 numpy-Array (mono)."""
    with _AUDIO_LOCK:
        if _AUDIO["data"] is None:
            data, sr = sf.read(CFG["audio_path"], dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            _AUDIO["data"] = data
            _AUDIO["sr"] = sr
    return _AUDIO["data"], _AUDIO["sr"]


def read_overrides():
    ovr = load_json(CFG["overrides_path"], None)
    if not isinstance(ovr, dict):
        ovr = {}
    ovr.setdefault("timeline_clips", [])
    ovr.setdefault("nudges", {})
    ovr.setdefault("gains", {})
    ovr.setdefault("broll", [])
    ovr.setdefault("extra_cut_word_ids", [])
    ovr.setdefault("uncut_word_ids", [])
    ovr.setdefault("deleted_segments", [])
    ovr.setdefault("splits", [])
    return ovr


def write_overrides_atomic(ovr):
    """Schreibt Overrides atomar (tmp + os.replace)."""
    path = CFG["overrides_path"]
    tmp = path + ".tmp"
    with _OVR_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ovr, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def seg_edges(seg_idx, segments, ovr):
    """Effektive in/out-Zeiten eines Segments inkl. aktueller Nudges (Sekunden)."""
    seg = segments[seg_idx]
    din = float(ovr["nudges"].get(f"{seg_idx}:in", 0.0) or 0.0)
    dout = float(ovr["nudges"].get(f"{seg_idx}:out", 0.0) or 0.0)
    return seg["in"] + din, seg["out"] + dout


def compute_joint_times(segments, ovr):
    """Proxy-Zeit jeder Naht k = kumulierte Dauer der Segmente 0..k (inkl. Nudges).

    Naht k liegt zwischen Segment k und k+1, also am Ende von Segment k.
    Rueckgabe: Liste von n_segments-1 Proxy-Zeiten (Sekunden).
    """
    cum = 0.0
    joints = []
    n = len(segments)
    for i in range(n):
        ins, outs = seg_edges(i, segments, ovr)
        dur = max(0.0, outs - ins)
        cum += dur
        if i < n - 1:
            joints.append(round(cum, 3))
    return joints


def compute_peaks():
    """Min/Max-Peaks des KOMPLETTEN Quell-Audios (Source-Zeitachse).

    Frueher wurden Peaks nur ueber die Keep-Segmente berechnet — beim Trimmen
    ueber die alte Segment-Grenze hinaus gab es fuer das zurueckgeholte
    Material keine Daten und das Frontend konnte die Waveform nur strecken.
    Source-basiert zeichnet jeder Clip einfach seinen echten [inS, outS]-
    Ausschnitt: Trim rein/raus zeigt immer das reale Audio (CapCut-Verhalten).

    Liest audio48k.wav blockweise (nie komplett in den RAM, OOM-Lesson) und
    downsampelt auf PEAKS_SR_EFF Peak-Paare/Sekunde. peaks[i] entspricht der
    Source-Zeit i/PEAKS_SR_EFF.

    Rueckgabe: {"sr_eff", "src": True, "peaks": [[min,max],…]}
    Cache: <workdir>/peaks_src_cache.json, invalidiert wenn Audio-mtime neuer
    (segment-unabhaengig — ueberlebt alle Schnitt-Aenderungen).
    """
    audio_path = CFG["audio_path"]
    cache_path = os.path.join(CFG["workdir"], "peaks_src_cache.json")
    audio_mtime = os.path.getmtime(audio_path) if os.path.exists(audio_path) else 0

    with _PEAKS_LOCK:
        # 1. In-Memory-Cache
        if _PEAKS["data"] is not None and _PEAKS["mtime"] == audio_mtime:
            return _PEAKS["data"]
        # 2. Disk-Cache (Format-Guard: nur Source-Format akzeptieren)
        if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= audio_mtime:
            try:
                data = load_json(cache_path)
                if isinstance(data, dict) and data.get("src") is True and "peaks" in data:
                    _PEAKS["data"] = data
                    _PEAKS["mtime"] = audio_mtime
                    return data
            except Exception:  # noqa: BLE001
                pass
        # 3. Frisch berechnen (blockweise, Raster exakt sr/PEAKS_SR_EFF Samples)
        peaks = []
        with sf.SoundFile(audio_path) as f:
            sr = f.samplerate
            spp = max(1, int(round(sr / PEAKS_SR_EFF)))  # Samples pro Peak (48k/50 = 960)
            block_peaks = 3000                            # ~60s Audio pro Block
            while True:
                block = f.read(spp * block_peaks, dtype="float32", always_2d=False)
                if len(block) == 0:
                    break
                if block.ndim > 1:
                    block = block.mean(axis=1)
                n = max(1, len(block) // spp)
                idx = np.arange(n) * spp
                mins = np.minimum.reduceat(block, idx)
                maxs = np.maximum.reduceat(block, idx)
                for mn, mx in zip(mins, maxs):
                    peaks.append([round(float(mn), 4), round(float(mx), 4)])
        data = {"sr_eff": PEAKS_SR_EFF, "src": True, "peaks": peaks}
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as cf:
                json.dump(data, cf)
            os.replace(tmp, cache_path)
        except OSError:
            pass
        _PEAKS["data"] = data
        _PEAKS["mtime"] = seg_mtime
        return data


# ---------------------------------------------------------------------------
# Eingebettetes Claude-Terminal (PTY) — echte 1:1-TUI in der rechten Spalte.
# Ein Singleton-Terminal pro Server; SSE streamt Output (base64-Chunks),
# POST /api/term/input schreibt Keystrokes. Ring-Buffer fuer Reconnect-Replay.
# ---------------------------------------------------------------------------
TERM = {
    "fd": None,
    "pid": None,
    "wproc": None,  # Windows: winpty.PtyProcess
    "buf": b"",
    "subs": [],
    "lock": threading.Lock(),
    "alive": False,
}
TERM_BUF_MAX = 400_000
TERM_AGENT = "video-cutter"


def _term_fail(msg):
    """Fehlermeldung als Terminal-Output anzeigen statt still zu sterben."""
    with TERM["lock"]:
        TERM["buf"] = ("\r\n" + msg + "\r\n").encode("utf-8")
        TERM["alive"] = False


def term_start(cols=120, rows=32):
    if IS_WINDOWS:
        return _term_start_windows(cols, rows)
    with TERM["lock"]:
        if TERM["alive"]:
            return
        pid, fd = pty.fork()
        if pid == 0:
            # Kind: claude im Login-Shell-Kontext starten (PATH/nvm etc.)
            os.chdir(os.path.dirname(HERE))
            os.environ["TERM"] = "xterm-256color"
            os.execvp("/bin/zsh", ["/bin/zsh", "-lc", "claude --agent %s" % TERM_AGENT])
        TERM["fd"] = fd
        TERM["pid"] = pid
        TERM["buf"] = b""
        TERM["alive"] = True
    term_resize(cols, rows)
    threading.Thread(target=_term_reader, daemon=True).start()


def _term_start_windows(cols=120, rows=32):
    with TERM["lock"]:
        if TERM["alive"]:
            return
        if winpty is None:
            return _term_fail("pywinpty fehlt - bitte installieren: .venv312/Scripts/pip install pywinpty")
        exe = shutil.which("claude")
        if exe is None:
            return _term_fail("claude nicht im PATH gefunden - ist Claude Code installiert?")
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        try:
            proc = winpty.PtyProcess.spawn(
                [exe, "--agent", TERM_AGENT],
                cwd=os.path.dirname(HERE),
                env=env,
                dimensions=(int(rows), int(cols)),
            )
        except Exception as e:  # noqa: BLE001
            return _term_fail("Claude-Start fehlgeschlagen: %s" % e)
        TERM["wproc"] = proc
        TERM["buf"] = b""
        TERM["alive"] = True
    threading.Thread(target=_term_reader_windows, daemon=True).start()


def _term_reader_windows():
    proc = TERM["wproc"]
    while True:
        try:
            chunk = proc.read(65536)  # str (ConPTY liefert Text)
            data = chunk.encode("utf-8", "replace") if chunk else b""
        except (EOFError, ConnectionAbortedError, OSError):
            data = b""
        if not data:
            with TERM["lock"]:
                TERM["alive"] = False
                subs = list(TERM["subs"])
            for q in subs:
                q.put(None)
            return
        with TERM["lock"]:
            TERM["buf"] = (TERM["buf"] + data)[-TERM_BUF_MAX:]
            subs = list(TERM["subs"])
        for q in subs:
            q.put(data)


def _term_reader():
    fd = TERM["fd"]
    while True:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            with TERM["lock"]:
                TERM["alive"] = False
                subs = list(TERM["subs"])
            for q in subs:
                q.put(None)
            try:
                os.waitpid(TERM["pid"], os.WNOHANG)
            except (OSError, TypeError):
                pass
            return
        with TERM["lock"]:
            TERM["buf"] = (TERM["buf"] + data)[-TERM_BUF_MAX:]
            subs = list(TERM["subs"])
        for q in subs:
            q.put(data)


def term_resize(cols, rows):
    if IS_WINDOWS:
        proc = TERM["wproc"]
        if proc is not None and TERM["alive"]:
            try:
                proc.setwinsize(int(rows), int(cols))
            except Exception:  # noqa: BLE001
                pass
        return
    if TERM["fd"] is None:
        return
    try:
        fcntl.ioctl(TERM["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except OSError:
        pass


def term_write(data):
    if IS_WINDOWS:
        proc = TERM["wproc"]
        if proc is None or not TERM["alive"]:
            return False
        try:
            proc.write(data.decode("utf-8", "replace"))
            return True
        except Exception:  # noqa: BLE001
            return False
    if TERM["fd"] is None or not TERM["alive"]:
        return False
    try:
        os.write(TERM["fd"], data)
        return True
    except OSError:
        return False


def term_kill():
    with TERM["lock"]:
        pid = TERM["pid"]
        wproc = TERM["wproc"]
        TERM["alive"] = False
    if IS_WINDOWS:
        if wproc is not None:
            try:
                wproc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
        return
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- Logging leiser machen ---------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[cockpit] %s - %s\n" % (self.address_string(), fmt % args))

    # --- kleine Helfer -----------------------------------------------------
    def _send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, status=HTTPStatus.OK, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, content_type="text/plain; charset=utf-8", status=HTTPStatus.OK):
        self._send_bytes(text.encode("utf-8"), content_type, status)

    # --- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if route == "/" or route == "/index.html":
                return self.serve_index()
            if route == "/favicon.ico":
                return self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            if route == "/api/state":
                return self.serve_state()
            if route == "/api/render_status":
                return self.render_status()
            if route == "/api/joint_audio":
                return self.serve_joint_audio(qs)
            if route == "/api/peaks":
                return self.serve_peaks()
            if route == "/media/proxy.mp4":
                return self.serve_media(CFG.get("proxy_path"))
            if route == "/media/source.mp4":
                return self.serve_media(CFG.get("src_video"))
            if route == "/api/term/stream":
                return self.serve_term_stream()
            if route.startswith("/vendor/"):
                return self.serve_vendor(route)
            self._send_json({"error": "not found", "path": route}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    # --- POST --------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/api/overrides":
                return self.save_overrides()
            if route == "/api/rerender":
                return self.rerender()
            if route == "/api/term/input":
                return self.term_input()
            if route == "/api/term/resize":
                return self.term_resize_route()
            if route == "/api/term/restart":
                return self.term_restart()
            self._send_json({"error": "not found", "path": route}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    # --- Claude-Terminal + Vendor ------------------------------------------
    VENDOR_FILES = {
        "xterm.js": "application/javascript; charset=utf-8",
        "xterm.css": "text/css; charset=utf-8",
        "addon-fit.js": "application/javascript; charset=utf-8",
    }

    def serve_vendor(self, route):
        name = route[len("/vendor/"):]
        ctype = self.VENDOR_FILES.get(name)
        if ctype is None:
            return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with open(os.path.join(HERE, "cockpit", "vendor", name), "rb") as f:
            self._send_bytes(f.read(), ctype, extra_headers={"Cache-Control": "no-store"})

    def _term_origin_ok(self):
        # CSRF-Schutz: fremde Webseiten im Browser duerfen den Terminal-Endpoint
        # nicht ansteuern. Same-origin (Cockpit selbst) + Origin-lose Clients ok.
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")

    def _read_post_json(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        if ln <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(ln) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def serve_term_stream(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        term_start()
        q = queue.Queue()
        with TERM["lock"]:
            replay = TERM["buf"]
            TERM["subs"].append(q)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(chunk):
                payload = base64.b64encode(chunk).decode("ascii")
                self.wfile.write(("data: %s\n\n" % payload).encode("ascii"))
                self.wfile.flush()

            if replay:
                emit(replay)
            while True:
                try:
                    data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if data is None:
                    self.wfile.write(b"event: exit\ndata: 1\n\n")
                    self.wfile.flush()
                    return
                emit(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.close_connection = True
            with TERM["lock"]:
                if q in TERM["subs"]:
                    TERM["subs"].remove(q)

    def term_input(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        body = self._read_post_json()
        try:
            data = base64.b64decode(body.get("data", ""))
        except (ValueError, TypeError):
            data = b""
        self._send_json({"ok": term_write(data)})

    def term_resize_route(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        body = self._read_post_json()
        term_resize(body.get("cols", 120), body.get("rows", 32))
        self._send_json({"ok": True})

    def term_restart(self):
        if not self._term_origin_ok():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        term_kill()
        time.sleep(0.4)
        term_start()
        self._send_json({"ok": True})

    # --- Routen-Implementierung -------------------------------------------
    def serve_index(self):
        with open(INDEX_HTML, "rb") as f:
            data = f.read()
        self._send_bytes(data, "text/html; charset=utf-8")

    def serve_state(self):
        words = load_json(CFG["words_path"], [])
        decisions = load_json(CFG["decisions_path"], {})
        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        qa = load_json(CFG["qa_path"], [])
        ovr = read_overrides()
        joint_times = compute_joint_times(segments, ovr)
        state = {
            "words": words,
            "decisions": decisions,
            "segments": segments,
            "segMeta": {
                "keep_s": seg_doc.get("keep_s") if isinstance(seg_doc, dict) else None,
                "total_s": seg_doc.get("total_s") if isinstance(seg_doc, dict) else None,
                "n_segments": len(segments),
            },
            "qa": qa,
            "overrides": ovr,
            "jointTimes": joint_times,
            "proxyUrl": "/media/proxy.mp4",
            "sourceUrl": ("/media/source.mp4"
                          if CFG.get("src_video") and os.path.exists(CFG["src_video"])
                          else None),
            "paths": {
                "workdir": CFG["workdir"],
                "overrides": CFG["overrides_path"],
                "segments": CFG["segments_path"],
                "qa": CFG["qa_path"],
            },
        }
        self._send_json(state)

    def serve_joint_audio(self, qs):
        """WAV mit +-2s um Naht k: letzte 2s von Segment k + erste 2s von Segment k+1.

        Beruecksichtigt aktuelle Nudges. Gestitcht aus audio48k.wav per numpy.
        """
        try:
            k = int(qs.get("k", ["-1"])[0])
        except ValueError:
            return self._send_json({"error": "bad k"}, HTTPStatus.BAD_REQUEST)
        window = 2.0
        try:
            window = float(qs.get("window", ["2.0"])[0])
        except ValueError:
            pass

        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        if k < 0 or k >= len(segments) - 1:
            return self._send_json({"error": "k out of range"}, HTTPStatus.BAD_REQUEST)

        ovr = read_overrides()
        data, sr = get_audio()
        total = len(data)

        # Segment k: out-Kante (inkl. Nudge). Fenster = letzte `window` s davor.
        _, out_k = seg_edges(k, segments, ovr)
        # Segment k+1: in-Kante (inkl. Nudge). Fenster = erste `window` s danach.
        in_k1, _ = seg_edges(k + 1, segments, ovr)

        pre_start = max(0, int(round((out_k - window) * sr)))
        pre_end = min(total, int(round(out_k * sr)))
        post_start = max(0, int(round(in_k1 * sr)))
        post_end = min(total, int(round((in_k1 + window) * sr)))

        pre = data[pre_start:pre_end] if pre_end > pre_start else np.zeros(0, dtype="float32")
        post = data[post_start:post_end] if post_end > post_start else np.zeros(0, dtype="float32")

        # Kurze Stille als hoerbare Naht-Markierung zwischen den beiden Haelften
        gap = np.zeros(int(0.05 * sr), dtype="float32")
        stitched = np.concatenate([pre, gap, post]).astype("float32")

        import io
        buf = io.BytesIO()
        sf.write(buf, stitched, sr, format="WAV", subtype="PCM_16")
        wav_bytes = buf.getvalue()
        self._send_bytes(
            wav_bytes,
            "audio/wav",
            extra_headers={"Cache-Control": "no-store"},
        )

    def serve_peaks(self):
        """Waveform-Peaks der Proxy-Timeline (gecacht)."""
        data = compute_peaks()
        self._send_json(data)

    def serve_media(self, path):
        """Video-Datei mit HTTP-Range-Support (fuer Browser-Seeking noetig)."""
        if not path or not os.path.exists(path):
            return self._send_json({"error": "media not found"}, HTTPStatus.NOT_FOUND)
        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        ctype = "video/mp4"

        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not m:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % file_size)
                self.end_headers()
                return
            start_s, end_s = m.group(1), m.group(2)
            if start_s == "":
                # suffix range: letzte N bytes
                length = int(end_s)
                start = max(0, file_size - length)
                end = file_size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % file_size)
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self._stream_file(path, start, length)
        else:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.end_headers()
            self._stream_file(path, 0, file_size)

    def _stream_file(self, path, start, length):
        chunk = 256 * 1024
        remaining = length
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def save_overrides(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            incoming = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            return self._send_json({"error": "bad json: %s" % e}, HTTPStatus.BAD_REQUEST)
        # Normalisieren + Defaults
        ovr = {
            "timeline_clips": incoming.get("timeline_clips", []) or [],
            "nudges": incoming.get("nudges", {}) or {},
            "gains": incoming.get("gains", {}) or {},
            "broll": incoming.get("broll", []) or [],
            "extra_cut_word_ids": incoming.get("extra_cut_word_ids", []) or [],
            "uncut_word_ids": incoming.get("uncut_word_ids", []) or [],
            "deleted_segments": incoming.get("deleted_segments", []) or [],
            "splits": incoming.get("splits", []) or [],
        }
        write_overrides_atomic(ovr)
        seg_doc = load_json(CFG["segments_path"], {})
        segments = seg_doc.get("segments", []) if isinstance(seg_doc, dict) else []
        joint_times = compute_joint_times(segments, ovr)
        self._send_json({"ok": True, "saved": CFG["overrides_path"], "jointTimes": joint_times})

    def rerender(self):
        """Echter Render-Hook: startet rerender.py als Background-Job.

        Kette in rerender.py: effektive Decisions (extra/uncut) -> solver_v5
        -> Nudges/Gains via first_id-Mapping -> Proxy-Render -> B-Roll-Pass.
        Status via GET /api/render_status.
        """
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if not CFG.get("src_video") or not os.path.exists(CFG["src_video"]):
            return self._send_json(
                {"error": "src_video fehlt — Server mit 6. Argument <quellvideo> starten"},
                HTTPStatus.BAD_REQUEST)
        with JOB_LOCK:
            job = CFG.get("job")
            if job and job["proc"].poll() is None:
                return self._send_json({"error": "Render läuft bereits"}, HTTPStatus.CONFLICT)
            if job and job.get("logf"):
                try:
                    job["logf"].close()
                except OSError:
                    pass
            py = sys.executable
            log_path = os.path.join(CFG["workdir"], "rerender.log")
            logf = open(log_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                [py, os.path.join(HERE, "rerender.py"), CFG["workdir"], CFG["src_video"],
                 CFG["words_path"], CFG["decisions_path"], CFG["proxy_path"],
                 "--mode", "proxy", "--nudge-base", CFG["segments_path"]],
                stdout=logf, stderr=subprocess.STDOUT)
            CFG["job"] = {"proc": proc, "log": log_path, "logf": logf}
        sys.stderr.write("[cockpit] RERENDER gestartet (pid %d)\n" % proc.pid)
        self._send_json({"status": "started", "pid": proc.pid})

    def render_status(self):
        with JOB_LOCK:
            job = CFG.get("job")
        if not job:
            return self._send_json({"status": "idle"})
        code = job["proc"].poll()
        tail = ""
        try:
            with open(job["log"], encoding="utf-8") as f:
                tail = "".join(f.readlines()[-6:])
        except OSError:
            pass
        if code is None:
            return self._send_json({"status": "running", "log": tail})
        if job.get("logf"):
            try:
                job["logf"].close()
            except OSError:
                pass
            job["logf"] = None
        self._send_json({"status": "done" if code == 0 else "failed",
                         "exit": code, "log": tail})


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    workdir = os.path.abspath(sys.argv[1])
    segments_path = os.path.abspath(sys.argv[2])
    qa_path = os.path.abspath(sys.argv[3])
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 8766
    src_video = os.path.abspath(sys.argv[5]) if len(sys.argv) > 5 else None

    def first_existing(*cands):
        for c in cands:
            if os.path.exists(c):
                return c
        return cands[0]

    CFG["workdir"] = workdir
    CFG["segments_path"] = segments_path
    CFG["qa_path"] = qa_path
    CFG["overrides_path"] = os.path.join(workdir, "cockpit_overrides.json")
    CFG["proxy_path"] = first_existing(
        os.path.join(workdir, "bench", "v5_proxy.mp4"),
        os.path.join(workdir, "proxy.mp4"))
    CFG["audio_path"] = os.path.join(workdir, "audio48k.wav")
    CFG["words_path"] = first_existing(
        os.path.join(workdir, "aai", "words.json"),
        os.path.join(workdir, "words_aai.json"))
    CFG["decisions_path"] = first_existing(
        os.path.join(workdir, "aai", "decisions.json"),
        os.path.join(workdir, "decisions.json"))
    CFG["src_video"] = src_video
    CFG["port"] = port

    for label, p in [
        ("workdir", workdir),
        ("segments", segments_path),
        ("qa", qa_path),
        ("proxy", CFG["proxy_path"]),
        ("audio48k", CFG["audio_path"]),
        ("words", CFG["words_path"]),
    ]:
        exists = "OK" if os.path.exists(p) else "FEHLT"
        print("[cockpit] %-10s %s  (%s)" % (label, p, exists))

    # SIGTERM (z.B. "PROJEKT WECHSELN" im Agentic OS killt per lsof) muss die
    # Claude-PTY-Session mitbeenden — sonst bleibt claude als Orphan zurueck.
    def _on_sigterm(signum, frame):  # noqa: ARG001
        term_kill()
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    print("[cockpit] laeuft auf %s  (Strg+C zum Beenden)" % url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        job = CFG.get("job")
        if job and job["proc"].poll() is None:
            print("\n[cockpit] beende laufenden Render-Job (pid %d) …" % job["proc"].pid)
            job["proc"].terminate()
            try:
                job["proc"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                job["proc"].kill()
        if job and job.get("logf"):
            try:
                job["logf"].close()
            except OSError:
                pass
        term_kill()
        print("\n[cockpit] beendet.")
        server.shutdown()


if __name__ == "__main__":
    main()
