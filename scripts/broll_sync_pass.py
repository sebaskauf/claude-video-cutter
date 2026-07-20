#!/usr/bin/env python3
"""B-Roll-SYNC-Pass (bc2+): parallel aufgenommenes Screen-Recording ueber das
GESCHNITTENE Video legen, quellzeit-gekoppelt, mit Facecam-Kreis unten rechts.

Konzept: Das B-Roll lief parallel zum Hauptvideo ab Source-Zeit `src_offset`
(B-Roll t=0 == Hauptvideo t=src_offset). Fuer jedes Keep-Segment, das mit dem
B-Roll-Fenster ueberlappt, wird das identische Stueck aus dem B-Roll
geschnitten (gespiegelte Cuts -> Screen bleibt synchron zur Narration) und
fullscreen ueber die Cut-Timeline gelegt. Obendrauf: Facecam als Kreis
(Crop aus dem geschnittenen Video selbst) + weicher Drop-Shadow.

Config (broll_sync.json):
{
  "file": "/abs/broll.mp4",
  "src_offset": 767.567,
  "pip": {"diameter_frac": 0.221, "margin_right_frac": 0.0146,
          "margin_bottom_frac": 0.037,
          "crop_frac": {"x": 0.3047, "y": 0.0208, "size_of_h": 0.9167},
          "shadow": true}
}

Usage: broll_sync_pass.py <base_cut.mp4> <segments.json> <broll_sync.json>
                          <out.mp4> [--mode proxy|final]
"""
import sys, os, json, subprocess, tempfile

import numpy as np
import cv2


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-4000:])
        raise SystemExit(f"Kommando fehlgeschlagen: {' '.join(cmd[:4])} ...")
    return r


def probe_wh_dur(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-show_entries",
             "format=duration", "-of", "json", path])
    d = json.loads(r.stdout)
    st = d["streams"][0]
    return int(st["width"]), int(st["height"]), float(d["format"]["duration"])


def make_pngs(tmp, D, shadow_grow=0.20, shadow_alpha=0.55):
    """Kreis-Alphamaske (D x D) + weicher Schatten (groesser, verschoben eingesetzt)."""
    ss = 4  # Supersampling gegen Treppen
    big = D * ss
    yy, xx = np.mgrid[0:big, 0:big].astype(np.float32)
    r = np.hypot(xx - (big - 1) / 2.0, yy - (big - 1) / 2.0)
    a = np.clip(((big / 2.0) - r) / ss, 0, 1)  # 1px-AA-Kante (nach Downscale)
    # GRAYSCALE-Maske: alphamerge nimmt den LUMA des zweiten Inputs als Alpha
    # (weisser Kreis auf schwarz). NICHT als RGBA schreiben!
    alpha = cv2.resize((a * 255).astype(np.uint8), (D, D), interpolation=cv2.INTER_AREA)
    mask_p = os.path.join(tmp, "circle_mask.png")
    cv2.imwrite(mask_p, alpha)

    # Schatten: schwarzer Kreis, kraeftig geblurrt, als BGRA
    S = int(D * (1 + shadow_grow))
    S += S % 2
    sh_alpha = np.zeros((S, S), np.float32)
    cv2.circle(sh_alpha, (S // 2, S // 2), int(D * 0.5), 1.0, -1)
    k = int(D * 0.10) | 1
    sh_alpha = cv2.GaussianBlur(sh_alpha, (k, k), 0) * shadow_alpha
    shadow = np.dstack([np.zeros((S, S, 3), np.uint8),
                        (sh_alpha * 255).astype(np.uint8)])
    shadow_p = os.path.join(tmp, "circle_shadow.png")
    cv2.imwrite(shadow_p, shadow)
    return mask_p, shadow_p, S


def main():
    args = sys.argv[1:]
    base_mp4, segs_path, cfg_path, out_mp4 = args[:4]
    mode = "proxy"
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]

    cfg = json.load(open(cfg_path))
    broll = cfg["file"]
    off = float(cfg["src_offset"])
    pip = cfg.get("pip", {})
    if not os.path.exists(broll):
        print(f"[sync] B-Roll fehlt ({broll}) -> Pass uebersprungen", flush=True)
        subprocess.run(["cp", base_mp4, out_mp4], check=True)
        return

    segs = json.load(open(segs_path))["segments"]
    segs = [s for s in segs if s["out"] > s["in"]]
    _, _, br_dur = probe_wh_dur(broll)
    W, H, _ = probe_wh_dur(base_mp4)

    # Cockpit-Override: Clips ohne Sync-Overlay (broll_sync_off = first_ids)
    ovr_path = os.path.join(os.path.dirname(os.path.abspath(cfg_path)),
                            "cockpit_overrides.json")
    sync_off = set()
    if os.path.exists(ovr_path):
        try:
            sync_off = set(json.load(open(ovr_path)).get("broll_sync_off", []))
        except Exception:
            pass
    if sync_off:
        print(f"[sync] {len(sync_off)} Clip(s) vom Overlay ausgenommen "
              f"(broll_sync_off)", flush=True)

    # 1. Timeline-Mapping: Keep-Segmente -> (timeline_a, timeline_b, broll_a, broll_b)
    pieces, cum = [], 0.0
    for s in segs:
        d = s["out"] - s["in"]
        if str(s.get("first_id", "")) in sync_off:
            cum += d
            continue
        ov_in, ov_out = max(s["in"], off), min(s["out"], off + br_dur)
        if ov_out - ov_in > 0.04:
            pieces.append((cum + (ov_in - s["in"]), cum + (ov_out - s["in"]),
                           max(0.0, ov_in - off), min(br_dur, ov_out - off)))
        cum += d
    if not pieces:
        print("[sync] Keine Ueberlappung mit B-Roll-Fenster -> Pass uebersprungen", flush=True)
        subprocess.run(["cp", base_mp4, out_mp4], check=True)
        return

    # 2. Runs: zusammenhaengende Timeline-Stuecke gruppieren
    runs, cur = [], [pieces[0]]
    for p in pieces[1:]:
        if p[0] - cur[-1][1] < 0.05:
            cur.append(p)
        else:
            runs.append(cur); cur = [p]
    runs.append(cur)
    total_ov = sum(p[1] - p[0] for p in pieces)
    print(f"[sync] {len(pieces)} B-Roll-Stuecke in {len(runs)} Run(s), "
          f"{total_ov:.1f}s Overlay, PIP ab Timeline {pieces[0][0]:.2f}s", flush=True)

    # 3. PIP-Geometrie + Masken
    D = int(W * float(pip.get("diameter_frac", 0.221)))
    D += D % 2
    mr = int(W * float(pip.get("margin_right_frac", 0.0146)))
    mb = int(H * float(pip.get("margin_bottom_frac", 0.037)))
    px, py = W - D - mr, H - D - mb
    cf = pip.get("crop_frac", {"x": 0.3047, "y": 0.0208, "size_of_h": 0.9167})
    cw = int(H * float(cf["size_of_h"]))
    cx, cy = int(W * float(cf["x"])), int(H * float(cf["y"]))
    cw -= cw % 2
    cx = max(0, min(cx, W - cw)); cy = max(0, min(cy, H - cw))
    tmp = tempfile.mkdtemp(prefix="sync_")
    mask_p, shadow_p, S = make_pngs(tmp, D)
    sx = px + (D - S) // 2 + int(D * 0.02)   # Schatten minimal nach rechts/unten
    sy = py + (D - S) // 2 + int(D * 0.035)

    # 4. Filtergraph
    enable = "+".join(f"between(t,{r[0][0]:.3f},{r[-1][1]:.3f})" for r in runs)
    n_pieces = len(pieces)
    fc = f"[0:v]split=2[bg0][fcsrc];"
    fc += f"[1:v]split={n_pieces}" + "".join(f"[b{j}]" for j in range(n_pieces)) + ";"
    j = 0
    tracks = []
    for ri, r in enumerate(runs):
        parts = []
        for (tla, tlb, bra, brb) in r:
            fc += (f"[b{j}]trim={bra:.4f}:{brb:.4f},setpts=PTS-STARTPTS,"
                   f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H}[t{j}];")
            parts.append(f"[t{j}]"); j += 1
        if len(parts) > 1:
            fc += "".join(parts) + f"concat=n={len(parts)}:v=1:a=0[trk{ri}];"
        else:
            fc += f"{parts[0]}null[trk{ri}];"
        fc += f"[trk{ri}]setpts=PTS-STARTPTS+{r[0][0]:.3f}/TB[trs{ri}];"
        tracks.append(f"[trs{ri}]")
    prev = "bg0"
    for ri in range(len(runs)):
        fc += (f"[{prev}]{tracks[ri]}overlay=x=0:y=0:eof_action=pass:"
               f"enable='between(t,{runs[ri][0][0]:.3f},{runs[ri][-1][1]:.3f})'[ov{ri}];")
        prev = f"ov{ri}"
    fc += (f"[fcsrc]crop={cw}:{cw}:{cx}:{cy},scale={D}:{D}[fcs];"
           f"[fcs][2:v]alphamerge[fca];")
    fc += f"[{prev}][3:v]overlay=x={sx}:y={sy}:eof_action=pass:enable='{enable}'[shd];"
    fc += f"[shd][fca]overlay=x={px}:y={py}:eof_action=pass:enable='{enable}'[vout]"

    if mode == "proxy":
        venc = ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]
    else:
        venc = ["-c:v", "hevc_videotoolbox", "-q:v", "60", "-tag:v", "hvc1",
                "-pix_fmt", "yuv420p"]
    print(f"[sync] Render Composite ({mode}, {W}x{H}, PIP D={D}px @ {px},{py}) ...", flush=True)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", base_mp4, "-i", broll,
         "-loop", "1", "-i", mask_p, "-loop", "1", "-i", shadow_p,
         "-filter_complex", fc, "-map", "[vout]", "-map", "0:a",
         *venc, "-c:a", "copy", "-movflags", "+faststart", out_mp4])
    print(f"[sync] FERTIG -> {out_mp4}", flush=True)


if __name__ == "__main__":
    main()
