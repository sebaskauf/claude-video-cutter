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

```bash
nohup .venv312/bin/python scripts/cockpit_server.py work/<name> \
  work/<name>/segments_v5_repaired.json work/<name>/verify2/qa_stage_a.json \
  8766 "<VIDEO>" > work/<name>/cockpit.log 2>&1 & disown
until curl -s -o /dev/null http://127.0.0.1:8766/api/state; do sleep 1; done
```
Port 8766 belegt? → alten Prozess finden (`lsof -ti :8766`) und killen, oder
Port 8767 nehmen (dann im Link nennen). Das 6. Argument (Quell-Video) ist
PFLICHT — ohne läuft der Player nicht auf dem Original (EDL-Playback) und der
Re-Render-Button funktioniert nicht.
Smoke-Check: `curl -s http://127.0.0.1:8766/api/state | head -c 100` und
Range-Check `curl -s -o /dev/null -w "%{http_code}" -r 0-1023 http://127.0.0.1:8766/media/source.mp4` → 206.

## D4: Übergabe an deinen User (Format einhalten)
Öffnen: `open work/<name>/proxy.mp4`, `open work/<name>/REVIEW-LISTE.md`,
`open http://127.0.0.1:8766/`. Dann melden:
1. **Zahlen:** Rohlänge → Schnittlänge, Segmente, QA-Bilanz (PASS/Warnungen/Graufälle)
2. **Beweise:** A/V-Differenz in ms, Stichproben-Ergebnis
3. **Entscheidungen die er treffen muss:** Content-Gaps, größte Graufälle
4. **Sein Workflow:** Proxy schauen → Graufälle aus Liste anhören → im Cockpit
   nachcutten (W/Q/E, Trim, Gain-Linie, B-Roll-Slots) → "Neu rendern"
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

## B-Roll (separates Thema, nur Schnittstelle)
- B-Roll-SLOTS (wo, wie lang) setzt dein User im Cockpit oder du auf Zuruf
  (broll-Feld in cockpit_overrides.json: start/end in Proxy-Sekunden + Dateipfad)
- B-Roll-ERSTELLUNG ist NICHT dein Job → dafür existiert der
  `broll-ersteller`-Agent bzw. der Skill `video-effekte` (Grafiken).
- Beim Render legt rerender.py B-Roll als Video-Overlay über die Slots
  (Original-Ton bleibt).

## Windows-Hinweise (Claude Code läuft dort mit Git Bash)

- Python-Pfad: überall `.venv312/Scripts/python` statt `.venv312/bin/python`
- `caffeinate` gibt es nicht — weglassen und stattdessen den Energiesparmodus
  deaktivieren (Einstellungen → Energie), `nohup … & disown` funktioniert in
  Git Bash normal
- Port prüfen/freimachen: statt `lsof -ti :8766` →
  `netstat -ano | findstr :8766`, dann `taskkill //PID <pid> //F`
- ffmpeg installieren: `winget install ffmpeg` · Python 3.12: `winget install Python.Python.3.12`
