import ctypes
from ctypes import wintypes
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from assemblyai.streaming.v3 import (
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    TurnEvent,
)
import numpy as np
import pyaudiowpatch as pyaudio
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = APP_DIR / "config.json"
HISTORY_DIR = APP_DIR / "history"
LOG_FILE = APP_DIR / "dictation.log"
ICON_FILE = APP_DIR / "assets" / "icon.ico"
API_KEYS_URL = "https://www.assemblyai.com/dashboard"

CHUNK_SIZE = 4096
DEFAULT_SPEECH_MODEL = "universal-streaming-multilingual"
ENGLISH_SPEECH_MODEL = "universal-streaming-english"
HOTKEY_ID = 0xD17A
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_D = 0x44
WM_HOTKEY = 0x0312
SW_RESTORE = 9
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


def read_config() -> dict:
    try:
        return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def update_config(**values) -> None:
    # Write to a temp file and swap, so a crash mid-write never corrupts config.json (and the API key in it).
    tmp = LOCAL_CONFIG.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(read_config() | values, indent=2), encoding="utf-8")
        os.replace(tmp, LOCAL_CONFIG)
    except Exception:
        logging.exception("Could not save config")


def load_api_key() -> str:
    return str(read_config().get("api_key", "")).strip() or os.getenv("ASSEMBLYAI_API_KEY", "").strip()


def load_window_position() -> tuple[int, int]:
    config = read_config()
    try:
        x, y = int(config["window_x"]), int(config["window_y"])
    except Exception:
        return 60, 80
    # A monitor that was unplugged would otherwise leave the window opening off-screen.
    if QGuiApplication.screenAt(QPoint(x + 20, y + 20)) is None:
        return 60, 80
    return x, y


def load_speech_model() -> str:
    if read_config().get("speech_model") == ENGLISH_SPEECH_MODEL:
        return ENGLISH_SPEECH_MODEL
    return DEFAULT_SPEECH_MODEL


def run_quiet(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=APP_DIR, capture_output=True, text=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def install_requirements() -> None:
    # Same marker start_dictation.bat uses: reinstall only when requirements.txt changed.
    requirements = APP_DIR / "requirements.txt"
    marker = Path(sys.prefix) / "requirements.installed"
    if sys.prefix == sys.base_prefix or (marker.exists() and marker.read_bytes() == requirements.read_bytes()):
        return
    result = run_quiet(sys.executable, "-m", "pip", "install", "-r", str(requirements), timeout=600)
    if result.returncode:
        logging.error("Dependency update failed: %s", result.stderr.strip())
        return
    shutil.copyfile(requirements, marker)


def self_update() -> bool:
    """Fast-forward a git clone to the latest published version. Returns True when new code was pulled.

    config.json, history/ and dictation.log are git-ignored, so the user's key and settings survive.
    """
    if not (APP_DIR / ".git").exists():
        return False  # downloaded as a zip: nothing to pull
    try:
        if run_quiet("git", "status", "--porcelain", "--untracked-files=no").stdout.strip():
            return False  # local edits to the code: leave a developer's checkout alone
        before = run_quiet("git", "rev-parse", "HEAD").stdout
        result = run_quiet("git", "pull", "--ff-only", "--quiet")
        if result.returncode:
            logging.warning("Update skipped: %s", result.stderr.strip())
            return False
        if run_quiet("git", "rev-parse", "HEAD").stdout == before:
            return False
        install_requirements()
        return True
    except (OSError, subprocess.SubprocessError) as exc:  # git missing, offline timeout
        logging.warning("Update skipped: %s", exc)
        return False


class AudioRecorder:
    def __init__(self) -> None:
        self.p = pyaudio.PyAudio()
        self.stream = None
        self.is_recording = False

    def get_default_input_device(self):
        try:
            info = self.p.get_default_input_device_info()
            channels = max(1, min(int(info.get("maxInputChannels", 1)), 2))
            return int(info["index"]), int(info["defaultSampleRate"]), channels, info["name"]
        except Exception:
            host = self.p.get_host_api_info_by_type(pyaudio.paWASAPI)
            for i in range(host.get("deviceCount", 0)):
                dev = self.p.get_device_info_by_host_api_device_index(host["index"], i)
                if dev.get("maxInputChannels", 0) > 0 and not dev.get("isLoopbackDevice", False):
                    channels = max(1, min(int(dev.get("maxInputChannels", 1)), 2))
                    return int(dev["index"]), int(dev["defaultSampleRate"]), channels, dev["name"]
        raise RuntimeError("No microphone input device was found.")

    def start(self, on_audio, on_level, device_info=None) -> tuple[int, str]:
        if device_info is None:
            device_info = self.get_default_input_device()
        device_index, sample_rate, channels, device_name = device_info
        self.is_recording = True

        def callback(in_data, frame_count, time_info, status):
            if not self.is_recording:
                return (None, pyaudio.paComplete)
            audio_data = np.frombuffer(in_data, dtype=np.int16)
            if channels > 1:
                try:
                    audio_data = audio_data.reshape(-1, channels).mean(axis=1).astype(np.int16)
                except Exception:
                    pass
            on_audio(audio_data.tobytes())
            try:
                rms = float(np.sqrt(np.mean(audio_data.astype(np.float32) ** 2)))
                peak = float(np.max(np.abs(audio_data))) if audio_data.size else 0.0
                on_level(min(1.0, max(rms / 1800.0, peak / 18000.0)))
            except Exception:
                on_level(0.0)
            return (in_data, pyaudio.paContinue)

        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK_SIZE,
            stream_callback=callback,
        )
        self.stream.start_stream()
        return sample_rate, device_name

    def stop(self) -> None:
        self.is_recording = False
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def close(self) -> None:
        self.stop()
        try:
            self.p.terminate()
        except Exception:
            pass


class AppSignals(QObject):
    toggle = Signal()
    level = Signal(float)
    partial = Signal(str)
    final = Signal(str)
    status = Signal(str, str)
    error = Signal(str)
    stopped = Signal()
    updated = Signal()


class GearButton(QPushButton):
    """Thin-stroke gear drawn in code, to match the flat UI (the emoji gear has shading)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event) -> None:
        self.update()

    def leaveEvent(self, event) -> None:
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(226, 232, 240, 210 if self.underMouse() else 110)
        center = QPointF(self.width() / 2, self.height() / 2)
        pen = QPen(color, 1.3)
        painter.setPen(pen)
        painter.drawEllipse(center, 4.2, 4.2)
        painter.drawEllipse(center, 1.5, 1.5)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.FlatCap)
        painter.setPen(pen)
        for step in range(8):
            angle = step * math.pi / 4
            direction = QPointF(math.cos(angle), math.sin(angle))
            painter.drawLine(center + direction * 4.6, center + direction * 6.6)


class Waveform(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(26)
        self.levels = [0.0] * 42
        self.is_recording = False

    def add_level(self, level: float) -> None:
        self.levels.append(float(level))
        self.levels = self.levels[-42:]
        self.update()

    def reset(self) -> None:
        self.levels = [0.0] * 42
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor("#38bdf8" if self.is_recording else "#3f4654")
        pen = QPen(color)
        pen.setWidth(3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        width = max(1, self.width())
        height = max(1, self.height())
        mid = height / 2
        gap = width / max(1, len(self.levels))
        for idx, level in enumerate(self.levels):
            smoothed = max(0.04, min(1.0, level))
            bar_height = 3 + smoothed * (height - 4)
            x = idx * gap + gap * 0.5
            painter.drawLine(int(x), int(mid - bar_height / 2), int(x), int(mid + bar_height / 2))


class DictationWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dictation Typer")
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowOpacity(0.86)

        self.setGeometry(*load_window_position(), 340, 170)
        self.setFixedWidth(340)

        self.api_key = load_api_key()
        self.speech_model = load_speech_model()
        self.recorder = AudioRecorder()
        self.client = None
        self.is_recording = False
        self.session_transcript = []
        self.current_partial = ""
        self.last_saved_text = ""
        self.drag_origin = QPoint()
        self.current_textbox_height = 0
        self.current_shell_height = 0
        self.current_window_height = 0
        self.hotkey_stop = threading.Event()
        self.hotkey_thread_id = 0
        # Serializes connect/start and stop, so a stop pressed mid-connect can't leave the mic open.
        self.session_lock = threading.Lock()
        self.stream_failed = False
        self.stopping = False
        self.error_text = ""

        self.signals = AppSignals()
        self.signals.toggle.connect(self.toggle_recording)
        self.signals.level.connect(self.on_level)
        self.signals.partial.connect(self.on_partial)
        self.signals.final.connect(self.on_final)
        self.signals.status.connect(self.set_status)
        self.signals.error.connect(self.on_error)
        self.signals.stopped.connect(self.on_stopped)

        self.build_ui()
        self.start_hotkey_listener()
        self.signals.updated.connect(self.on_updated)
        threading.Thread(target=lambda: self_update() and self.signals.updated.emit(), daemon=True).start()
        if not self.api_key:
            self.toggle_settings()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and not self.isMinimized():
            self.raise_()

    def build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.card = QFrame()
        self.card.setObjectName("card")
        self.card.setStyleSheet("""
            QFrame#card {
                background: #111318;
                border: 1px solid #2a2d35;
                border-radius: 14px;
            }
            QLabel { color: #e5e7eb; }
        """)
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(9)
        self.record_button = QPushButton()
        self.record_button.setFixedSize(34, 34)
        self.record_button.clicked.connect(self.toggle_recording)
        self.set_record_style(False)
        header.addWidget(self.record_button)

        labels = QVBoxLayout()
        labels.setSpacing(1)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #f3f4f6; font: 700 13px 'Segoe UI';")
        self.hint_label = QLabel("Ctrl+Alt+D")
        self.hint_label.setStyleSheet("color: #9ca3af; font: 10px 'Segoe UI';")
        labels.addWidget(self.status_label)
        labels.addWidget(self.hint_label)
        header.addLayout(labels, 1)

        self.settings_button = GearButton()
        self.settings_button.setToolTip("API key")
        self.settings_button.clicked.connect(self.toggle_settings)
        header.addWidget(self.settings_button, 0, Qt.AlignTop)

        self.minimize_button = QPushButton("")
        self.minimize_button.setFixedSize(16, 16)
        self.minimize_button.clicked.connect(self.showMinimized)
        self.minimize_button.setStyleSheet(self.circle_style("rgba(251, 191, 36, 170)", "rgba(245, 158, 11, 210)", 8))
        self.close_button = QPushButton("")
        self.close_button.setFixedSize(16, 16)
        self.close_button.clicked.connect(self.close)
        self.close_button.setStyleSheet(self.circle_style("rgba(239, 68, 68, 170)", "rgba(220, 38, 38, 210)", 8))
        header.addWidget(self.minimize_button, 0, Qt.AlignTop)
        header.addWidget(self.close_button, 0, Qt.AlignTop)
        layout.addLayout(header)

        self.waveform = Waveform()
        layout.addWidget(self.waveform)

        shell_style = "QFrame#shell { background: #151821; border: 1px solid #2a2d35; border-radius: 10px; }"
        self.transcript_shell = QFrame()
        self.transcript_shell.setObjectName("shell")
        self.transcript_shell.setStyleSheet(shell_style)
        transcript_layout = QVBoxLayout(self.transcript_shell)
        transcript_layout.setContentsMargins(10, 4, 10, 8)
        transcript_layout.setSpacing(2)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.english_button = QPushButton("english")
        self.english_button.setCheckable(True)
        self.english_button.setChecked(self.speech_model == ENGLISH_SPEECH_MODEL)
        self.english_button.toggled.connect(self.set_english_mode)
        self.english_button.setStyleSheet(self.language_style())
        self.english_button.setFixedSize(48, 18)
        self.update_language_tooltip()
        self.clear_button = QPushButton("clear")
        self.clear_button.clicked.connect(self.clear_visible_text)
        self.clear_button.setStyleSheet(self.action_style())
        self.clear_button.setFixedSize(38, 18)
        self.log_button = QPushButton("log")
        self.log_button.clicked.connect(self.open_log)
        self.log_button.setStyleSheet(self.action_style())
        self.log_button.setFixedSize(28, 18)
        self.copy_button = QPushButton(chr(0x29C9))
        self.copy_button.clicked.connect(self.copy_transcript)
        self.copy_button.setStyleSheet(self.action_style(font_size=14))
        self.copy_button.setFixedSize(30, 18)
        actions.addWidget(self.english_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.log_button)
        actions.addWidget(self.copy_button)
        transcript_layout.addLayout(actions)

        self.textbox = QTextEdit()
        self.textbox.setReadOnly(True)
        self.textbox.setFrameShape(QFrame.NoFrame)
        self.textbox.setFixedWidth(294)
        self.textbox.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.textbox.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.textbox.setStyleSheet("""
            QTextEdit {
                background: #111827;
                color: #e5e7eb;
                border: 0;
                border-radius: 8px;
                font: 12px 'Segoe UI';
                padding: 8px 8px 6px 8px;
            }
        """)
        transcript_layout.addWidget(self.textbox, 0, Qt.AlignLeft)
        layout.addWidget(self.transcript_shell)

        self.settings_panel = QFrame()
        self.settings_panel.setObjectName("shell")
        self.settings_panel.setStyleSheet(shell_style)
        settings_layout = QVBoxLayout(self.settings_panel)
        settings_layout.setContentsMargins(10, 8, 10, 8)
        settings_layout.setSpacing(6)
        key_label = QLabel("AssemblyAI API key")
        key_label.setStyleSheet("color: #9ca3af; font: 10px 'Segoe UI';")
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)
        self.key_input.setPlaceholderText("paste your key")
        self.key_input.returnPressed.connect(self.save_settings)
        self.key_input.setStyleSheet("""
            QLineEdit {
                background: #111827;
                color: #e5e7eb;
                border: 1px solid #2a2d35;
                border-radius: 6px;
                padding: 5px 8px;
                font: 12px 'Segoe UI';
            }
            QLineEdit:focus { border-color: #38bdf8; }
        """)
        settings_row = QHBoxLayout()
        get_key = QLabel(f'<a href="{API_KEYS_URL}" style="color:#7dd3fc; text-decoration:none;">get a key</a>')
        get_key.setOpenExternalLinks(True)
        get_key.setStyleSheet("font: 10px 'Segoe UI';")
        save_button = QPushButton("save")
        save_button.clicked.connect(self.save_settings)
        save_button.setStyleSheet(self.action_style())
        save_button.setFixedSize(38, 18)
        settings_row.addWidget(get_key)
        settings_row.addStretch(1)
        settings_row.addWidget(save_button)
        settings_layout.addWidget(key_label)
        settings_layout.addWidget(self.key_input)
        settings_layout.addLayout(settings_row)
        self.settings_panel.hide()
        layout.addWidget(self.settings_panel)
        QTimer.singleShot(0, self.refresh_inline_transcript)

    def set_record_style(self, recording: bool) -> None:
        # Idle: dark button with a red record dot. Recording: red button with a stop square.
        if recording:
            glyph, color, background, hover, size = chr(0x25A0), "#ffffff", "#ef4444", "#dc2626", 13
        else:
            glyph, color, background, hover, size = chr(0x25CF), "#ef4444", "#1f2330", "#2a3040", 18
        self.record_button.setText(glyph)
        self.record_button.setStyleSheet(
            f"QPushButton {{ background: {background}; color: {color}; border: 1px solid #2a2d35;"
            f" border-radius: 17px; font: {size}px 'Segoe UI Symbol'; padding: 0 0 2px 0; }}"
            f" QPushButton:hover {{ background: {hover}; }}"
        )

    def circle_style(self, color: str, hover: str, radius: int) -> str:
        return f"QPushButton {{ background: {color}; border: 0; border-radius: {radius}px; }} QPushButton:hover {{ background: {hover}; }}"

    def action_style(self, font_size: int = 10) -> str:
        return f"""
            QPushButton {{
                background: transparent;
                color: #cbd5e1;
                border: 0;
                font: {font_size}px 'Segoe UI';
            }}
            QPushButton:hover {{ background: #202633; border-radius: 4px; }}
        """

    def language_style(self) -> str:
        return """
            QPushButton {
                background: transparent;
                color: rgba(203, 213, 225, 105);
                border: 0;
                font: 10px 'Segoe UI';
            }
            QPushButton:hover {
                background: #202633;
                color: rgba(226, 232, 240, 185);
                border-radius: 4px;
            }
            QPushButton:checked { color: #f8fafc; }
            QPushButton:checked:hover { color: #ffffff; }
            QPushButton:disabled { color: rgba(203, 213, 225, 75); }
            QPushButton:checked:disabled { color: #f8fafc; }
        """

    def set_english_mode(self, enabled: bool) -> None:
        self.speech_model = ENGLISH_SPEECH_MODEL if enabled else DEFAULT_SPEECH_MODEL
        update_config(speech_model=self.speech_model)
        self.update_language_tooltip()

    def toggle_settings(self) -> None:
        opening = self.settings_panel.isHidden()
        self.settings_panel.setVisible(opening)
        self.transcript_shell.setVisible(not opening)
        if opening:
            self.key_input.setText(self.api_key)
            self.key_input.setFocus()
        self.fit_height()

    def save_settings(self) -> None:
        self.api_key = self.key_input.text().strip()
        update_config(api_key=self.api_key)
        self.toggle_settings()
        self.set_status("Key saved" if self.api_key else "No API key", "#bbf7d0" if self.api_key else "#fbbf24")

    def update_language_tooltip(self) -> None:
        if self.speech_model == ENGLISH_SPEECH_MODEL:
            tooltip = "English model selected"
        else:
            tooltip = "Multilingual model selected (Portuguese included)"
        self.english_button.setToolTip(tooltip)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_origin)

    def mouseReleaseEvent(self, event) -> None:
        update_config(window_x=self.x(), window_y=self.y())

    def toggle_recording(self) -> None:
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        # While stopping, the pending `stopped` signal would otherwise reset the new session's UI.
        if self.is_recording or self.stopping:
            return
        if not self.api_key:
            self.api_key = load_api_key()
        if not self.api_key:
            if self.settings_panel.isHidden():
                self.toggle_settings()
            self.set_status("Add your API key", "#fbbf24")
            return
        if not self.settings_panel.isHidden():
            self.toggle_settings()
        self.error_text = ""
        self.hint_label.setToolTip("")
        self.session_transcript = []
        self.current_partial = ""
        self.current_textbox_height = 0
        self.current_shell_height = 0
        self.current_window_height = 0
        self.refresh_inline_transcript()
        self.is_recording = True
        self.english_button.setEnabled(False)
        self.waveform.is_recording = True
        self.waveform.reset()
        self.set_record_style(True)
        self.set_status("Connecting...", "#f3f4f6")
        threading.Thread(target=self.recording_worker, daemon=True).start()

    def recording_worker(self) -> None:
        with self.session_lock:
            try:
                self.stream_failed = False
                device_info = self.recorder.get_default_input_device()
                _idx, sample_rate, _channels, device_name = device_info
                self.client = StreamingClient(StreamingClientOptions(api_key=self.api_key, api_host="streaming.assemblyai.com"))
                self.client.on(StreamingEvents.Turn, self.on_turn)
                self.client.on(StreamingEvents.Error, self.on_stream_error)
                parameters = {
                    "sample_rate": sample_rate,
                    "format_turns": True,
                    "speech_model": self.speech_model,
                }
                if self.speech_model == DEFAULT_SPEECH_MODEL:
                    parameters["language_detection"] = True
                self.client.connect(StreamingParameters(**parameters))
                # connect() does not raise on failure (bad key, network): it calls on_stream_error and returns.
                if self.stream_failed or not self.is_recording:
                    return
                self.recorder.start(self.stream_audio, self.signals.level.emit, device_info=device_info)
                short_name = device_name[:28] + ("..." if len(device_name) > 28 else "")
                self.signals.status.emit("Recording", short_name)
            except Exception as exc:
                self.signals.error.emit(str(exc))

    def stream_audio(self, audio_data: bytes) -> None:
        if self.client and self.is_recording:
            self.client.stream(audio_data)

    def stop_recording(self) -> None:
        if not self.is_recording:
            return
        self.is_recording = False
        self.stopping = True
        self.set_status("Stopping...", "#fbbf24")
        threading.Thread(target=self.stop_worker, daemon=True).start()

    def stop_worker(self) -> None:
        with self.session_lock:
            self.recorder.stop()
            if self.client:
                try:
                    self.client.disconnect(terminate=True)
                except Exception:
                    pass
                self.client = None
        self.signals.stopped.emit()

    def on_turn(self, client: StreamingClient, event: TurnEvent) -> None:
        text = (event.transcript or "").strip()
        if not text:
            return
        if event.end_of_turn:
            self.signals.final.emit(text)
        else:
            self.signals.partial.emit(text)

    def on_stream_error(self, client: StreamingClient, error: StreamingError) -> None:
        self.stream_failed = True
        self.signals.error.emit(str(error))

    def on_level(self, level: float) -> None:
        self.waveform.add_level(level)

    def on_partial(self, text: str) -> None:
        self.current_partial = text
        self.refresh_inline_transcript()
        self.set_status("Listening...", "#bfdbfe")
        self.hint_label.setText("Speaking...")

    def on_final(self, text: str) -> None:
        self.current_partial = ""
        clean = self.normalize_text(text)
        if clean:
            self.session_transcript.append(clean)
        self.refresh_inline_transcript()
        self.set_status("Recording", "#bbf7d0")

    def on_updated(self) -> None:
        # The new code is already on disk; it runs from the next launch.
        if not self.is_recording and not self.error_text:
            self.set_status("Updated", "reopen to apply")

    def on_error(self, text: str) -> None:
        logging.error(text)
        self.error_text = text
        self.show_error()
        # A dead connection makes stream() a silent no-op, so stop instead of pretending to record.
        self.stop_recording()

    def show_error(self) -> None:
        self.set_status("Error", "#fca5a5")
        self.hint_label.setText(self.error_text[:36])
        self.hint_label.setToolTip(self.error_text)

    def on_stopped(self) -> None:
        self.is_recording = False
        self.stopping = False
        self.finish_session()
        self.english_button.setEnabled(True)
        self.waveform.is_recording = False
        self.waveform.reset()
        self.set_record_style(False)
        if self.error_text:
            self.show_error()
            return
        self.set_status("Ready", "#f3f4f6")
        self.hint_label.setText("Ctrl+Alt+D")

    def set_status(self, text: str, hint: str) -> None:
        self.status_label.setText(text)
        if hint.startswith("#"):
            self.status_label.setStyleSheet(f"color: {hint}; font: 700 13px 'Segoe UI';")
        else:
            self.hint_label.setText(hint)

    def normalize_text(self, text: str) -> str:
        return " ".join((text or "").split())

    def visible_text(self) -> str:
        parts = [part for part in self.session_transcript if part.strip()]
        if self.current_partial.strip():
            parts.append(self.normalize_text(self.current_partial))
        return " ".join(parts).strip()

    def refresh_inline_transcript(self) -> None:
        text = self.visible_text()
        self.setUpdatesEnabled(False)
        if self.textbox.toPlainText() != text:
            self.textbox.setPlainText(text)

        if text:
            metrics = QFontMetrics(self.textbox.font())
            rect = metrics.boundingRect(0, 0, 262, 10000, Qt.TextWordWrap, text)
            textbox_height = max(54, rect.height() + 28)
        else:
            textbox_height = 54

        shell_height = textbox_height + 40
        if abs(textbox_height - self.current_textbox_height) >= 2:
            self.current_textbox_height = textbox_height
            self.textbox.setFixedHeight(textbox_height)
        if abs(shell_height - self.current_shell_height) >= 2:
            self.current_shell_height = shell_height
            self.transcript_shell.setFixedHeight(shell_height)

        self.fit_height()
        self.setUpdatesEnabled(True)
        self.update()

    def fit_height(self) -> None:
        self.card.layout().activate()
        target_height = self.card.sizeHint().height() + 2
        if abs(target_height - self.current_window_height) >= 2:
            self.current_window_height = target_height
            self.setFixedHeight(target_height)

    def copy_transcript(self) -> None:
        text = self.visible_text()
        if not text:
            self.set_status("Nothing to copy", "#fbbf24")
            return
        QApplication.clipboard().setText(text)
        self.set_status("Copied transcript", "#bbf7d0")

    def clear_visible_text(self) -> None:
        # Clearing mid-recording would otherwise drop the not-yet-saved part of the session from history.
        self.save_current_session()
        self.current_partial = ""
        self.session_transcript = []
        self.current_textbox_height = 0
        self.current_shell_height = 0
        self.current_window_height = 0
        self.refresh_inline_transcript()

    def finish_session(self) -> None:
        # Speech cut off by stop never gets a final turn; keep it, since it was on screen.
        if self.current_partial.strip():
            self.session_transcript.append(self.normalize_text(self.current_partial))
            self.current_partial = ""
        self.save_current_session()

    def save_current_session(self) -> None:
        text = " ".join(self.session_transcript).strip()
        if not text or text == self.last_saved_text:
            return
        try:
            HISTORY_DIR.mkdir(exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = HISTORY_DIR / f"dictation_{stamp}.txt"
            path.write_text(text, encoding="utf-8")
            self.last_saved_text = text
        except Exception:
            logging.exception("Could not save history")

    def recent_history_text(self) -> str:
        if not HISTORY_DIR.exists():
            return ""
        sections = []
        try:
            for path in sorted(HISTORY_DIR.glob("dictation_*.txt"), reverse=True)[:5]:
                label = path.stem.replace("dictation_", "").replace("_", " ")
                sections.append(f"[{label}]\n{path.read_text(encoding='utf-8').strip()}")
        except Exception:
            pass
        return "\n\n".join(section for section in sections if section.strip())

    def open_log(self) -> None:
        self.log_window = QWidget(self, Qt.Window)
        self.log_window.setWindowTitle("Dictation log")
        self.log_window.resize(520, 360)
        layout = QVBoxLayout(self.log_window)
        text = QTextEdit()
        text.setReadOnly(True)
        current = self.visible_text() if self.is_recording else ""
        history = self.recent_history_text()
        sections = []
        if current:
            sections.append(f"Current session\n\n{current}")
        if history:
            sections.append(f"Recent saved sessions\n\n{history}")
        text.setPlainText("\n\n---\n\n".join(sections) if sections else "No transcript yet.")
        layout.addWidget(text)
        self.log_window.show()

    def start_hotkey_listener(self) -> None:
        threading.Thread(target=self.hotkey_loop, daemon=True).start()

    def hotkey_loop(self) -> None:
        self.hotkey_thread_id = int(kernel32.GetCurrentThreadId())
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT, VK_D):
            self.signals.error.emit("Hotkey used by another app")
            return
        msg = MSG()
        try:
            while not self.hotkey_stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.signals.toggle.emit()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def closeEvent(self, event) -> None:
        self.hotkey_stop.set()
        if self.hotkey_thread_id:
            user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
        self.finish_session()
        self.is_recording = False
        self.recorder.close()
        if self.client:
            try:
                self.client.disconnect(terminate=True)
            except Exception:
                pass
        event.accept()


def focus_running_instance() -> bool:
    """If the app is already open, bring that window to the front and return True."""
    # The mutex handle is never closed on purpose: Windows releases it when this process exits.
    win_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    win_kernel32.CreateMutexW(None, False, "DictationTyper.SingleInstance")
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return False
    hwnd = user32.FindWindowW(None, "Dictation Typer")
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    return True


def main() -> int:
    if focus_running_instance():
        return 0
    # pythonw has no console, so errors would otherwise vanish; this file is what to check when something fails.
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.excepthook = lambda *exc: logging.error("Unhandled error", exc_info=exc)
    # Own taskbar identity, so Windows shows our icon instead of grouping under pythonw.exe.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DictationTyper")
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(ICON_FILE)))
    window = DictationWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
