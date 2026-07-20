#!/usr/bin/env python3
"""V5 Phase 5: Render aus segments_v5.json (Solver-Ausgabe, gemessene Kanten).

Unterschiede zu V4 cut.py:
- Kanten kommen fertig vermessen aus solver_v5 (KEINE eigene Zeit-Logik hier)
- Source-fps bleibt erhalten (V4 erzwang -r 30 auf 60fps-Material)
- --mode proxy  : 1080p, libx264 veryfast (schnelle Review-Runde)
- --mode final  : Source-Aufloesung, hevc_videotoolbox (Hardware, 4K60-tauglich)

Usage: cut_v5.py <src_video> <segments_v5.json> <out.mp4> [proxy|final]
"""
import sys, os, json, subprocess, tempfile

BATCH = 25
FADE_S = 0.012

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:]); raise SystemExit("ffmpeg failed")
    return r

def main():
    src, segs_path, out_mp4 = sys.argv[1], sys.argv[2], sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) > 4 else "proxy"
    S = json.load(open(segs_path))["segments"]
    segs = [(s["in"], s["out"]) for s in S]

    if mode == "proxy":
        vf_extra = ",scale=-2:1080"
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    else:
        vf_extra = ""
        venc = ["-c:v", "hevc_videotoolbox", "-q:v", "55", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"]

    keep = sum(e - s for s, e in segs)
    print(f"[cutv5] {len(segs)} Segmente, {keep:.0f}s, mode={mode}", flush=True)

    tmp = tempfile.mkdtemp(prefix="cutv5_"); batches = []
    for bi in range(0, len(segs), BATCH):
        chunk = segs[bi:bi + BATCH]; parts = ""; ci = ""
        for k, (s, e) in enumerate(chunk):
            fo = max(0.0, (e - s) - FADE_S)
            parts += (f"[0:v]trim={s:.4f}:{e:.4f},setpts=PTS-STARTPTS{vf_extra}[v{k}];"
                      f"[0:a]atrim={s:.4f}:{e:.4f},asetpts=PTS-STARTPTS,"
                      f"afade=t=in:st=0:d={FADE_S},afade=t=out:st={fo:.4f}:d={FADE_S}[a{k}];")
            ci += f"[v{k}][a{k}]"
        fc = parts + f"{ci}concat=n={len(chunk)}:v=1:a=1[v][a]"
        bf = os.path.join(tmp, f"b{bi//BATCH:03d}.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", fc,
             "-map", "[v]", "-map", "[a]", *venc,
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", bf])
        batches.append(bf)
        print(f"[cutv5]   Batch {bi//BATCH+1}/{(len(segs)+BATCH-1)//BATCH}", flush=True)

    if len(batches) == 1:
        os.replace(batches[0], out_mp4)
    else:
        lst = os.path.join(tmp, "l.txt")
        open(lst, "w").write("".join(f"file '{b}'\n" for b in batches))
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-c", "copy", out_mp4])
    print(f"[cutv5] FERTIG -> {out_mp4}", flush=True)

if __name__ == "__main__":
    main()
