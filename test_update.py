"""Self-update check. Run: .venv\\Scripts\\python.exe test_update.py"""
import io
import subprocess
import tempfile
import zipfile
from pathlib import Path

import qt_main

assert qt_main.parse_version("v0.10.0") > qt_main.parse_version("0.9.1")

# Zip installs: a release zip overwrites the code but keeps the user's files.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    app = Path(tmp, "app")
    app.mkdir()
    (app / "code.txt").write_text("old")
    (app / "config.json").write_text('{"api_key": "secret"}')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("owner-repo-abc123/", "")
        archive.writestr("owner-repo-abc123/code.txt", "new")
        archive.writestr("owner-repo-abc123/assets/icon.ico", "icon")
        archive.writestr("owner-repo-abc123/../escape.txt", "evil")
    qt_main.extract_release(buffer.getvalue(), app)
    assert (app / "code.txt").read_text() == "new"
    assert (app / "assets" / "icon.ico").read_text() == "icon"
    assert (app / "config.json").read_text() == '{"api_key": "secret"}', "user config must survive"
    assert not Path(tmp, "escape.txt").exists(), "zip must not write outside the app folder"

# Git installs: fast-forward pull.


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    remote, app = Path(tmp, "remote"), Path(tmp, "app")
    remote.mkdir()
    git(remote, "init", "-q", "-b", "main")
    (remote / "code.txt").write_text("v1")
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "v1")
    git(tmp, "clone", "-q", str(remote), str(app))
    (app / "config.json").write_text('{"api_key": "secret"}')

    qt_main.APP_DIR = app
    qt_main.install_requirements = lambda: None

    assert qt_main.self_update() is False, "nothing new to pull"

    (remote / "code.txt").write_text("v2")
    git(remote, "commit", "-qam", "v2")
    assert qt_main.self_update() is True, "new version should be pulled"
    assert (app / "code.txt").read_text() == "v2"
    assert (app / "config.json").read_text() == '{"api_key": "secret"}', "user config must survive"

    (app / "code.txt").write_text("local edit")
    (remote / "code.txt").write_text("v3")
    git(remote, "commit", "-qam", "v3")
    assert qt_main.self_update() is False, "local code edits block the update"
    assert (app / "code.txt").read_text() == "local edit"

print("ok")
