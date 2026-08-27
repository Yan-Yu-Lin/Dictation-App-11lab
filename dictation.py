#!/usr/bin/env python3
"""
Dictation app using ElevenLabs Scribe v2 Realtime — Linux/Wayland (Hyprland) port.

Runs as a background daemon. Wayland does not allow apps to grab global
hotkeys, so control comes in over a unix socket instead:

    uv run dictation.py                # start the daemon
    python3 dictation-ctl.py toggle    # start/stop recording (bind this in Hyprland)

Transcript pipeline: ElevenLabs commit -> OpenCC (s2t) -> character
replacements -> artifact stripping -> paste at cursor (wl-copy + wtype).
No punctuation model and no LLM post-processing.
"""

import argparse
import array
import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from queue import Queue, Empty, Full
from typing import Optional

import opencc
import pyaudio
from dotenv import load_dotenv

try:
    import evdev
    from evdev import ecodes

    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
from elevenlabs import (
    AudioFormat,
    CommitStrategy,
    ElevenLabs,
    RealtimeEvents,
    RealtimeAudioOptions,
)
from elevenlabs.realtime.connection import RealtimeConnection

# Load environment variables
load_dotenv()

# Configuration
SAMPLE_RATE = 16000  # 16kHz recommended by ElevenLabs
CHUNK_SIZE = 4096  # Audio chunk size (0.25 seconds at 16kHz)
AUDIO_FORMAT = pyaudio.paInt16  # 16-bit PCM
CHANNELS = 1  # Mono
MAX_AUDIO_QUEUE_CHUNKS = 120  # ~30s of buffered audio at CHUNK_SIZE=4096
CONNECT_TIMEOUT_SECONDS = 8.0
FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 2.5
TRANSCRIPT_DEBUG_LOG = os.getenv("DICTATION_TRANSCRIPT_DEBUG", "1") != "0"
CHARACTER_REPLACEMENTS_PATH = os.path.join(
    os.path.dirname(__file__), "character_replacements.json"
)
DEFAULT_CHARACTER_REPLACEMENTS = {
    "纔": "才",
}
TRAILING_STRIP_CHARS = "。"
TRAILING_CUTOFF_DASH_CHARS = "-–—"
DICTATION_ELLIPSIS_ARTIFACT_RE = re.compile(r"[.．]{2,}|…+")
PASTE_REPLACEMENT_CHAR = "\ufffd"
PASTE_ZERO_WIDTH_CHARS = frozenset(("\u200b", "\u200c", "\u200d", "\ufeff"))
PASTE_ALLOWED_CONTROL_CHARS = frozenset(("\n", "\t", "\r"))

# Control socket (Hyprland keybind -> dictation-ctl.py -> here)
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dictation-app.sock"
)

# Sound effects (freedesktop sound theme)
SOUND_START = "/usr/share/sounds/freedesktop/stereo/audio-volume-change.oga"
SOUND_STOP = "/usr/share/sounds/freedesktop/stereo/message.oga"
SOUND_START_VOLUME = 0.25
SOUND_STOP_VOLUME = 0.25

# Window classes treated as terminals when the Hyprland "terminal" tag is absent
TERMINAL_CLASSES = {
    "alacritty",
    "com.mitchellh.ghostty",
    "foot",
    "ghostty",
    "kitty",
    "org.wezfurlong.wezterm",
    "xterm",
}

# Global state
event_loop = None  # Store reference to the event loop
status_notifier = None


def silence_alsa_errors():
    """Stop ALSA from spamming stderr while PyAudio probes devices.

    Purely cosmetic: PipeWire's ALSA plugin works fine, but device enumeration
    prints dozens of harmless config errors without this handler.
    """
    try:
        from ctypes import CDLL, CFUNCTYPE, c_char_p, c_int

        handler_type = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)

        def _noop_handler(filename, line, function, err, fmt):
            pass

        global _alsa_error_handler  # Keep a reference so ctypes callback survives
        _alsa_error_handler = handler_type(_noop_handler)
        CDLL("libasound.so.2").snd_lib_error_set_handler(_alsa_error_handler)
    except Exception:
        pass


def play_sound(sound_path, volume=0.25):
    """Play a system sound asynchronously (non-blocking)"""
    try:
        safe_volume = max(0.0, min(1.0, float(volume)))
        # paplay volume is linear 0-65536
        subprocess.Popen(
            ["paplay", f"--volume={int(safe_volume * 65536)}", sound_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # Silently fail if sound can't be played


class StatusNotifier:
    """Recording state indicator via desktop notifications.

    Reuses a single notification bubble (notify-send -p/-r) and closes it
    through the org.freedesktop.Notifications D-Bus API when dictation ends.
    """

    def __init__(self):
        self.notification_id: Optional[int] = None
        self.enabled = shutil.which("notify-send") is not None
        if not self.enabled:
            print("⚠️  notify-send not found; status notifications disabled")

    def _send(self, summary: str, urgency: str = "normal"):
        if not self.enabled:
            return
        try:
            command = [
                "notify-send",
                "-a",
                "Dictation",
                "-u",
                urgency,
                "-p",
            ]
            if self.notification_id is not None:
                command += ["-r", str(self.notification_id)]
            command.append(summary)
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=2
            )
            printed = result.stdout.strip()
            if printed.isdigit():
                self.notification_id = int(printed)
        except Exception:
            pass

    def show_recording(self):
        self._send("🎙️ Recording…", urgency="critical")

    def show_finalizing(self):
        self._send("⏳ Finalizing…")

    def show_partial(self, text: str):
        pass  # Live preview only makes sense on the floating dot

    def hide(self):
        if not self.enabled or self.notification_id is None:
            return
        try:
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.Notifications",
                    "--object-path",
                    "/org/freedesktop/Notifications",
                    "--method",
                    "org.freedesktop.Notifications.CloseNotification",
                    str(self.notification_id),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass
        self.notification_id = None


class DotOverlay:
    """Floating status dot rendered by dictation-dot.py (GTK4 layer-shell).

    The dot runs as a child process on the SYSTEM python3 (PyGObject isn't in
    the uv venv) and is driven by one-word commands on its stdin. Raises on
    construction if the child dies immediately, so the caller can fall back
    to desktop notifications.
    """

    DOT_SCRIPT = os.path.join(os.path.dirname(__file__), "dictation-dot.py")
    # PyGObject links libwayland before gtk4-layer-shell; preloading the layer
    # shell lib is the documented fix (gtk4-layer-shell/linking.md)
    LAYER_SHELL_LIB = "/usr/lib/libgtk4-layer-shell.so"

    def __init__(self):
        env = dict(os.environ)
        if os.path.exists(self.LAYER_SHELL_LIB):
            env["LD_PRELOAD"] = self.LAYER_SHELL_LIB
        self.process = subprocess.Popen(
            ["/usr/bin/python3", self.DOT_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.send_lock = threading.Lock()  # partials arrive from the SDK thread
        time.sleep(0.3)
        if self.process.poll() is not None:
            raise RuntimeError(
                f"dictation-dot.py exited with code {self.process.returncode}"
            )

    def _send(self, command: str):
        try:
            with self.send_lock:
                if self.process.poll() is not None:
                    return
                self.process.stdin.write((command + "\n").encode())
                self.process.stdin.flush()
        except Exception:
            pass

    def show_recording(self):
        self._send("recording")

    def show_finalizing(self):
        self._send("finalizing")

    def show_partial(self, text: str):
        self._send("partial " + text.replace("\n", " "))

    def hide(self):
        self._send("hide")

    def shutdown(self):
        self._send("quit")
        try:
            self.process.wait(timeout=2)
        except Exception:
            self.process.terminate()


def create_status_indicator():
    """Prefer the floating dot; fall back to desktop notifications."""
    try:
        overlay = DotOverlay()
        print("🔴 Status indicator: floating dot (layer-shell)")
        return overlay
    except Exception as e:
        print(f"⚠️  Floating dot unavailable ({e}); using notifications")
        return StatusNotifier()


def set_overlay_recording():
    if status_notifier:
        status_notifier.show_recording()


def set_overlay_finalizing():
    if status_notifier:
        status_notifier.show_finalizing()


def hide_overlay():
    if status_notifier:
        status_notifier.hide()


def _log_preview_text(text: str, limit: int = 220) -> str:
    """Build a single-line preview for transcript debug logs."""
    safe = text.replace("\n", "\\n")
    if len(safe) <= limit:
        return safe
    return safe[: limit - 3] + "..."


def log_transcript_stage(session_id: int, stage: str, text: str):
    """Print transcript text after each processing stage for debugging."""
    if not TRANSCRIPT_DEBUG_LOG:
        return
    preview = _log_preview_text(text)
    print(f"📊 [session {session_id}] {stage} (len={len(text)}): {preview}")


def pcm16_level(audio_data: bytes) -> tuple[int, float]:
    """Return peak and RMS amplitude for little-endian signed 16-bit PCM."""
    if not audio_data:
        return 0, 0.0

    samples = array.array("h")
    samples.frombytes(audio_data[: len(audio_data) - (len(audio_data) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()

    if not samples:
        return 0, 0.0

    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return peak, rms


def remove_dictation_ellipsis_artifacts(text: str) -> str:
    """Remove STT ellipsis pause/cutoff artifacts anywhere in the transcript.

    Scribe can emit "......" / "……" when speech drops or pauses. For dictation,
    those are almost always noise, even mid-sentence. A single English period is
    preserved because that may be intentional punctuation.
    """
    return DICTATION_ELLIPSIS_ARTIFACT_RE.sub("", text)


def strip_trailing_final_punctuation(text: str) -> str:
    """Strip dictation artifacts that should never be pasted.

    Ellipsis artifacts are removed anywhere: "它有一個......this" -> "它有一個this".
    Dash cutoff markers are only removed at the end: "它有一個--" -> "它有一個".
    Internal dashes/hyphens are preserved.
    """
    stripped = remove_dictation_ellipsis_artifacts(text).rstrip()

    while stripped:
        before = stripped
        stripped = stripped.rstrip(TRAILING_STRIP_CHARS).rstrip()
        stripped = stripped.rstrip(TRAILING_CUTOFF_DASH_CHARS).rstrip()

        if stripped == before:
            break

    return stripped


def sanitize_for_paste(text: str) -> str:
    """Remove damaged Unicode and invisible/control characters before paste."""
    sanitized_chars = []
    replacement_count = 0
    zero_width_count = 0
    control_count = 0

    for char in text:
        if char == PASTE_REPLACEMENT_CHAR:
            replacement_count += 1
            continue
        if char in PASTE_ZERO_WIDTH_CHARS:
            zero_width_count += 1
            continue
        if (
            unicodedata.category(char) == "Cc"
            and char not in PASTE_ALLOWED_CONTROL_CHARS
        ):
            control_count += 1
            continue
        sanitized_chars.append(char)

    total_count = replacement_count + zero_width_count + control_count
    if total_count:
        categories = []
        if replacement_count:
            categories.append(f"u+fffd ×{replacement_count}")
        if zero_width_count:
            categories.append(f"zero-width ×{zero_width_count}")
        if control_count:
            categories.append(f"control ×{control_count}")
        print(
            f"🧹 Sanitized {total_count} chars before paste: "
            + ", ".join(categories)
        )

    return "".join(sanitized_chars)


def active_window_is_terminal() -> bool:
    """Ask Hyprland whether the focused window is a terminal.

    Primary signal is the Omarchy "terminal" window tag (mirrors the logic in
    ~/.config/hypr/bindings.lua); window class is the fallback.
    """
    try:
        result = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        window = json.loads(result.stdout)
    except Exception:
        return False

    for tag in window.get("tags") or []:
        if tag.rstrip("*") == "terminal":
            return True

    window_class = (window.get("class") or "").lower()
    return window_class in TERMINAL_CLASSES


def read_clipboard() -> Optional[str]:
    """Read current text clipboard contents, or None if empty/non-text."""
    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def write_clipboard(text: str) -> bool:
    try:
        subprocess.run(["wl-copy", "--", text], timeout=2, check=True)
        return True
    except Exception:
        return False


def type_text_directly(text: str):
    """Fallback: type the text with wtype instead of pasting."""
    subprocess.run(["wtype", "--", text], timeout=10, check=True)


def paste_text(text):
    """Paste text at the cursor using the clipboard + a paste key chord.

    GUI apps get Ctrl+V; terminals (detected via Hyprland window tags/class)
    get Ctrl+Shift+V. Falls back to typing the text directly with wtype.
    """
    text = sanitize_for_paste(text)
    if not text:
        return

    try:
        # Save current clipboard
        old_clipboard = read_clipboard()

        # Copy text to clipboard
        if not write_clipboard(text):
            raise RuntimeError("wl-copy failed")
        time.sleep(0.15)  # Let the clipboard offer settle before pasting

        # Simulate the paste chord in the focused window
        if active_window_is_terminal():
            chord = ["-M", "ctrl", "-M", "shift", "-k", "v", "-m", "shift", "-m", "ctrl"]
        else:
            chord = ["-M", "ctrl", "-k", "v", "-m", "ctrl"]
        subprocess.run(["wtype", *chord], timeout=5, check=True)

        # Delay before restoring clipboard (gives time for paste and clipboard managers)
        time.sleep(0.6)

        # Restore old clipboard
        if old_clipboard is not None:
            write_clipboard(old_clipboard)
    except Exception as e:
        print(f"⚠️  Clipboard paste failed ({e}); typing text directly")
        try:
            type_text_directly(text)
        except Exception as type_error:
            print(f"❌ wtype fallback also failed: {type_error}")


class DictationApp:
    def __init__(self, chinese="tw"):
        self.is_recording = False
        self.audio_stream = None
        self.audio_interface = None
        self.connection = None
        self.last_partial_text = ""
        self.audio_queue = None  # Will be created per session
        self.audio_queue_session_id = None
        self.session_id = 0  # Track session number to handle parallel cleanup
        self.current_sender_task = None  # Track the current send_audio_chunks task
        self.session_lock = asyncio.Lock()  # Serialize start/stop
        self.active_session_id: Optional[int] = (
            None  # Identify which session events belong to
        )
        self.cleanup_task: Optional[asyncio.Task] = None
        self.connect_tasks: dict[int, asyncio.Task] = {}
        self.stopping_sessions: set[int] = set()
        self.registered_connection_sessions: set[int] = set()
        self.session_started_sessions: set[int] = set()
        self.commit_events: dict[int, asyncio.Event] = {}
        self.last_committed_text_by_session: dict[int, tuple[str, float]] = {}
        self.audio_callback_counts: dict[int, int] = {}
        self.audio_drop_counts: dict[int, int] = {}
        self.sender_chunk_counts: dict[int, int] = {}
        self.session_peak_audio: dict[int, int] = {}
        self.session_rms_sum: dict[int, float] = {}
        self.sender_first_send_at: dict[int, float] = {}
        self.paste_lock = asyncio.Lock()

        # Initialize Chinese character converter
        # s2t: Simplified to Traditional, t2s: Traditional to Simplified
        self.chinese_variant = chinese
        if chinese == "tw":
            self.chinese_converter = opencc.OpenCC("s2t")
        else:
            self.chinese_converter = opencc.OpenCC("t2s")
        self.character_replacement_items = self._load_character_replacements()

        # Initialize ElevenLabs client
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
        if not elevenlabs_key:
            print("ERROR: ELEVENLABS_API_KEY not found in .env file")
            sys.exit(1)

        self._install_realtime_event_history_patch()
        self.elevenlabs = ElevenLabs(api_key=elevenlabs_key)

        print(f"ElevenLabs API Key: ...{elevenlabs_key[-4:]}")

        # Keep PyAudio alive for the app lifetime. The input stream is opened
        # lazily and then start/stop is reused across sessions to avoid
        # repeated device teardown between rapid sessions.
        silence_alsa_errors()
        self.audio_interface = pyaudio.PyAudio()
        self.audio_stream = None

        print("Dictation App Ready!")
        print(
            f"Chinese output: {'Traditional (TW)' if chinese == 'tw' else 'Simplified (CN)'}"
        )
        print("Punctuation post-processing: none (raw Scribe v2 output)")
        print(f"Control socket: {SOCKET_PATH}")
        print("Toggle with: python3 dictation-ctl.py toggle (Hyper+D in Hyprland)\n")

    def _ensure_audio_stream(self):
        """Open the microphone stream once; restart this stream between sessions."""
        if self.audio_stream is not None:
            return

        self.audio_stream = self.audio_interface.open(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
            stream_callback=self.audio_callback,
        )

    def _stop_audio_stream(self):
        """Stop capture without closing the PortAudio stream object."""
        try:
            if self.audio_stream and self.audio_stream.is_active():
                self.audio_stream.stop_stream()
        except Exception as e:
            print(f"⚠️  Error stopping audio stream: {e}")

    def _load_character_replacements(self) -> list[tuple[str, str]]:
        """Load literal character replacements, longest source first."""
        replacements = dict(DEFAULT_CHARACTER_REPLACEMENTS)

        try:
            with open(CHARACTER_REPLACEMENTS_PATH, "r", encoding="utf-8") as file:
                loaded = json.load(file)

            if not isinstance(loaded, dict):
                raise ValueError("top-level JSON value must be an object")

            for source, target in loaded.items():
                if not isinstance(source, str) or not isinstance(target, str):
                    raise ValueError("all replacement keys and values must be strings")
                if not source:
                    raise ValueError("replacement keys cannot be empty strings")
                replacements[source] = target
        except FileNotFoundError:
            print(
                "⚠️  character_replacements.json not found; "
                "using built-in replacements"
            )
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(
                f"⚠️  Failed to load character_replacements.json ({e}); "
                "using built-in replacements"
            )
            replacements = dict(DEFAULT_CHARACTER_REPLACEMENTS)

        replacement_items = sorted(
            replacements.items(), key=lambda item: len(item[0]), reverse=True
        )
        print(f"🔤 Character replacements loaded: {len(replacement_items)}")
        return replacement_items

    def apply_character_replacements(self, text: str) -> str:
        """Apply literal character replacements using longest match first.

        Prints a before/after log when any replacement actually fires, listing
        which mappings were used.
        """
        original = text
        applied: list[str] = []
        for source, target in self.character_replacement_items:
            if source in text:
                count = text.count(source)
                applied.append(f"{source}→{target} (×{count})")
                text = text.replace(source, target)
        if applied:
            print(f"🔤 Replacements applied: {', '.join(applied)}")
            print(f"   before: {original}")
            print(f"   after:  {text}")
        return text

    @staticmethod
    def _install_realtime_event_history_patch():
        """Record SDK-emitted events so early session_started can be replayed."""
        if getattr(RealtimeConnection, "_dictation_event_history_patch", False):
            return

        original_emit = RealtimeConnection._emit

        def emit_with_history(connection, event, *args):
            history = getattr(connection, "_dictation_event_history", None)
            if history is None:
                history = []
                connection._dictation_event_history = history
            history.append((event, args))
            return original_emit(connection, event, *args)

        RealtimeConnection._emit = emit_with_history
        RealtimeConnection._dictation_event_history_patch = True

    def _replay_early_session_started(self, connection, session_id: int):
        """Replay session_started if the SDK emitted it before handlers existed."""
        if session_id in self.session_started_sessions:
            return

        for event, args in getattr(connection, "_dictation_event_history", []):
            event_value = getattr(event, "value", event)
            if event_value != RealtimeEvents.SESSION_STARTED.value:
                continue

            data = args[0] if args else {}
            print(f"🔁 Replaying early session_started event (session {session_id})")
            self.on_session_started(data, session_id)
            return

    def _register_connection_handlers(self, connection, session_id: int):
        """Register WebSocket event handlers once for a session."""
        if session_id in self.registered_connection_sessions:
            return

        connection.on(
            RealtimeEvents.SESSION_STARTED,
            lambda data, sid=session_id: self.on_session_started(data, sid),
        )
        connection.on(
            RealtimeEvents.PARTIAL_TRANSCRIPT,
            lambda data, sid=session_id: self.on_partial_transcript(data, sid),
        )
        connection.on(
            RealtimeEvents.COMMITTED_TRANSCRIPT,
            lambda data, sid=session_id: self.on_committed_transcript(data, sid),
        )
        connection.on(
            RealtimeEvents.ERROR,
            lambda error, sid=session_id: self.on_error(error, sid),
        )
        connection.on(
            RealtimeEvents.CLOSE,
            lambda *_, sid=session_id: self.on_close(sid),
        )
        self.registered_connection_sessions.add(session_id)
        self._replay_early_session_started(connection, session_id)

    async def start_recording(self):
        """Start recording audio and connect to ElevenLabs"""
        async with self.session_lock:
            if self.is_recording:
                return

            self.is_recording = True
            self.last_partial_text = ""

            # Increment session ID for this new session
            self.session_id += 1
            current_session = self.session_id
            self.active_session_id = current_session
            self.commit_events[current_session] = asyncio.Event()
            self.audio_callback_counts[current_session] = 0
            self.audio_drop_counts[current_session] = 0
            self.sender_chunk_counts[current_session] = 0
            self.session_peak_audio[current_session] = 0
            self.session_rms_sum[current_session] = 0.0

            # Create a NEW queue for this session (isolates from previous sessions)
            self.audio_queue = Queue(maxsize=MAX_AUDIO_QUEUE_CHUNKS)
            self.audio_queue_session_id = current_session

        # Start the existing stream object instead of closing/re-opening the
        # audio device every session.
        try:
            self._ensure_audio_stream()
            if not self.audio_stream.is_active():
                self.audio_stream.start_stream()

            play_sound(SOUND_START, SOUND_START_VOLUME)
            set_overlay_recording()
            print("\n🎙️  Recording started. Listening now...")
            print("🔄 Connecting to ElevenLabs realtime...")
        except Exception as e:
            print(f"❌ Error starting audio stream: {e}")
            hide_overlay()
            if self.audio_stream is not None:
                try:
                    self.audio_stream.close()
                except Exception:
                    pass
                self.audio_stream = None
            async with self.session_lock:
                if self.active_session_id == current_session:
                    self.is_recording = False
                    self.active_session_id = None
                    self.audio_queue = None
                    self.audio_queue_session_id = None
                self.commit_events.pop(current_session, None)
            return

        # Connect to ElevenLabs Realtime API (outside lock to keep hotkey responsive)
        try:
            realtime_client = getattr(self.elevenlabs.speech_to_text, "realtime", None)
            if realtime_client is None:
                raise RuntimeError(
                    "Realtime client unavailable in current ElevenLabs SDK"
                )

            if not getattr(realtime_client, "_dictation_no_verbatim_patch", False):
                original_build_websocket_url = realtime_client._build_websocket_url

                def build_websocket_url_with_no_verbatim(*args, **kwargs):
                    url = original_build_websocket_url(*args, **kwargs)
                    separator = "&" if "?" in url else "?"
                    return f"{url}{separator}no_verbatim=true"

                realtime_client._build_websocket_url = (
                    build_websocket_url_with_no_verbatim
                )
                realtime_client._dictation_no_verbatim_patch = True

            connect_task = asyncio.create_task(
                realtime_client.connect(
                    RealtimeAudioOptions(
                        model_id="scribe_v2_realtime",
                        audio_format=AudioFormat.PCM_16000,
                        sample_rate=SAMPLE_RATE,
                        commit_strategy=CommitStrategy.MANUAL,
                        include_timestamps=False,
                    )
                )
            )
            self.connect_tasks[current_session] = connect_task

            new_connection = await asyncio.wait_for(
                connect_task,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            self._register_connection_handlers(new_connection, current_session)

            # Stop may have happened while the websocket was connecting.
            # In that case cleanup owns this connection and will flush buffered audio.
            if current_session in self.stopping_sessions:
                return

            # If a stop happened during connect, drop this connection
            if (not self.is_recording) or (self.active_session_id != current_session):
                await new_connection.close()
                return

            # Only assign to self.connection after successfully creating it
            # Verify session is still current (protect against stale starts)
            if self.active_session_id != current_session:
                await new_connection.close()
                return

            self.connection = new_connection

            # Start sender only after websocket is ready.
            # Audio captured before this point stays buffered in self.audio_queue.
            self.current_sender_task = asyncio.create_task(
                self.send_audio_chunks(
                    new_connection, self.audio_queue, current_session
                )
            )

        except asyncio.TimeoutError:
            print(
                f"❌ Timed out connecting to ElevenLabs after {CONNECT_TIMEOUT_SECONDS:.1f}s"
            )
            if current_session in self.stopping_sessions:
                return
            hide_overlay()
            self._stop_audio_stream()
            if self.active_session_id == current_session:
                self.audio_queue = None
                self.audio_queue_session_id = None
                self.current_sender_task = None
                self.connection = None
                self.is_recording = False
                self.active_session_id = None
            self.commit_events.pop(current_session, None)
            self.connect_tasks.pop(current_session, None)
            self.registered_connection_sessions.discard(current_session)
            return
        except Exception as e:
            print(f"❌ Error connecting to ElevenLabs: {e}")
            if current_session in self.stopping_sessions:
                return
            hide_overlay()
            self._stop_audio_stream()
            if self.active_session_id == current_session:
                self.audio_queue = None
                self.audio_queue_session_id = None
                self.current_sender_task = None
                self.connection = None
                self.is_recording = False
                self.active_session_id = None
            self.commit_events.pop(current_session, None)
            self.connect_tasks.pop(current_session, None)
            self.registered_connection_sessions.discard(current_session)

    async def send_audio_chunks(self, connection, audio_queue: Queue, session_id: int):
        """Send audio chunks from the queue to ElevenLabs for one session."""
        print(f"📡 Sender task started (session {session_id})")
        last_wait_log = time.monotonic()
        try:
            while self.is_recording and self.active_session_id == session_id:
                try:
                    # Get audio chunk from queue (non-blocking with timeout)
                    try:
                        audio_data = audio_queue.get(timeout=0.01)
                    except Empty:
                        now = time.monotonic()
                        if now - last_wait_log >= 0.75:
                            callback_count = self.audio_callback_counts.get(
                                session_id, 0
                            )
                            print(
                                f"📡 Waiting for audio chunks "
                                f"(session {session_id}, callbacks={callback_count})"
                            )
                            last_wait_log = now
                        await asyncio.sleep(0.01)
                        continue

                    # Convert audio to base64
                    audio_base64 = base64.b64encode(audio_data).decode("utf-8")

                    # Send to ElevenLabs
                    await connection.send(
                        {"audio_base_64": audio_base64, "sample_rate": SAMPLE_RATE}
                    )
                    chunk_count = self.sender_chunk_counts.get(session_id, 0) + 1
                    self.sender_chunk_counts[session_id] = chunk_count
                    chunk_peak, chunk_rms = pcm16_level(audio_data)
                    if chunk_peak > self.session_peak_audio.get(session_id, 0):
                        self.session_peak_audio[session_id] = chunk_peak
                    self.session_rms_sum[session_id] = (
                        self.session_rms_sum.get(session_id, 0.0) + chunk_rms
                    )
                    if chunk_count == 1:
                        self.sender_first_send_at[session_id] = time.monotonic()
                        callback_count = self.audio_callback_counts.get(session_id, 0)
                        peak, rms = pcm16_level(audio_data)
                        print(
                            f"📡 First audio chunk sent "
                            f"(session {session_id}, callbacks={callback_count}, "
                            f"peak={peak}, rms={rms:.1f})"
                        )
                    elif chunk_count % 20 == 0:
                        callback_count = self.audio_callback_counts.get(session_id, 0)
                        drop_count = self.audio_drop_counts.get(session_id, 0)
                        print(
                            f"📡 Sent {chunk_count} audio chunks "
                            f"(session {session_id}, callbacks={callback_count}, "
                            f"drops={drop_count})"
                        )

                except Exception as e:
                    print(f"⚠️  Error sending audio (session {session_id}): {e}")
                    await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            # Task was cancelled, clean exit
            pass
        finally:
            callback_count = self.audio_callback_counts.get(session_id, 0)
            chunk_count = self.sender_chunk_counts.get(session_id, 0)
            drop_count = self.audio_drop_counts.get(session_id, 0)
            session_peak = self.session_peak_audio.get(session_id, 0)
            avg_rms = self.session_rms_sum.get(session_id, 0.0) / max(chunk_count, 1)
            print(
                f"📡 Sender task stopped (session {session_id}, "
                f"sent={chunk_count}, callbacks={callback_count}, drops={drop_count}, "
                f"peak={session_peak}, avg_rms={avg_rms:.1f})"
            )

    async def stop_recording(self):
        """Stop recording and commit the transcript"""
        async with self.session_lock:
            if not self.is_recording:
                return

            current_session = self.active_session_id or self.session_id

            # Immediately stop recording to allow new session to start
            self.is_recording = False
            self.stopping_sessions.add(current_session)

            # Play stop sound
            play_sound(SOUND_STOP, SOUND_STOP_VOLUME)
            set_overlay_finalizing()

            print("\n🛑 Recording stopped. Finalizing transcription...")

            # Cancel sender task; any remaining buffered chunks are flushed in cleanup
            if self.current_sender_task and not self.current_sender_task.done():
                self.current_sender_task.cancel()
                try:
                    await self.current_sender_task  # Wait for cancellation to complete
                except asyncio.CancelledError:
                    pass  # Expected

            # Stop capture so the mic releases, but keep the same PortAudio
            # stream object for the next session.
            self._stop_audio_stream()

            # Capture references to current session's resources
            old_connection = self.connection
            old_audio_queue = self.audio_queue
            old_connect_task = self.connect_tasks.get(current_session)
            queued_chunks = old_audio_queue.qsize() if old_audio_queue else 0
            print(
                f"📊 Session {current_session} audio summary before cleanup: "
                f"callbacks={self.audio_callback_counts.get(current_session, 0)}, "
                f"sent={self.sender_chunk_counts.get(current_session, 0)}, "
                f"queued={queued_chunks}, "
                f"drops={self.audio_drop_counts.get(current_session, 0)}"
            )

            # Clear references immediately so new session can start
            self.connection = None
            self.audio_queue = None
            self.audio_queue_session_id = None
            self.current_sender_task = None

            # Clean up old session asynchronously in background and remember task
            self.cleanup_task = asyncio.create_task(
                self._cleanup_session(
                    old_connection,
                    old_audio_queue,
                    current_session,
                    old_connect_task,
                )
            )

    async def _flush_remaining_audio(
        self,
        connection,
        audio_queue: Optional[Queue],
        session_to_cleanup: int,
    ):
        """Flush any buffered chunks that were captured before stop."""
        if not connection or not audio_queue:
            return

        flushed = 0
        while True:
            try:
                audio_data = audio_queue.get_nowait()
            except Empty:
                break

            audio_base64 = base64.b64encode(audio_data).decode("utf-8")
            await connection.send(
                {"audio_base_64": audio_base64, "sample_rate": SAMPLE_RATE}
            )
            flushed += 1
            self.sender_chunk_counts[session_to_cleanup] = (
                self.sender_chunk_counts.get(session_to_cleanup, 0) + 1
            )

        if flushed > 0:
            print(
                f"📤 Flushed {flushed} buffered chunks (session {session_to_cleanup})"
            )

    async def _cleanup_session(
        self,
        connection,
        audio_queue: Optional[Queue],
        session_to_cleanup: int,
        connect_task: Optional[asyncio.Task] = None,
    ):
        """Clean up a session's resources in the background"""
        try:
            if connection is None and connect_task is not None:
                try:
                    connection = await asyncio.wait_for(
                        asyncio.shield(connect_task),
                        timeout=CONNECT_TIMEOUT_SECONDS,
                    )
                    self._register_connection_handlers(connection, session_to_cleanup)
                    print(
                        "🔌 WebSocket connected after stop; finalizing buffered audio"
                    )
                except asyncio.TimeoutError:
                    print(
                        "⚠️  Connection did not finish after stop; dropping buffered audio"
                    )
                except asyncio.CancelledError:
                    print("⚠️  Connection task was cancelled during cleanup")
                except Exception as e:
                    print(f"⚠️  Connection failed during cleanup: {e}")

            # Commit and close connection
            if connection:
                try:
                    await self._flush_remaining_audio(
                        connection, audio_queue, session_to_cleanup
                    )

                    # Reset the event so we wait for the commit caused by this stop.
                    # Without this, prior auto-commit events (e.g. 90s periodic commit)
                    # can make wait() return immediately and drop the final chunk.
                    commit_event = self.commit_events.get(session_to_cleanup)
                    if commit_event:
                        commit_event.clear()

                    await connection.commit()

                    if commit_event:
                        try:
                            await asyncio.wait_for(
                                commit_event.wait(),
                                timeout=FINAL_TRANSCRIPT_TIMEOUT_SECONDS,
                            )
                        except asyncio.TimeoutError:
                            print(
                                "⚠️  Final transcript event timed out; closing session"
                            )

                    await connection.close()
                except Exception as e:
                    print(f"⚠️  Error closing connection: {e}")

            print("✅ Transcription complete!\n")
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")
        finally:
            self.commit_events.pop(session_to_cleanup, None)
            self.last_committed_text_by_session.pop(session_to_cleanup, None)
            self.connect_tasks.pop(session_to_cleanup, None)
            self.stopping_sessions.discard(session_to_cleanup)
            self.registered_connection_sessions.discard(session_to_cleanup)
            self.session_started_sessions.discard(session_to_cleanup)
            self.audio_callback_counts.pop(session_to_cleanup, None)
            self.audio_drop_counts.pop(session_to_cleanup, None)
            self.sender_chunk_counts.pop(session_to_cleanup, None)
            self.session_peak_audio.pop(session_to_cleanup, None)
            self.session_rms_sum.pop(session_to_cleanup, None)
            self.sender_first_send_at.pop(session_to_cleanup, None)
            # Clear active session only if this cleanup belongs to the active one
            if self.active_session_id == session_to_cleanup:
                self.active_session_id = None
                hide_overlay()
            elif not self.is_recording:
                hide_overlay()
            # Reset reference to this cleanup task
            if self.cleanup_task is asyncio.current_task():
                self.cleanup_task = None

    async def _process_final_transcript(self, text: str, session_id: int):
        """Process final transcript: convert characters, clean artifacts, paste."""
        async with self.paste_lock:
            log_transcript_stage(session_id, "input.committed", text)

            # Step 1: OpenCC conversion (sync, fast)
            converted_text = self.chinese_converter.convert(text)
            log_transcript_stage(session_id, "after.opencc", converted_text)

            # Step 2: User-configurable literal replacements after OpenCC.
            converted_text = self.apply_character_replacements(converted_text)
            log_transcript_stage(
                session_id, "after.character_replacements", converted_text
            )

            # Step 3: Strip dictation cutoff artifacts
            stripped_text = strip_trailing_final_punctuation(converted_text)
            if stripped_text != converted_text:
                print("🧹 Stripped trailing dictation cutoff marker")
            converted_text = stripped_text
            log_transcript_stage(session_id, "after.strip_trailing", converted_text)

            # Step 4: Paste (run in executor: paste_text sleeps while injecting keys)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, paste_text, converted_text)
            log_transcript_stage(session_id, "paste.output", converted_text)
            print(f"\n✅ Pasted: {converted_text}\n")
            self.last_partial_text = ""

    def audio_callback(self, in_data, frame_count, time_info, status):
        """Callback for audio stream - put chunks in queue"""
        # Capture session-bound references locally to prevent cross-session pollution.
        session_id = self.active_session_id
        queue_session_id = self.audio_queue_session_id
        queue = self.audio_queue
        if (
            self.is_recording
            and session_id is not None
            and queue is not None
            and queue_session_id == session_id
        ):
            callback_count = self.audio_callback_counts.get(session_id, 0) + 1
            self.audio_callback_counts[session_id] = callback_count
            if callback_count == 1 or callback_count % 20 == 0:
                print(
                    f"🎧 Audio callback active "
                    f"(session {session_id}, callbacks={callback_count})"
                )
            try:
                queue.put_nowait(in_data)
            except Full:
                # Keep newest audio if buffer is full
                self.audio_drop_counts[session_id] = (
                    self.audio_drop_counts.get(session_id, 0) + 1
                )
                try:
                    queue.get_nowait()
                except Empty:
                    pass
                try:
                    queue.put_nowait(in_data)
                except Full:
                    pass

        return (in_data, pyaudio.paContinue)

    def on_session_started(self, data, session_id):
        """Called when WebSocket session starts"""
        self.session_started_sessions.add(session_id)
        if session_id != self.active_session_id:
            return
        print("🔌 Connected to ElevenLabs Scribe v2 Realtime")

    def on_partial_transcript(self, data, session_id):
        """Called when partial transcript is received"""
        if session_id != self.active_session_id:
            return
        new_text = data.get("text", "").strip()

        if not new_text:
            return

        # Update internal state and show progress in console + live preview.
        # Preview goes through OpenCC + replacements so Arthur reads 繁體, not
        # whatever variant Scribe happens to emit mid-stream.
        self.last_partial_text = new_text
        print(f"📝 Processing: {new_text}")
        if status_notifier:
            try:
                preview_text = self.chinese_converter.convert(new_text)
                for source, target in self.character_replacement_items:
                    preview_text = preview_text.replace(source, target)
            except Exception:
                preview_text = new_text
            status_notifier.show_partial(preview_text)

    def on_committed_transcript(self, data, session_id):
        """Called when final transcript is committed"""
        commit_event = self.commit_events.get(session_id)
        if commit_event and event_loop:
            event_loop.call_soon_threadsafe(commit_event.set)

        if session_id not in self.commit_events:
            return

        final_text = data.get("text", "").strip()
        if not final_text:
            return

        log_transcript_stage(session_id, "event.committed", final_text)

        # Deduplicate rapid repeated committed events for the same segment.
        # Keep only near-identical repeats, allow same text later in long sessions.
        now = time.monotonic()
        last_committed = self.last_committed_text_by_session.get(session_id)
        if last_committed:
            last_text, last_time = last_committed
            if last_text == final_text and (now - last_time) < 1.5:
                log_transcript_stage(session_id, "dedupe.skip", final_text)
                return
        self.last_committed_text_by_session[session_id] = (final_text, now)

        if event_loop:
            # Schedule async processing (OpenCC + artifact cleanup + paste)
            asyncio.run_coroutine_threadsafe(
                self._process_final_transcript(final_text, session_id), event_loop
            )

    def on_error(self, error, session_id):
        """Called when an error occurs"""
        if session_id != self.active_session_id:
            return
        print(f"❌ Error: {error}")

    def on_close(self, session_id):
        """Called when connection closes"""
        if session_id != self.active_session_id:
            return
        print("🔌 Connection closed")
        # If the server dropped us mid-recording (auth error, network loss),
        # abort the session instead of letting the sender spin on a dead socket.
        if self.is_recording and event_loop:
            print("⚠️  Connection lost while recording; stopping session")
            asyncio.run_coroutine_threadsafe(self.stop_recording(), event_loop)

    def cleanup(self):
        """Clean up resources"""
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
        if self.audio_interface:
            self.audio_interface.terminate()


class RightShiftPTT:
    """Right Shift push-to-talk via evdev (hold to record, release to stop).

    Passive monitoring, like the macOS NSEvent version: Right Shift still
    works as a normal Shift key. keyd grabs the physical keyboards, so events
    actually arrive from keyd's virtual keyboard device; we simply watch every
    device that has a Right Shift and read from whichever delivers.
    Requires membership in the `input` group.
    """

    def __init__(self, app: DictationApp):
        self.app = app
        self.press_id = 0
        self.started_session = False
        self.watch_tasks: list[asyncio.Task] = []

    def start(self) -> int:
        devices = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue
            keys = device.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_RIGHTSHIFT in keys:
                devices.append(device)
            else:
                device.close()

        for device in devices:
            self.watch_tasks.append(asyncio.create_task(self._watch(device)))
        return len(devices)

    async def _watch(self, device):
        try:
            async for event in device.async_read_loop():
                if (
                    event.type == ecodes.EV_KEY
                    and event.code == ecodes.KEY_RIGHTSHIFT
                ):
                    if event.value == 1:  # press (2 = autorepeat, ignored)
                        self._on_press()
                    elif event.value == 0:  # release
                        self._on_release()
        except (OSError, asyncio.CancelledError):
            pass  # device unplugged or shutdown

    def _on_press(self):
        self.press_id += 1
        if not self.app.is_recording:
            self.started_session = True
            asyncio.create_task(self.app.start_recording())
        else:
            self.started_session = False

    def _on_release(self):
        if self.started_session:
            press_id = self.press_id
            self.started_session = False
            asyncio.create_task(self._stop_when_possible(press_id))

    async def _stop_when_possible(self, press_id: int):
        """Stop after release, even if start is still connecting."""
        for _ in range(25):
            if press_id != self.press_id:
                return
            if self.app.is_recording:
                await self.app.stop_recording()
                return
            await asyncio.sleep(0.02)

    def stop(self):
        for task in self.watch_tasks:
            task.cancel()
        self.watch_tasks.clear()


async def handle_control_client(app: DictationApp, reader, writer):
    """Handle one command from dictation-ctl.py over the unix socket."""
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=2)
        command = raw.decode("utf-8", "replace").strip().lower()
    except Exception:
        writer.close()
        return

    response = "err unknown command"
    if command == "toggle":
        if app.is_recording:
            await app.stop_recording()
            response = "ok stopped"
        else:
            asyncio.get_running_loop().create_task(app.start_recording())
            response = "ok started"
    elif command == "start":
        if app.is_recording:
            response = "ok already-recording"
        else:
            asyncio.get_running_loop().create_task(app.start_recording())
            response = "ok started"
    elif command == "stop":
        if app.is_recording:
            await app.stop_recording()
            response = "ok stopped"
        else:
            response = "ok not-recording"
    elif command == "status":
        response = "recording" if app.is_recording else "idle"

    try:
        writer.write((response + "\n").encode("utf-8"))
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def daemon_already_running() -> bool:
    """Check whether another daemon instance owns the control socket."""
    import socket as socket_module

    if not os.path.exists(SOCKET_PATH):
        return False

    probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(SOCKET_PATH)
        probe.sendall(b"status\n")
        probe.recv(64)
        return True
    except OSError:
        # Stale socket from a dead daemon
        return False
    finally:
        probe.close()


async def run_daemon(chinese: str, enable_right_shift_ptt: bool = True):
    global event_loop, status_notifier

    if daemon_already_running():
        print(f"ERROR: dictation daemon already running on {SOCKET_PATH}")
        sys.exit(1)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    event_loop = asyncio.get_running_loop()
    status_notifier = create_status_indicator()
    app = DictationApp(chinese=chinese)

    ptt = None
    if enable_right_shift_ptt:
        if not EVDEV_AVAILABLE:
            print("⚠️  evdev not installed; Right Shift push-to-talk disabled")
        else:
            ptt = RightShiftPTT(app)
            watched = ptt.start()
            if watched:
                print(
                    f"⇧ Right Shift push-to-talk enabled "
                    f"(hold to record; watching {watched} input device(s))"
                )
            else:
                ptt = None
                print(
                    "⚠️  No readable keyboard devices found; Right Shift "
                    "push-to-talk disabled (are you in the `input` group?)"
                )

    server = await asyncio.start_unix_server(
        lambda r, w: handle_control_client(app, r, w),
        path=SOCKET_PATH,
    )

    try:
        async with server:
            await server.serve_forever()
    finally:
        if ptt:
            ptt.stop()
        if app.is_recording:
            await app.stop_recording()
            await asyncio.sleep(1)  # Give cleanup a moment
        app.cleanup()
        hide_overlay()
        if isinstance(status_notifier, DotOverlay):
            status_notifier.shutdown()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dictation daemon using ElevenLabs Scribe v2 Realtime (Linux/Wayland)"
    )
    parser.add_argument(
        "--chinese",
        choices=["tw", "cn"],
        default="tw",
        help="Chinese character variant: tw (Traditional, default) or cn (Simplified)",
    )
    parser.add_argument(
        "--disable-right-shift-ptt",
        action="store_true",
        help="Disable hold-to-talk on Right Shift (Hyper+D toggle stays enabled)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            run_daemon(
                chinese=args.chinese,
                enable_right_shift_ptt=not args.disable_right_shift_ptt,
            )
        )
    except KeyboardInterrupt:
        print("\n\nShutting down...")
