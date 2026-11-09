# Dictation Typer

Minimal always-on-top Windows overlay that streams your microphone to [AssemblyAI](https://www.assemblyai.com/) and shows the live transcript, ready to copy. Works in English or multilingual mode (Portuguese included).

## Install

Requires Windows, Python 3.12+ and [Git](https://git-scm.com/downloads).

```bat
git clone https://github.com/aloumamae/dictation-typer.git
cd dictation-typer
start_dictation.bat
```

The first run creates a local `.venv`, installs `requirements.txt` and adds a **Dictation Typer** shortcut to your desktop. Use that shortcut from then on: it opens the app with no console window. Run the `.bat` again if you move the folder or update the requirements.

## Updates

Each time it opens, the app checks for a newer version in the background and pulls it (plus any new dependencies). When it shows **Updated**, reopen it to run the new version. Your API key, settings and history are never touched. Installs downloaded as a zip don't update; use `git clone`.

## API key

You need your own AssemblyAI key ([get one](https://www.assemblyai.com/dashboard)). On first launch the app asks for it; you can change it later with the gear icon. The key is stored in `config.json` next to the app, which is git-ignored.

The `ASSEMBLYAI_API_KEY` environment variable is used as a fallback.

## Controls

- `Ctrl+Alt+D`: start or stop dictation from anywhere.
- Red/green circle: start or stop.
- Drag any blank area to move the overlay.
- Gear: API key. Yellow dot: minimize. Red dot: close.
- `english`: English-only model. When dimmed, the multilingual model is active.
- `clear`: clear the visible text (history is kept).
- `log`: current session plus the last saved sessions.
- Copy icon: copy the visible transcript.

## Files

- `history/`: one text file per recorded session.
- `dictation.log`: errors, useful when something fails.

The app does not type into other apps; copy the text from the overlay when ready.
