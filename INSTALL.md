# INSTALL.md — Anleitung für Claude Code

> **An Claude:** Diese Datei ist an dich adressiert. Dein User will den
> Video-Cutter bei sich einrichten. Führe ihn Schritt für Schritt durch —
> erkläre jeden Schritt kurz, BEVOR du ihn ausführst. Brich sauber ab und
> erkläre das Problem, wenn eine Voraussetzung fehlt und dein User sie
> nicht installieren will.

## Schritt 1: Voraussetzungen prüfen

```bash
uname -s                 # Darwin = macOS (alles andere: warnen, ungetestet!)
ffmpeg -version | head -1
ffprobe -version | head -1
python3.12 --version || echo "PYTHON 3.12 FEHLT"
```

- Kein macOS → deinem User ehrlich sagen: Der Cutter ist auf macOS gebaut
  und auf anderen Systemen ungetestet. Nur fortfahren, wenn er das
  ausdrücklich will.
- ffmpeg fehlt → `brew install ffmpeg` (nachfragen, bevor du installierst).
- Python 3.12 fehlt → `brew install python@3.12` oder via uv (nachfragen).

## Schritt 2: Python-Umgebung bauen

Aus dem Verzeichnis dieses Repos:

```bash
uv venv --python 3.12 .venv312 2>/dev/null || python3.12 -m venv .venv312
VIRTUAL_ENV=$PWD/.venv312 uv pip install -r requirements.txt 2>/dev/null || .venv312/bin/pip install -r requirements.txt
.venv312/bin/python -c "import numpy, soundfile, faster_whisper, requests, rapidfuzz; print('Umgebung OK')"
```

Hinweis für deinen User: Beim allerersten QA-Lauf lädt faster-whisper das
large-v3-Modell (~3 GB) einmalig herunter — das ist normal.

## Schritt 3: Agent + Skills installieren

Der Agent und die Skills enthalten den Platzhalter `{{CUTTER_DIR}}`. Ersetze
ihn beim Kopieren durch den **absoluten Pfad dieses Repos** (das Verzeichnis,
in dem diese INSTALL.md liegt).

```bash
CUTTER_DIR="$(pwd)"   # absoluter Pfad des Klons

# Kollisions-Check — existiert schon ein video-cutter? Dann User fragen!
ls ~/.claude/agents/video-cutter.md 2>/dev/null && echo "ACHTUNG: existiert schon"
ls -d ~/.claude/skills/video-cut-* 2>/dev/null && echo "ACHTUNG: Skills existieren schon"

mkdir -p ~/.claude/agents ~/.claude/skills
sed "s|{{CUTTER_DIR}}|$CUTTER_DIR|g" agent/video-cutter.md > ~/.claude/agents/video-cutter.md
for s in video-cut-transkription video-cut-entscheidung video-cut-pipeline video-cut-uebergabe; do
  mkdir -p ~/.claude/skills/$s
  sed "s|{{CUTTER_DIR}}|$CUTTER_DIR|g" skills/$s/SKILL.md > ~/.claude/skills/$s/SKILL.md
done
grep -L "{{CUTTER_DIR}}" ~/.claude/agents/video-cutter.md ~/.claude/skills/video-cut-*/SKILL.md
```

Der letzte Befehl muss ALLE fünf Dateien listen (= kein Platzhalter mehr
drin). Wenn nicht: prüfen und wiederholen.

## Schritt 4: AssemblyAI einrichten (mit deinem User zusammen)

Der Cutter transkribiert mit AssemblyAI (verbatim — der einzige Weg, wie
Versprecher sichtbar und damit schneidbar bleiben).

1. **Such deinem User den aktuellen Registrierungs-Link** (Websuche:
   AssemblyAI sign up). Stand Juli 2026: kostenloser Account mit **$50
   Startguthaben, ohne Kreditkarte** (~185 Stunden Transkription).
2. Warte, bis er den Account hat. Dann führe ihn zum API-Key:
   im AssemblyAI-Dashboard, typischerweise direkt auf der Startseite
   ("Your API key" / Kopier-Symbol).
3. Trage den Key ein:
   ```bash
   cp -n .env.example .env
   # dann AAI_KEY=<sein-key> in .env eintragen (Editor oder sed)
   set -a; . ./.env; set +a
   .venv312/bin/python -c "import os; assert os.environ.get('AAI_KEY'), 'Key fehlt'; print('AAI-Key gesetzt')"
   ```
4. **Niemals** den Key committen — die `.env` ist per `.gitignore`
   ausgeschlossen. Sag deinem User das explizit.

## Schritt 5: Abschluss + erste Nutzung erklären

Erkläre deinem User:

- **Schneiden:** In Claude Code einfach sagen:
  `schneide das Video /Pfad/zum/video.mov` — der video-cutter-Agent
  übernimmt (Transkription → Entscheidungen → Schnitt+QA → Render).
- **Dauer:** Bei 60 Min Video ca. eine Stunde, läuft größtenteils allein.
- **Cockpit:** Am Ende öffnet sich das Cut-Cockpit auf
  `http://127.0.0.1:8766/` im Browser — Feinschnitt wie in CapCut
  (W/Q/E-Cuts, Trimmen mit echter Waveform, Gain, B-Roll-Slots) und rechts
  ein Claude-Terminal mit dem Cutting-Agenten.
- **Kosten-Gefühl:** Transkription ~$0.21 pro Audio-Stunde vom
  $50-Startguthaben.

Wenn dein User das Agentic OS Obsidian-Plugin nutzt: Weiter mit
`INSTALL-AGENTIC-OS.md` (CUTTER-Tab direkt im OS).
