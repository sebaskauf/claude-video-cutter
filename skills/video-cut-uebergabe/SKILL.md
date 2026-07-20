---
name: video-cut-uebergabe
description: Phase D des Longform-Video-Cutters — Proxy-Render, End-to-End-Verifikation, Review-Liste, Cut-Cockpit starten und an den User übergeben. Auch für Final-Render nach dem Review und B-Roll-Integration. Nutzen als letzter Schritt des video-cutter-Agents oder bei "starte das Cockpit / render final".
---

# Phase D: Render, Beweis, Cockpit, Übergabe

Regel: NIE "fertig" melden ohne Beweis (A/V-Messung + Hör-Stichproben).
Dein User bekommt IMMER: Proxy + Review-Liste + laufendes Cockpit.

Alle Kommandos aus `{{CUTTER_DIR}}`.

## D1: Proxy-Render (entkoppelt — Renderdauer ≈ 0,2-0,4x Videolänge)
```bash
nohup caffeinate -i .venv312/bin/python scripts/cut_v5.py "<VIDEO>" \
  work/<name>/segments_v5_repaired.json work/<name>/proxy.mp4 proxy \
  > work/<name>/render.log 2>&1 & disown
```
Überwachen bis `FERTIG` im Log. Danach PFLICHT-Verifikation:
```bash
ffprobe -v error -show_entries stream=codec_type,duration -of csv=p=0 work/<name>/proxy.mp4
```
- [ ] Video- und Audio-Dauer differieren <100ms (A/V-Sync-Beweis)
- [ ] 3 Hör-Stichproben: je 6s an zufälligen Stellen extrahieren, mit
  faster-whisper (small) transkribieren — Text muss flüssig lesen, keine
  Wortfragmente, kein Fremd-Content:
```bash
for t in <T1> <T2> <T3>; do ffmpeg -y -loglevel error -ss $((t-3)) -t 6 -i work/<name>/proxy.mp4 -vn -ac 1 -ar 16000 /tmp/spot_$t.wav; done
```

## D2: Review-Liste
Gegenleser-Feinschliff-Befunde (aus Phase B) nach `work/<name>/gegenleser.json`
schreiben (Format: {"issues":[{type,from_id,to_id,beschreibung,vorschlag,confidence}]}), dann:
```bash
.venv312/bin/python scripts/generate_review.py work/<name> \
  work/<name>/segments_v5_repaired.json work/<name>/verify2/qa_stage_a.json \
  work/<name>/gegenleser.json work/<name>/REVIEW-LISTE.md
```
Die Liste enthält: Content-Gaps (oben!), echte Graufälle mit Proxy-Timecodes,
Atmer-Warnungen, Feinschliff-Kandidaten, komprimierte Stille >20s.

## D3: Cut-Cockpit starten (die Schnitt-Software)

**D3a — Cockpit-Manifest schreiben (Pflicht, VOR dem Server-Start):**
Schreibe `work/<name>/cockpit.json`, damit das Agentic OS (CUTTER-Tab) das
Cockpit später per Klick ohne Nachfragen neu starten kann (der src-Video-Pfad
ist sonst nirgends persistiert):
```json
{
  "src_video": "<VOLLER PFAD ZUM ORIGINAL-VIDEO>",
  "segments": "segments_v5_repaired.json",
  "qa": "verify2/qa_stage_a.json",
  "port": 8766
}
```

Verwende `qa_stage_a.json` als QA-Argument, wenn Repair 0 Kanten geändert hat
(dann existiert kein verify2/ — Diff prüfen). Das Plugin akzeptiert beide.
```bash
lsof -ti :8766 | xargs kill 2>/dev/null; sleep 1   # NUR EIN Server auf 8766!
nohup .venv312/bin/python scripts/cockpit_server.py work/<name> \
  work/<name>/segments_v5_repaired.json work/<name>/qa_stage_a.json \
  8766 "<VIDEO>" > work/<name>/cockpit.log 2>&1 & disown
until curl -s -o /dev/null http://127.0.0.1:8766/api/state; do sleep 1; done
```
Das 6. Argument (Quell-Video) ist PFLICHT — ohne läuft der Player nicht auf dem
Original (EDL-Playback), der Re-Render-Button und die B-Roll-Composite-Preview
funktionieren nicht.

**⚠️ Port-8766-Regel (aus Fehler 20.07.):** NIEMALS zwei Server auf 8766. Wenn
dein User im Agentic OS ein Projekt startet, während dein Test-Server läuft,
crasht der Plugin-Start ("cockpit.log prüfen"). Nach dem Playwright-Test den
Server ENTWEDER laufen lassen (das Plugin erkennt ihn) ODER killen — nie beides.

**Smoke-Checks (alle drei, sonst ist die Übergabe unvollständig):**
```bash
curl -s http://127.0.0.1:8766/api/state | .venv312/bin/python -c \
 "import sys,json; d=json.load(sys.stdin); print('brollSync', bool(d.get('brollSync')), '| source', bool(d.get('sourceUrl')))"
curl -s -o /dev/null -w "source.mp4 range: %{http_code}\n" -r 0-1023 http://127.0.0.1:8766/media/source.mp4  # → 206
```
Dann via Playwright (Pflicht bei B-Roll-Videos): navigate → warte 4s → prüfe
`.sync-clip`-Anzahl > 0 (B-Roll-Spur da), `canvas`-Pixel != leer (Waveform
sichtbar), Composite-Preview (Player-Zeit in Sync-Fenster setzen → `#canvasBox`
hat Klasse `pip`). Screenshot ansehen. KEINE Übergabe ohne diese drei Beweise.

## D4: Übergabe an deinen User (Format einhalten)
Die Schnitt-Oberfläche ist der **CUTTER-Tab [04] im Agentic OS**, NICHT der
localhost-Link (dein User arbeitet dort und wechselt per "PROJEKT WECHSELN").
Der laufende Server (D3) wird vom Plugin automatisch erkannt und im iframe
gezeigt — dein User muss den CUTTER-Tab nur öffnen/neu laden. Den localhost-Link
nur als Fallback nennen. Proxy + Review-Liste öffnen: `open work/<name>/proxy.mp4`,
`open work/<name>/REVIEW-LISTE.md`. Dann melden:
1. **Zahlen:** Rohlänge → Schnittlänge, Segmente, QA-Bilanz (PASS/Warnungen/Graufälle)
2. **Beweise:** A/V-Differenz in ms, Stichproben-Ergebnis, + bei B-Roll: Sync
   verifiziert (Playwright-Beweis)
3. **Entscheidungen die er treffen muss:** Content-Gaps, größte Graufälle
4. **Sein Workflow:** Agentic OS → CUTTER [04] → Proxy schauen → Graufälle aus
   Liste anhören → im Cockpit nachcutten (W/Q/E, Trim, Gain-Linie; B-Roll-Sync-Spur
   folgt den Hauptclips beim Trimmen automatisch, Overlay pro Clip im Inspector
   schaltbar) → "Neu rendern"
Ehrlich bleiben: Was maschinell verifiziert ist vs. was sein Ohr braucht.

## D5: Final-Render (erst NACH Review + Freigabe durch deinen User)
Overrides aus dem Cockpit werden automatisch berücksichtigt (rerender.py,
Timeline-Modus). Final in Source-Auflösung:
```bash
nohup caffeinate -i .venv312/bin/python scripts/rerender.py work/<name> "<VIDEO>" \
  work/<name>/words_aai.json work/<name>/decisions.json \
  work/<name>/final.mp4 --mode final --nudge-base work/<name>/segments_v5_repaired.json \
  > work/<name>/final_render.log 2>&1 & disown
```
Danach dieselbe Verifikation wie D1.

## B-Roll — zwei Modi
**1. Manuelle SLOTS** (dein User setzt sie im Cockpit, +B-Roll-Button): start/end
in Proxy-Sekunden + Dateipfad im `broll`-Feld. Video-Overlay über die Slots,
Original-Ton bleibt.

**2. SYNC-B-Roll** (parallel aufgenommenes Screen-Recording, siehe Agent-Memory
`broll_parallel_sync`): dein User nennt Sync-Punkt als MM:SS:FF @ Video-fps.
- Config `work/<name>/broll_sync.json` (file, src_offset in s, pip{}). VOR dem
  Cockpit-Start schreiben, damit die Sync-Spur + Composite-Preview erscheinen.
- Sync-Punkt IMMER verifizieren: Duration-Math (offset+broll_dur ≈ Video-Ende)
  + 2 Content-Checkpoints (B-Roll-Frame bei raw_t−offset gegen Transkript).
- `broll_sync_pass.py` spiegelt die Cuts aufs B-Roll (Screen bleibt synchron zur
  Narration) + Facecam-Kreis mit Shadow. Läuft am Ende von `rerender.py` bei
  JEDEM Render mit — Cockpit-Re-Renders behalten es, respektieren `broll_sync_off`.
- Im Cockpit: goldene ⛓-Sync-Spur folgt den Hauptclips beim Trimmen; Player zeigt
  Composite-Preview; Inspector schaltet Overlay pro Clip.

B-Roll-ERSTELLUNG ist NICHT dein Job → `broll-ersteller`-Agent / Skill `video-effekte`.

## Windows-Hinweise (Claude Code läuft dort mit Git Bash)

- Python-Pfad: überall `.venv312/Scripts/python` statt `.venv312/bin/python`
- `caffeinate` gibt es nicht — weglassen und stattdessen den Energiesparmodus
  deaktivieren (Einstellungen → Energie), `nohup … & disown` funktioniert in
  Git Bash normal
- Port prüfen/freimachen: statt `lsof -ti :8766` →
  `netstat -ano | findstr :8766`, dann `taskkill //PID <pid> //F`
- ffmpeg installieren: `winget install ffmpeg` · Python 3.12: `winget install Python.Python.3.12`
