"""Self-update check. Run: .venv\\Scripts\\python.exe test_update.py"""
import subprocess
import tempfile
from pathlib import Path

import qt_main


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
