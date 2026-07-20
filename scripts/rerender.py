#!/usr/bin/env python3
"""V5/V6 Cockpit-Render-Hook: Overrides -> Solver -> Nudges -> Render.

Kette:
1. decisions_effective = decisions.cut_word_ids + extra_cut_word_ids - uncut_word_ids
2. solver_v5.py neu ausfuehren -> segments_v5.json (frische, gemessene Kanten)
3. Nudges/Gains anwenden: Overrides speichern segIdx relativ zur UI-Basis
   (--nudge-base), Mapping erfolgt ueber first_id/last_id (stabil ueber Re-Solves)
4. Render (proxy 1080p / final source-res) mit per-Segment volume-Filter
5. Optional B-Roll-Overlay-Pass (Zeiten = Proxy-Timeline, Video-only, Ton bleibt)

Usage: rerender.py <workdir> <src_video> <words_json> <decisions_json> <out_mp4>
                   [--mode proxy|final] [--nudge-base segments.json]
"""
import sys, os, json, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = 25
FADE_S = 0.012

def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or "")[-3000:])
        raise SystemExit(f"Kommando fehlgeschlagen: {' '.join(cmd[:3])}…")
    return r

def main():
    args = sys.argv[1:]
    workdir, src, words_path, dec_path, out_mp4 = args[:5]
    mode = "proxy"; nudge_base = None
    if "--mode" in args: mode = args[args.index("--mode") + 1]
    if "--nudge-base" in args: nudge_base = args[args.index("--nudge-base") + 1]

    ovr_path = os.path.join(workdir, "cockpit_overrides.json")
    ovr = json.load(open(ovr_path)) if os.path.exists(ovr_path) else {}
    nudges = ovr.get("nudges", {}); gains = ovr.get("gains", {})
    broll = ovr.get("broll", []); extra = set(ovr.get("extra_cut_word_ids", []))
    uncut = set(ovr.get("uncut_word_ids", []))
    deleted = set(ovr.get("deleted_segments", []))   # first_ids (inkl. Split-Haelften "wN#sK")
    splits = sorted(ovr.get("splits", []))           # Source-Sekunden

    # 0. TIMELINE-MODUS (Cockpit v2.3): Wenn das Cockpit eine fertige Clip-Liste
    #    gespeichert hat, ist DIE die Quelle der Wahrheit — kein Solver, kein
    #    Split/Delete/Nudge-Mapping. {id, in, out, gain} pro Clip, in Reihenfolge.
    timeline_clips = ovr.get("timeline_clips") or []
    if timeline_clips:
        segs = []
        for tc in timeline_clips:
            if tc.get("out", 0) - tc.get("in", 0) < 0.04:
                continue
            segs.append({"in": round(float(tc["in"]), 4), "out": round(float(tc["out"]), 4),
                         "first_id": tc.get("id", f"tl{len(segs)}"),
                         "gain_db": float(tc.get("gain", 0) or 0), "flags": ["timeline"]})
        print(f"[rerender] TIMELINE-Modus: {len(segs)} Clips direkt aus dem Cockpit", flush=True)
        json.dump({"segments": segs, "keep_s": round(sum(s['out']-s['in'] for s in segs), 1)},
                  open(os.path.join(workdir, "segments_effective.json"), "w"),
                  ensure_ascii=False, indent=1)
        render_segments(segs, src, out_mp4, mode, broll, workdir)
        return

    # 1. effektive Entscheidungen
    dec = json.load(open(dec_path))
    cut_ids = (set(dec.get("cut_word_ids", [])) | extra) - uncut
    dec_eff = {**dec, "cut_word_ids": sorted(cut_ids, key=lambda s: int(s[1:]))}
    eff_path = os.path.join(workdir, "decisions_effective.json")
    json.dump(dec_eff, open(eff_path, "w"), ensure_ascii=False, indent=0)
    print(f"[rerender] {len(cut_ids)} Cut-Woerter effektiv "
          f"(+{len(extra)} extra, -{len(uncut)} restauriert)", flush=True)

    # 2. Solver
    audio48 = os.path.join(workdir, "audio48k.wav")
    run([sys.executable, os.path.join(HERE, "solver_v5.py"),
         audio48, words_path, eff_path, workdir])
    seg_doc = json.load(open(os.path.join(workdir, "segments_v5.json")))
    segs = seg_doc["segments"]

    # 2b. Splits anwenden (Timeline-UI): Source-Zeit t im Segment → zwei Haelften.
    #     ID-Konvention (deterministisch, mit Cockpit-Frontend abgestimmt):
    #     linke Haelfte behaelt first_id, rechte bekommt "<first_id>#sK" (K = 1..n
    #     pro Ursprungs-Segment, Splits aufsteigend sortiert).
    if splits:
        applied_s = 0
        for t in splits:
            for k, s in enumerate(segs):
                if s["in"] + 0.05 < t < s["out"] - 0.05:
                    n_prev = sum(1 for x in segs if str(x["first_id"]).split("#")[0]
                                 == str(s["first_id"]).split("#")[0])
                    base_id = str(s["first_id"]).split("#")[0]
                    right = {**s, "in": round(t, 4),
                             "first_id": f"{base_id}#s{n_prev}",
                             "flags": list(s.get("flags", [])) + ["split"]}
                    left = {**s, "out": round(t, 4),
                            "flags": list(s.get("flags", [])) + ["split"]}
                    segs[k:k + 1] = [left, right]
                    applied_s += 1
                    break
            else:
                print(f"[rerender] Split bei {t:.2f}s verworfen (liegt in keinem Segment)", flush=True)
        print(f"[rerender] {applied_s} Splits angewendet", flush=True)

    # 2c. Geloeschte Segmente (Timeline-UI, per first_id)
    if deleted:
        before = len(segs)
        segs = [s for s in segs if str(s["first_id"]) not in deleted]
        print(f"[rerender] {before - len(segs)} Segmente geloescht "
              f"({len(deleted)} angefordert)", flush=True)

    # 3. Nudges + Gains von der UI-Basis auf den neuen Solve mappen:
    #    in-Kante gehoert zum ERSTEN Wort (first_id), out-Kante zum LETZTEN
    #    (last_id) — nach Re-Solve koennen Segmente anders geschnitten sein
    by_first = {s["first_id"]: s for s in segs}
    by_last = {s["last_id"]: s for s in segs}
    applied_n, applied_g = 0, 0
    if nudge_base and os.path.exists(nudge_base) and (nudges or gains):
        base_segs = json.load(open(nudge_base))["segments"]
        for key, delta in nudges.items():
            idx_s, edge = key.split(":")
            i = int(idx_s)
            if i >= len(base_segs) or edge not in ("in", "out"):
                print(f"[rerender] Nudge {key} verworfen (ungültig)", flush=True); continue
            anchor = base_segs[i]["first_id"] if edge == "in" else base_segs[i]["last_id"]
            tgt = (by_first if edge == "in" else by_last).get(anchor)
            if tgt is None:
                print(f"[rerender] Nudge {key} verworfen (Segment nach Re-Solve weg)", flush=True)
                continue
            tgt[edge] = round(tgt[edge] + float(delta), 4)
            applied_n += 1
        for idx_s, db in gains.items():
            i = int(idx_s)
            if i >= len(base_segs): continue
            tgt = by_first.get(base_segs[i]["first_id"])
            if tgt is None:
                print(f"[rerender] Gain für Basis-Segment {i} verworfen (Segment weg)", flush=True)
                continue
            tgt["gain_db"] = float(db); applied_g += 1
        # Clamping: Nudges duerfen Segmente weder invertieren noch in Nachbarn schieben
        for k, s in enumerate(segs):
            nxt_in = segs[k + 1]["in"] if k + 1 < len(segs) else float("inf")
            prv_out = segs[k - 1]["out"] if k > 0 else 0.0
            s["in"] = max(s["in"], prv_out + 0.005)
            s["out"] = max(s["in"] + 0.05, min(s["out"], nxt_in - 0.005))
    print(f"[rerender] {applied_n} Nudges, {applied_g} Gains angewendet", flush=True)
    seg_doc["segments"] = segs
    json.dump(seg_doc, open(os.path.join(workdir, "segments_effective.json"), "w"),
              ensure_ascii=False, indent=1)
    render_segments(segs, src, out_mp4, mode, broll, workdir)


def render_segments(segs, src, out_mp4, mode, broll, workdir):
    """Schritt 4+5: Segmente rendern (per-Segment-Gain) + optionaler B-Roll-Pass."""
    if mode == "proxy":
        vf_extra = ",scale=-2:1080"
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    else:
        vf_extra = ""
        venc = (["-c:v", "hevc_videotoolbox", "-q:v", "55", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]
                if sys.platform == "darwin"
                else ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"])

    pairs = [(s["in"], s["out"], s.get("gain_db", 0.0)) for s in segs if s["out"] > s["in"]]
    print(f"[rerender] Render {len(pairs)} Segmente, mode={mode}", flush=True)
    tmp = tempfile.mkdtemp(prefix="rer_"); batches = []
    for bi in range(0, len(pairs), BATCH):
        chunk = pairs[bi:bi + BATCH]; parts = ""; ci = ""
        for k, (s, e, g) in enumerate(chunk):
            fo = max(0.0, (e - s) - FADE_S)
            vol = f",volume={g:.1f}dB" if abs(g) > 0.01 else ""
            parts += (f"[0:v]trim={s:.4f}:{e:.4f},setpts=PTS-STARTPTS{vf_extra}[v{k}];"
                      f"[0:a]atrim={s:.4f}:{e:.4f},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={FADE_S},afade=t=out:st={fo:.4f}:d={FADE_S}{vol}[a{k}];")
            ci += f"[v{k}][a{k}]"
        fc = parts + f"{ci}concat=n={len(chunk)}:v=1:a=1[v][a]"
        bf = os.path.join(tmp, f"b{bi//BATCH:03d}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", fc,
             "-map", "[v]", "-map", "[a]", *venc, "-c:a", "aac", "-b:a", "192k",
             "-ar", "48000", bf])
        batches.append(bf)
        print(f"[rerender]   Batch {bi//BATCH+1}/{(len(pairs)+BATCH-1)//BATCH}", flush=True)
    base = os.path.join(tmp, "base.mp4")
    if len(batches) == 1:
        os.replace(batches[0], base)
    else:
        lst = os.path.join(tmp, "l.txt")
        open(lst, "w").write("".join(f"file '{b}'\n" for b in batches))
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", base])

    # 5. B-Roll-Overlay-Pass (Zeiten beziehen sich auf die NEUE Proxy-Timeline)
    valid_broll = [b for b in broll if b.get("file") and os.path.exists(b["file"])
                   and b.get("end", 0) > b.get("start", 0)]
    if valid_broll:
        print(f"[rerender] B-Roll-Pass: {len(valid_broll)} Slots", flush=True)
        probe = run(["ffprobe", "-v", "error", "-select_streams", "v",
                     "-show_entries", "stream=width,height", "-of", "csv=p=0", base])
        W, H = probe.stdout.strip().split("\n")[0].split(",")[:2]
        inputs = ["-i", base]
        fc, prev = "", "0:v"
        for i, b in enumerate(valid_broll):
            inputs += ["-i", b["file"]]
            fc += (f"[{i+1}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},setpts=PTS-STARTPTS+{b['start']:.3f}/TB[br{i}];"
                   f"[{prev}][br{i}]overlay=enable='between(t,{b['start']:.3f},{b['end']:.3f})'[o{i}];")
            prev = f"o{i}"
        fc = fc.rstrip(";")
        overlaid = os.path.join(tmp, "overlaid.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
             "-map", f"[{prev}]", "-map", "0:a", *venc,
             "-c:a", "copy", overlaid])
        os.replace(overlaid, out_mp4)
    else:
        os.replace(base, out_mp4)

    # 6. B-Roll-SYNC-Pass (broll_sync.json im Workdir): parallel aufgenommenes
    #    Screen-Recording quellzeit-gekoppelt + Facecam-Kreis. Wird bei JEDEM
    #    Render angewendet (auch Cockpit-Re-Render), Mapping rechnet immer aus
    #    den aktuellen effektiven Segmenten -> ueberlebt Re-Cuts.
    sync_cfg = os.path.join(workdir, "broll_sync.json")
    segs_eff = os.path.join(workdir, "segments_effective.json")
    if os.path.exists(sync_cfg) and os.path.exists(segs_eff):
        tmp_sync = out_mp4 + ".sync.mp4"
        run([sys.executable, os.path.join(HERE, "broll_sync_pass.py"),
             out_mp4, segs_eff, sync_cfg, tmp_sync, "--mode", mode])
        os.replace(tmp_sync, out_mp4)
        print("[rerender] B-Roll-SYNC-Pass angewendet", flush=True)
    print(f"[rerender] FERTIG -> {out_mp4}", flush=True)

if __name__ == "__main__":
    main()
