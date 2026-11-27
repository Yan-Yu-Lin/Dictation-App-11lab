#!/usr/bin/env python3
"""
Dictation app using ElevenLabs Scribe (v3 Live / v3 batch) — Linux/Wayland (Hyprland) port.

Runs as a background daemon. Wayland does not allow apps to grab global
hotkeys, so control comes in over a unix socket instead:

    uv run dictation.py                # start the daemon
    python3 dictation-ctl.py toggle    # start/stop recording (bind this in Hyprland)
    python3 dictation-ctl.py preview   # live preview on/off
    python3 dictation-ctl.py mode      # live <-> batch

Transcript pipeline: Scribe text -> OpenCC (s2tw) -> character replacements ->
per-sentence punctuation -> artifact stripping -> paste at cursor.
No punctuation model and no LLM post-processing. Kept in step with the macOS
version (branch main); the text functions are copied verbatim from it.
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
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
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
# Scribe answers every commit, so a slow answer is still worth waiting for:
# giving up drops the end of what you said. This is the hard limit.
FINAL_TRANSCRIPT_MAX_SECONDS = 8.0
# Scribe Live commits by itself after ~35 s of uncommitted audio. If at least this
# much was uncommitted when you stopped, an auto-commit may still be on its way,
# so the stop waits for one more answer (the first one might be that auto-commit).
AUTO_COMMIT_RISK_SECONDS = 28.0
# systemctl stop: how long to let stopped recordings finish transcribing and paste.
SHUTDOWN_FINISH_SECONDS = 20.0
SHUTDOWN_MEDIA_SECONDS = 2.0
# Batch mode: one HTTP request per recording, sent after you stop.
MODES = ("live", "batch")
BATCH_MIN_TIMEOUT_SECONDS = 20  # longer recordings get 10 s + half their length
BATCH_ATTEMPTS = 2  # the audio is still in memory, so a failed request is retried once
TRANSCRIPT_DEBUG_LOG = os.getenv("DICTATION_TRANSCRIPT_DEBUG", "1") != "0"
CHARACTER_REPLACEMENTS_PATH = os.path.join(
    os.path.dirname(__file__), "character_replacements.json"
)
DEFAULT_CHARACTER_REPLACEMENTS = {
    "纔": "才",
}
KEYTERMS_PATH = os.path.join(os.path.dirname(__file__), "keyterms.json")
# Realtime keyterm limits (ElevenLabs docs): max 50 terms, 1000 chars total.
KEYTERMS_MAX_COUNT = 50
KEYTERMS_MAX_TOTAL_CHARS = 1000

# Model profiles, picked with --model (same as the macOS version).
#   v2: the original setup, kept unchanged as a fallback.
#   v3: Scribe v3 Live / Scribe v3 + keyterms.json + OpenCC s2tw (Taiwan characters:
#       裡/才/啟/著 instead of s2t's 裏/纔/啓/着).
MODEL_PROFILES = {
    "v2": {
        "model_id": "scribe_v2_realtime",
        "batch_model_id": "scribe_v2",
        "label": "Scribe v2 Realtime",
        "opencc_config": "s2t",
        "use_keyterms": False,
    },
    "v3": {
        "model_id": "scribe_v3_live",
        "batch_model_id": "scribe_v3",
        "label": "Scribe v3 Live",
        "opencc_config": "s2tw",
        "use_keyterms": True,
    },
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

# Live preview on/off and live/batch mode, kept across restarts.
SETTINGS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "dictation-app",
    "settings.json",
)


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings: dict):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️  Could not save settings: {e}")


# Global state
event_loop = None  # Store reference to the event loop
status_notifier = None
live_preview_enabled = bool(load_settings().get("live_preview", True))
# Pauses music/videos while recording (MediaPauser below). Created in run_daemon,
# so code that only imports this module never touches the media players.
media_pauser = None


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

    def show_notice(self, text: str):
        if not self.enabled:
            return
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Dictation", "-t", "1500", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def set_preview(self, enabled: bool):
        pass

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
        self.fallback: Optional[StatusNotifier] = None  # used once the dot has died
        time.sleep(0.3)
        if self.process.poll() is not None:
            raise RuntimeError(
                f"dictation-dot.py exited with code {self.process.returncode}"
            )


    def _send(self, command: str) -> bool:
        """Send one command; False once the dot process is gone."""
        try:
            with self.send_lock:
                if self.fallback is not None or self.process.poll() is not None:
                    return False
                self.process.stdin.write((command + "\n").encode())
                self.process.stdin.flush()
                return True
        except Exception:
            return False

    def _alive(self, command: str) -> bool:
        """Send, and switch to desktop notifications if the dot died."""
        if self._send(command):
            return True
        if self.fallback is None:
            print("⚠️  Floating dot exited; using notifications from now on")
            self.fallback = StatusNotifier()
        return False

    def show_recording(self):
        if not self._alive("recording"):
            self.fallback.show_recording()

    def show_finalizing(self):
        if not self._alive("finalizing"):
            self.fallback.show_finalizing()

    def show_partial(self, text: str):
        self._send("partial " + text.replace("\n", " "))

    def show_notice(self, text: str):
        if not self._alive("notice " + text.replace("\n", " ")):
            self.fallback.show_notice(text)

    def set_preview(self, enabled: bool):
        self._send("preview " + ("on" if enabled else "off"))

    def hide(self):
        if not self._alive("hide"):
            self.fallback.hide()

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
        overlay.set_preview(live_preview_enabled)
        print("🔴 Status indicator: floating dot (layer-shell)")
        return overlay
    except Exception as e:
        print(f"⚠️  Floating dot unavailable ({e}); using notifications")
        return StatusNotifier()


def show_overlay_notice(text: str):
    if status_notifier:
        status_notifier.show_notice(text)


class MediaPauser:
    """Pauses whatever is playing while you record, and resumes it afterwards.

    Linux counterpart of the macOS media_duck.py: every MPRIS player (browsers,
    Spotify, mpv, ...) that is Playing when a recording starts is paused, and
    exactly those players are resumed when it stops. Players that were already
    paused are left alone. Talks to D-Bus through `gdbus` (ships with glib2).

    Like media_duck.py it works from the latest wanted state, not a queue of
    every start/stop: one worker thread compares "should media be paused now?"
    with what it has done, so a quick stop + start never replays an old resume
    in the middle of the new recording. A player is owned only after its Pause
    call succeeded, and stays owned until it is confirmed resumed (or someone
    else changed it); a failed Play is retried, and once more at shutdown.
    """

    MPRIS_PREFIX = "org.mpris.MediaPlayer2."
    OBJECT_PATH = "/org/mpris/MediaPlayer2"
    CALL_TIMEOUT = 1.0  # seconds per gdbus call
    RETRY_SECONDS = 1.0  # a failed resume is tried again after this
    MAX_RESUME_ATTEMPTS = 4

    def __init__(self):
        self.enabled = shutil.which("gdbus") is not None
        self.cond = threading.Condition()
        self.owned: list[str] = []  # players we paused and have not resumed yet
        self.want_paused = False
        self.scanned = False  # this recording's pause scan is done
        self.stopped = False  # shutting down: no new pauses
        self.busy = False
        self.retry_at: Optional[float] = None
        self.attempts = 0
        if not self.enabled:
            print("⚠️  gdbus not found; media pausing disabled")
            return
        threading.Thread(target=self._run, name="media", daemon=True).start()

    # --- D-Bus -----------------------------------------------------------

    def _gdbus(self, *args) -> Optional[str]:
        """stdout of one gdbus call, or None when it failed or timed out."""
        try:
            result = subprocess.run(
                ["gdbus", "call", "--session", *args],
                capture_output=True,
                text=True,
                timeout=self.CALL_TIMEOUT,
            )
            return result.stdout if result.returncode == 0 else None
        except Exception:
            return None

    def _players(self) -> Optional[list[str]]:
        out = self._gdbus(
            "--dest", "org.freedesktop.DBus",
            "--object-path", "/org/freedesktop/DBus",
            "--method", "org.freedesktop.DBus.ListNames",
        )
        if out is None:
            return None
        return re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)

    def _status(self, player: str) -> str:
        """Playing / Paused / Stopped, or "" when it could not be read."""
        out = self._gdbus(
            "--dest", player,
            "--object-path", self.OBJECT_PATH,
            "--method", "org.freedesktop.DBus.Properties.Get",
            "org.mpris.MediaPlayer2.Player", "PlaybackStatus",
        )
        match = re.search(r"'(\w+)'", out or "")
        return match.group(1) if match else ""

    def _call(self, player: str, method: str) -> bool:
        out = self._gdbus(
            "--dest", player,
            "--object-path", self.OBJECT_PATH,
            "--method", f"org.mpris.MediaPlayer2.Player.{method}",
        )
        return out is not None

    # --- called from the event loop --------------------------------------

    def set_recording(self, recording: bool):
        if not self.enabled:
            return
        with self.cond:
            if recording and self.stopped:
                return
            if recording and not self.want_paused:
                self.scanned = False  # a new recording: scan again
            self.want_paused = recording
            self.retry_at = None
            self.attempts = 0
            self.cond.notify_all()

    def restore(self, timeout: float) -> bool:
        """No more pauses; resume what we paused. True when nothing is left paused."""
        if not self.enabled:
            return True
        deadline = time.monotonic() + timeout
        with self.cond:
            self.stopped = True
            self.want_paused = False
            self.retry_at = None
            self.cond.notify_all()
            while (self.busy or self.owned) and time.monotonic() < deadline:
                self.cond.wait(timeout=0.05)
            left = list(self.owned)
        if left:
            names = ", ".join(p[len(self.MPRIS_PREFIX):] for p in left)
            print(f"⚠️  Could not resume playback before exit: {names}")
        return not left

    # --- worker thread ---------------------------------------------------

    def _next_job(self) -> str:
        """Block until there is work: "pause" or "resume". Holds self.cond."""
        while True:
            if self.want_paused and not self.scanned:
                return "pause"
            if not self.want_paused and self.owned:
                now = time.monotonic()
                if self.retry_at is None or now >= self.retry_at:
                    return "resume"
                self.cond.wait(timeout=self.retry_at - now)
                continue
            self.cond.wait()

    def _run(self):
        while True:
            with self.cond:
                self.busy = False
                self.cond.notify_all()
                job = self._next_job()
                self.busy = True
            if job == "pause":
                self._pause_playing()
            else:
                self._resume_owned()

    def _wanted(self) -> bool:
        with self.cond:
            return self.want_paused

    def _pause_playing(self):
        paused = []
        for player in self._players() or []:
            if not self._wanted():
                break  # the recording already stopped: pause nothing more
            with self.cond:
                if player in self.owned:
                    continue
            if self._status(player) == "Playing" and self._call(player, "Pause"):
                with self.cond:
                    self.owned.append(player)  # recorded at once, not after the scan
                paused.append(player)
        with self.cond:
            if self.want_paused:
                self.scanned = True
        if paused:
            names = ", ".join(p[len(self.MPRIS_PREFIX):] for p in paused)
            print(f"⏸️  Paused playback: {names}")

    def _resume_owned(self):
        with self.cond:
            players = list(self.owned)
        live = self._players()
        done, resumed, failed = [], [], []
        for player in players:
            if self._wanted():
                return  # a new recording started meanwhile: keep them paused
            if live is not None and player not in live:
                done.append(player)  # the player was closed
                continue
            status = self._status(player)
            if status == "Paused":
                if self._call(player, "Play"):
                    done.append(player)
                    resumed.append(player)
                else:
                    failed.append(player)
            elif status in ("Playing", "Stopped"):
                done.append(player)  # someone else changed it meanwhile: leave it
            else:
                failed.append(player)  # could not even read it
        with self.cond:
            self.owned = [p for p in self.owned if p not in done]
            if failed:
                self.attempts += 1
                if self.attempts >= self.MAX_RESUME_ATTEMPTS and not self.stopped:
                    names = ", ".join(p[len(self.MPRIS_PREFIX):] for p in failed)
                    print(f"⚠️  Giving up resuming playback: {names}")
                    self.owned = [p for p in self.owned if p not in failed]
                    self.attempts = 0
                    self.retry_at = None
                else:
                    self.retry_at = time.monotonic() + (0.2 if self.stopped else self.RETRY_SECONDS)
            else:
                self.attempts = 0
                self.retry_at = None
        if resumed:
            print(f"▶️  Resumed playback ({len(resumed)} player(s))")


def set_media_paused(recording: bool):
    if media_pauser:
        media_pauser.set_recording(recording)


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


CJK_CHAR_RE = re.compile(r"[㐀-鿿豈-﫿぀-ヿ]")
LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
FULL_TO_HALF_PUNCT = {"，": ",", "。": ".", "？": "?", "！": "!", "；": ";", "：": ":"}
HALF_TO_FULL_PUNCT = {",": "，", ".": "。", "?": "？", "!": "！", ";": "；", ":": "："}
SENTENCE_END_FULL = "。？！"
SENTENCE_END_HALF = ".?!"


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split into sentences: after 。？！ / newline, or after .?! followed by space or end."""
    spans = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        boundary = c in SENTENCE_END_FULL or c == "\n" or (
            c in SENTENCE_END_HALF and (i + 1 == n or text[i + 1].isspace())
        )
        if boundary:
            j = i + 1
            while j < n and text[j] in SENTENCE_END_FULL + SENTENCE_END_HALF:
                j += 1
            spans.append((start, j))
            start = i = j
        else:
            i += 1
    if start < n:
        spans.append((start, n))
    return spans


def _half_mark_belongs_to_chinese(text: str, k: int) -> bool:
    """Should the half-width mark at k become full-width (it sits in a Chinese sentence)?"""
    c = text[k]
    j = k + 1
    while j < len(text) and text[j] == " ":
        j += 1
    nxt = text[j] if j < len(text) else ""
    next_is_cjk = bool(nxt) and bool(CJK_CHAR_RE.match(nxt))
    if c in ".?!":
        # A whole English clause ending here ("上面—— Your agent needs your input. 上面有…")
        # keeps its own mark; a word or two inside Chinese ("變成 DGX?") gets the Chinese one.
        before = text[:k]
        last_cjk = max((m.end() for m in CJK_CHAR_RE.finditer(before)), default=0)
        if len(LATIN_WORD_RE.findall(before[last_cjk:])) >= 3:
            return False
        if c == ".":
            return next_is_cjk  # keeps 3.5, Node.js
        return next_is_cjk or not nxt
    # , ; : only when Chinese follows: keeps 10:30 and "DGX, Spark" as they are
    return next_is_cjk


def normalize_mixed_punctuation(text: str) -> str:
    """English sentences get English punctuation, Chinese sentences get Chinese punctuation.

    Scribe picks the punctuation style per stretch of speech, so a short English sentence said
    between Chinese ones comes back as "Yeah，as expected，which is a good thing". The language is
    decided per sentence, not per word: a Chinese sentence that contains English words
    ("我剛剛選擇 GitHub Mobile，然後…") keeps its full-width marks.
      * sentence with Latin letters and no CJK: ，。？！；： -> , . ? ! ; : (plus a space before the next word)
      * sentence with CJK: , ; : . -> full-width when Chinese follows; ? ! -> full-width at the end too
    """
    if not text:
        return text
    english_at = [False] * len(text)
    chinese_at = [False] * len(text)
    for s, e in _sentence_spans(text):
        segment = text[s:e]
        has_cjk = bool(CJK_CHAR_RE.search(segment))
        is_english = not has_cjk and bool(LATIN_CHAR_RE.search(segment))
        for k in range(s, e):
            english_at[k] = is_english
            chinese_at[k] = has_cjk

    out: list[str] = []
    skip_spaces = False
    for k, c in enumerate(text):
        if skip_spaces:
            if c == " ":
                continue
            skip_spaces = False
        nxt = text[k + 1] if k + 1 < len(text) else ""
        if english_at[k] and c in FULL_TO_HALF_PUNCT:
            out.append(FULL_TO_HALF_PUNCT[c])
            if nxt and not nxt.isspace() and nxt not in FULL_TO_HALF_PUNCT and nxt not in HALF_TO_FULL_PUNCT:
                out.append(" ")
        elif chinese_at[k] and c in HALF_TO_FULL_PUNCT and _half_mark_belongs_to_chinese(text, k):
            while out and out[-1] == " ":
                out.pop()
            out.append(HALF_TO_FULL_PUNCT[c])
            skip_spaces = True
        else:
            out.append(c)
    return "".join(out)


def join_live_segments(segments: list[str]) -> str:
    """Join the pieces Scribe Live committed during one recording into one text.

    A long live recording comes back in several pieces, because Scribe commits
    on its own about every 35 s. A piece that was cut mid-sentence ends with a
    dash marker; the marker is dropped. A space is added only where an English
    word meets the next word, the way Scribe spaces text inside one piece.
    """
    joined = ""
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        joined = joined.rstrip(TRAILING_CUTOFF_DASH_CHARS).rstrip()
        if not joined:
            joined = segment
            continue
        prev, nxt = joined[-1], segment[0]
        prev_latin = prev.isascii() and (prev.isalnum() or prev in ".,?!;:")
        next_latin = nxt.isascii() and nxt.isalnum()
        prev_cjk = bool(CJK_CHAR_RE.match(prev))
        next_cjk = bool(CJK_CHAR_RE.match(nxt))
        if (prev_latin and (next_latin or next_cjk)) or (prev_cjk and next_latin):
            joined += " "
        joined += segment
    return joined


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


_uinput_keyboard = None


def get_uinput_keyboard():
    """Lazily create a kernel-level virtual keyboard for the paste chord.

    wtype uses the Wayland virtual-keyboard protocol, which games and many
    XWayland apps never see. uinput events are indistinguishable from a real
    keyboard, so they reach everything. Needs write access to /dev/uinput
    (covered by the `input` group on this machine).
    """
    global _uinput_keyboard
    if _uinput_keyboard is None:
        from evdev import UInput, ecodes

        _uinput_keyboard = UInput(
            {ecodes.EV_KEY: [ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_V]},
            name="dictation-paste-keyboard",
        )
        time.sleep(0.5)  # let keyd/Hyprland enumerate the new device
    return _uinput_keyboard


def press_paste_chord_uinput(with_shift: bool):
    """Send Ctrl(+Shift)+V as real kernel input events."""
    from evdev import ecodes

    ui = get_uinput_keyboard()
    mods = [ecodes.KEY_LEFTCTRL] + ([ecodes.KEY_LEFTSHIFT] if with_shift else [])
    for key in mods:
        ui.write(ecodes.EV_KEY, key, 1)
    ui.syn()
    time.sleep(0.03)
    ui.write(ecodes.EV_KEY, ecodes.KEY_V, 1)
    ui.syn()
    time.sleep(0.06)  # hold V across at least one game frame-poll
    ui.write(ecodes.EV_KEY, ecodes.KEY_V, 0)
    ui.syn()
    time.sleep(0.03)
    for key in reversed(mods):
        ui.write(ecodes.EV_KEY, key, 0)
    ui.syn()


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

        # Simulate the paste chord in the focused window.
        # uinput first (reaches games/XWayland); wtype as fallback.
        with_shift = active_window_is_terminal()
        try:
            press_paste_chord_uinput(with_shift)
        except Exception as uinput_error:
            print(f"⚠️  uinput chord failed ({uinput_error}); falling back to wtype")
            if with_shift:
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
    def __init__(self, chinese="tw", model="v3", mode="live"):
        self.profile = MODEL_PROFILES[model]
        self.mode = mode  # "live" or "batch"; read when a recording starts
        self.batch_sessions: set[int] = set()  # sessions recorded in batch mode
        # Every stopped recording (live or batch) gets one finalizer task. Each
        # finalizer pastes only after the previous one is done, so pastes keep
        # recording order across modes; `finalizers` is what shutdown waits for.
        self.delivery_tail: Optional[asyncio.Task] = None
        self.finalizers: set[asyncio.Task] = set()
        # Live mode: text Scribe committed while still recording, pasted at stop.
        self.live_segments: dict[int, list[str]] = {}
        # Live mode: committed events seen, and seconds of audio sent since the
        # last one (Scribe auto-commits after ~35 s of uncommitted audio).
        self.commit_counts: dict[int, int] = {}
        self.uncommitted_seconds: dict[int, float] = {}
        self.commit_signal: dict[int, asyncio.Event] = {}
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
            self.chinese_converter = opencc.OpenCC(self.profile["opencc_config"])
        else:
            self.chinese_converter = opencc.OpenCC("t2s")
        self.character_replacement_items = self._load_character_replacements()
        self.keyterms = (
            self._load_keyterms() if self.profile["use_keyterms"] else []
        )

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
            f"Model: {self.profile['label']} ({self.profile['model_id']}), "
            f"OpenCC: {self.profile['opencc_config']}, keyterms: {len(self.keyterms)}"
        )
        print(
            f"Mode: {self.mode} "
            f"(live = {self.profile['model_id']}, batch = {self.profile['batch_model_id']})"
        )
        print(
            f"Chinese output: {'Traditional (TW)' if chinese == 'tw' else 'Simplified (CN)'}"
        )
        print(f"Control socket: {SOCKET_PATH}")
        print(
            "Control: dictation-ctl.py toggle (Hyper+D) | preview (Hyper+Shift+D) "
            "| mode (Hyper+Shift+M)\n"
        )

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

    def _load_keyterms(self) -> list[str]:
        """Load keyterms.json (a JSON list of strings) within realtime limits."""
        try:
            with open(KEYTERMS_PATH, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if not isinstance(loaded, list) or not all(
                isinstance(term, str) for term in loaded
            ):
                raise ValueError("keyterms.json must be a JSON list of strings")
        except FileNotFoundError:
            print("⚠️  keyterms.json not found; running without keyterms")
            return []
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"⚠️  Failed to load keyterms.json ({e}); running without keyterms")
            return []

        terms: list[str] = []
        total_chars = 0
        for term in (t.strip() for t in loaded):
            if not term or term in terms:
                continue
            if (
                len(terms) >= KEYTERMS_MAX_COUNT
                or total_chars + len(term) > KEYTERMS_MAX_TOTAL_CHARS
            ):
                print(
                    f"⚠️  keyterms.json over the realtime limit "
                    f"({KEYTERMS_MAX_COUNT} terms / {KEYTERMS_MAX_TOTAL_CHARS} chars); "
                    f"ignoring from '{term}' on"
                )
                break
            terms.append(term)
            total_chars += len(term)

        print(f"🎯 Keyterms loaded: {len(terms)} ({total_chars} chars)")
        return terms

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
            self.commit_counts[current_session] = 0
            self.uncommitted_seconds[current_session] = 0.0
            self.audio_callback_counts[current_session] = 0
            self.audio_drop_counts[current_session] = 0
            self.sender_chunk_counts[current_session] = 0
            self.session_peak_audio[current_session] = 0
            self.session_rms_sum[current_session] = 0.0

            # The mode is fixed for the whole recording, even if it is switched meanwhile.
            batch = self.mode == "batch"
            if batch:
                self.batch_sessions.add(current_session)

            # Create a NEW queue for this session (isolates from previous sessions).
            # Batch keeps the whole recording, so its queue has no size limit.
            self.audio_queue = Queue(maxsize=0 if batch else MAX_AUDIO_QUEUE_CHUNKS)
            self.audio_queue_session_id = current_session

        # Start the existing stream object instead of closing/re-opening the
        # audio device every session.
        try:
            self._ensure_audio_stream()
            if not self.audio_stream.is_active():
                self.audio_stream.start_stream()

            play_sound(SOUND_START, SOUND_START_VOLUME)
            set_media_paused(True)
            set_overlay_recording()
            print("\n🎙️  Recording started. Listening now...")
            if batch:
                print("📼 Batch mode: audio is sent when you stop")
            else:
                print("🔄 Connecting to ElevenLabs realtime...")
        except Exception as e:
            print(f"❌ Error starting audio stream: {e}")
            set_media_paused(False)
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
                self.batch_sessions.discard(current_session)
            return

        if batch:
            return  # nothing to connect; stop_recording sends the audio

        # Connect to ElevenLabs Realtime API (outside lock to keep hotkey responsive)
        try:
            realtime_client = getattr(self.elevenlabs.speech_to_text, "realtime", None)
            if realtime_client is None:
                raise RuntimeError(
                    "Realtime client unavailable in current ElevenLabs SDK"
                )

            if not getattr(realtime_client, "_dictation_url_patch", False):
                original_build_websocket_url = realtime_client._build_websocket_url
                # SDK 2.45 has no options for these, so append them to the URL.
                extra_query = [("no_verbatim", "true")] + [
                    ("keyterms", term) for term in self.keyterms
                ]

                def build_websocket_url_with_extras(*args, **kwargs):
                    url = original_build_websocket_url(*args, **kwargs)
                    separator = "&" if "?" in url else "?"
                    return f"{url}{separator}{urllib.parse.urlencode(extra_query)}"

                realtime_client._build_websocket_url = build_websocket_url_with_extras
                realtime_client._dictation_url_patch = True

            connect_task = asyncio.create_task(
                realtime_client.connect(
                    RealtimeAudioOptions(
                        model_id=self.profile["model_id"],
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
            if not self.finalizers:
                hide_overlay()
            show_overlay_notice("連線失敗")
            set_media_paused(False)
            self._stop_audio_stream()
            if self.active_session_id == current_session:
                self.audio_queue = None
                self.audio_queue_session_id = None
                self.current_sender_task = None
                self.connection = None
                self.is_recording = False
                self.active_session_id = None
            self._forget_session(current_session)
            return
        except Exception as e:
            print(f"❌ Error connecting to ElevenLabs: {e}")
            if current_session in self.stopping_sessions:
                return
            if not self.finalizers:
                hide_overlay()
            show_overlay_notice("連線失敗")
            set_media_paused(False)
            self._stop_audio_stream()
            if self.active_session_id == current_session:
                self.audio_queue = None
                self.audio_queue_session_id = None
                self.current_sender_task = None
                self.connection = None
                self.is_recording = False
                self.active_session_id = None
            self._forget_session(current_session)

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
                    self.uncommitted_seconds[session_id] = self.uncommitted_seconds.get(
                        session_id, 0.0
                    ) + len(audio_data) / (SAMPLE_RATE * 2)
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

            # Play stop sound, then bring playback back
            play_sound(SOUND_STOP, SOUND_STOP_VOLUME)
            set_media_paused(False)
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

            # One finalizer per recording, chained so pastes keep recording
            # order across live and batch.
            previous = self.delivery_tail
            if current_session in self.batch_sessions:
                job = self._finish_batch_session(old_audio_queue, current_session, previous)
            else:
                job = self._cleanup_session(
                    old_connection,
                    old_audio_queue,
                    current_session,
                    old_connect_task,
                    previous,
                )
            task = asyncio.create_task(job)
            self.delivery_tail = task
            self.finalizers.add(task)
            task.add_done_callback(self._finalizer_done)

    def _finalizer_done(self, task: asyncio.Task):
        self.finalizers.discard(task)
        if self.delivery_tail is task:
            self.delivery_tail = None
        # The indicator goes away only when nothing is recording or pending.
        if not self.is_recording and not self.finalizers:
            hide_overlay()

    @staticmethod
    async def _wait_turn(previous: Optional[asyncio.Task]):
        """Wait until the previous recording has pasted (or failed)."""
        if previous is not None and not previous.done():
            await asyncio.wait({previous})

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
            self.uncommitted_seconds[session_to_cleanup] = self.uncommitted_seconds.get(
                session_to_cleanup, 0.0
            ) + len(audio_data) / (SAMPLE_RATE * 2)
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
        previous: Optional[asyncio.Task] = None,
    ):
        """Finish a live recording: get Scribe's last piece, then paste it all once."""
        sid = session_to_cleanup
        text = ""
        try:
            if connection is None and connect_task is not None:
                try:
                    connection = await asyncio.wait_for(
                        asyncio.shield(connect_task),
                        timeout=CONNECT_TIMEOUT_SECONDS,
                    )
                    self._register_connection_handlers(connection, sid)
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

            if connection:
                try:
                    await self._flush_remaining_audio(connection, audio_queue, sid)
                    await self._commit_and_wait(connection, sid)
                except Exception as e:
                    print(f"⚠️  Error finalizing transcript: {e}")
                try:
                    await connection.close()
                except Exception as e:
                    print(f"⚠️  Error closing connection: {e}")

            # Everything has been received (or the wait gave up): take all the
            # pieces at once. Nothing is pasted before this point, so a piece
            # that arrives late can never produce a second paste.
            segments = self.live_segments.pop(sid, [])
            self.commit_events.pop(sid, None)  # later events for this session are ignored
            if len(segments) > 1:
                print(f"🧩 Joining {len(segments)} pieces (session {sid})")
            text = join_live_segments(segments)
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")
        finally:
            self._forget_session(sid)

        await self._wait_turn(previous)
        if text:
            await self._process_final_transcript(text, sid)
        print("✅ Transcription complete!\n")

    async def _commit_and_wait(self, connection, sid: int):
        """Commit the end of the audio and wait for the answer to *that* commit.

        Scribe answers every manual commit with one committed_transcript, and also
        commits by itself after ~35 s of uncommitted audio. If enough audio was
        uncommitted that such an auto-commit may be in flight, the first answer
        after our commit could be that auto-commit, so we also wait a little for
        a second one.
        """
        signal_event = asyncio.Event()
        self.commit_signal[sid] = signal_event
        base = self.commit_counts.get(sid, 0)
        risky = self.uncommitted_seconds.get(sid, 0.0) >= AUTO_COMMIT_RISK_SECONDS
        await connection.commit()

        async def wait_for_count(count: int, seconds: float) -> bool:
            deadline = time.monotonic() + seconds
            while self.commit_counts.get(sid, 0) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                signal_event.clear()
                if self.commit_counts.get(sid, 0) >= count:
                    break
                try:
                    await asyncio.wait_for(signal_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
            return True

        if not await wait_for_count(base + 1, FINAL_TRANSCRIPT_MAX_SECONDS):
            print("⚠️  Final transcript event timed out; closing session")
            return
        if risky and not await wait_for_count(base + 2, FINAL_TRANSCRIPT_TIMEOUT_SECONDS):
            pass  # no auto-commit was in flight: the one answer was ours

    def _forget_session(self, session_id: int):
        """Drop a finished session's bookkeeping and hide the overlay if it is idle."""
        self.commit_events.pop(session_id, None)
        self.last_committed_text_by_session.pop(session_id, None)
        self.connect_tasks.pop(session_id, None)
        self.stopping_sessions.discard(session_id)
        self.registered_connection_sessions.discard(session_id)
        self.session_started_sessions.discard(session_id)
        self.batch_sessions.discard(session_id)
        self.live_segments.pop(session_id, None)
        self.audio_callback_counts.pop(session_id, None)
        self.audio_drop_counts.pop(session_id, None)
        self.sender_chunk_counts.pop(session_id, None)
        self.session_peak_audio.pop(session_id, None)
        self.session_rms_sum.pop(session_id, None)
        self.sender_first_send_at.pop(session_id, None)
        self.commit_counts.pop(session_id, None)
        self.uncommitted_seconds.pop(session_id, None)
        self.commit_signal.pop(session_id, None)
        # The overlay is hidden by _finalizer_done once nothing is pending.
        if self.active_session_id == session_id:
            self.active_session_id = None

    def _batch_request(self, pcm: bytes, timeout_seconds: int) -> str:
        """One blocking batch request (runs in a worker thread)."""
        extra = {"keyterms": self.keyterms} if self.keyterms else {}
        response = self.elevenlabs.speech_to_text.convert(
            model_id=self.profile["batch_model_id"],
            file=("dictation.pcm", pcm, "application/octet-stream"),
            file_format="pcm_s16le_16",  # raw 16 kHz mono PCM, same as the mic stream
            tag_audio_events=False,
            no_verbatim=True,
            request_options={"timeout_in_seconds": timeout_seconds},
            **extra,
        )
        return (getattr(response, "text", "") or "").strip()

    async def _transcribe_batch(self, pcm: bytes, session_id: int) -> Optional[str]:
        """Send one recording to the batch API. Returns None when it failed."""
        duration = len(pcm) / (SAMPLE_RATE * 2)
        timeout_seconds = int(max(BATCH_MIN_TIMEOUT_SECONDS, 10 + duration / 2))
        loop = asyncio.get_running_loop()
        for attempt in range(1, BATCH_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                text = await loop.run_in_executor(
                    None, self._batch_request, pcm, timeout_seconds
                )
            except Exception as e:
                status = getattr(e, "status_code", None)
                detail = (
                    f"HTTP {status}: {getattr(e, 'body', '')}"
                    if status is not None
                    else f"{type(e).__name__}: {e}"
                )
                print(
                    f"❌ Batch transcription failed (session {session_id}, "
                    f"attempt {attempt}/{BATCH_ATTEMPTS}, "
                    f"{time.monotonic() - started:.2f}s): {detail[:300]}"
                )
                # A 4xx (bad key, bad request, rate limit) is not retried: report it
                # now instead of making you wait for a second answer.
                retryable = status is None or status >= 500
                if not retryable or attempt == BATCH_ATTEMPTS:
                    return None
                await asyncio.sleep(0.5)
            else:
                print(
                    f"📨 Batch transcript received (session {session_id}, "
                    f"audio={duration:.1f}s, took {time.monotonic() - started:.2f}s)"
                )
                return text
        return None

    async def _finish_batch_session(
        self,
        audio_queue: Optional[Queue],
        session_id: int,
        previous: Optional[asyncio.Task] = None,
    ):
        """Batch mode: send the whole recording in one request, then paste."""
        try:
            text: Optional[str] = ""
            try:
                chunks = []
                while audio_queue is not None:
                    try:
                        chunks.append(audio_queue.get_nowait())
                    except Empty:
                        break
                pcm = b"".join(chunks)
                if pcm:
                    samples = array.array("h")
                    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
                    if sys.byteorder != "little":
                        samples.byteswap()
                    peak = max(max(samples), -min(samples)) if samples else 0
                    print(
                        f"📤 Sending {len(pcm) / (SAMPLE_RATE * 2):.1f}s of audio to "
                        f"{self.profile['batch_model_id']} "
                        f"(session {session_id}, peak={peak})"
                    )
                    text = await self._transcribe_batch(pcm, session_id)
                else:
                    print(f"🧹 No audio captured (session {session_id})")
            finally:
                self._forget_session(session_id)

            # Longer recordings take longer to come back; keep the paste order.
            await self._wait_turn(previous)

            if text is None:
                show_overlay_notice("轉錄失敗")
                print("❌ Nothing pasted: batch transcription failed\n")
                return
            if text:
                log_transcript_stage(session_id, "event.batch", text)
                await self._process_final_transcript(text, session_id)
            else:
                print("🧹 Suppressed empty/no-speech transcript")
            print("✅ Transcription complete!\n")
        except Exception as e:
            print(f"⚠️  Error during batch transcription (session {session_id}): {e}")

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

            # Step 2b: English sentences get English punctuation, Chinese ones Chinese.
            normalized_text = normalize_mixed_punctuation(converted_text)
            if normalized_text != converted_text:
                print("🔣 Normalized mixed-language punctuation")
            converted_text = normalized_text
            log_transcript_stage(session_id, "after.punctuation", converted_text)

            # Step 3: Strip dictation cutoff artifacts
            stripped_text = strip_trailing_final_punctuation(converted_text)
            if stripped_text != converted_text:
                print("🧹 Stripped trailing dictation cutoff marker")
            converted_text = stripped_text
            log_transcript_stage(session_id, "after.strip_trailing", converted_text)

            # A suppression replacement can intentionally remove the whole transcript.
            if not converted_text.strip():
                log_transcript_stage(session_id, "paste.skip_empty", converted_text)
                print("🧹 Suppressed empty/no-speech transcript")
                self.last_partial_text = ""
                return

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
        print(f"🔌 Connected to ElevenLabs {self.profile['label']}")

    def on_partial_transcript(self, data, session_id):
        """Called when partial transcript is received"""
        if session_id != self.active_session_id:
            return
        new_text = data.get("text", "").strip()

        if not new_text:
            return

        # Update internal state and show progress in console + live preview.
        # Preview goes through the same text pipeline as the paste, so it reads
        # 繁體 with the right punctuation, not whatever Scribe emits mid-stream.
        self.last_partial_text = new_text
        print(f"📝 Processing: {new_text}")
        if status_notifier:
            try:
                held = self.live_segments.get(session_id)
                if held:
                    new_text = join_live_segments([*held, new_text])
                preview_text = self.chinese_converter.convert(new_text)
                for source, target in self.character_replacement_items:
                    preview_text = preview_text.replace(source, target)
                preview_text = normalize_mixed_punctuation(preview_text)
            except Exception:
                preview_text = new_text
            status_notifier.show_partial(preview_text)

    def on_committed_transcript(self, data, session_id):
        """Called when Scribe commits a piece of the transcript.

        Nothing is pasted here. Scribe Live commits on its own about every 35 s
        of a long recording; pasting those pieces right away put text into
        whatever app had focus mid-speech. Pieces are held and pasted together
        when the recording stops (see _cleanup_session).
        """
        try:
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

            segments = self.live_segments.setdefault(session_id, [])
            segments.append(final_text)
            if session_id not in self.stopping_sessions:
                print(
                    f"⏸️  Holding piece {len(segments)} until you stop "
                    f"(session {session_id})"
                )
        finally:
            # Count every answer (empty and deduped ones too: each is one commit
            # answered) only after its text is stored, then wake the stop path.
            if session_id in self.commit_events:
                self.commit_counts[session_id] = self.commit_counts.get(session_id, 0) + 1
                self.uncommitted_seconds[session_id] = 0.0
                signal_event = self.commit_signal.get(session_id)
                if signal_event is not None:
                    signal_event.set()

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
        self.watch_tasks: dict[str, asyncio.Task] = {}
        self.pressed_devices: set[str] = set()
        self.rescan_task = None
        self.stopping = False

    def start(self) -> int:
        self.stopping = False
        self._scan_devices()
        self.rescan_task = asyncio.create_task(self._rescan())
        return len(self.watch_tasks)

    def _scan_devices(self):
        for path in evdev.list_devices():
            if path in self.watch_tasks:
                continue
            device = None
            try:
                device = evdev.InputDevice(path)
                keys = device.capabilities().get(ecodes.EV_KEY, [])
                if ecodes.KEY_RIGHTSHIFT in keys:
                    self.watch_tasks[path] = asyncio.create_task(self._watch(device, path))
                    print(f"⇧ PTT watching {device.name} ({path})", flush=True)
                    continue
            except OSError:
                pass
            if device is not None:
                device.close()

    async def _rescan(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                self._scan_devices()
        except asyncio.CancelledError:
            pass

    async def _watch(self, device, path):
        try:
            async for event in device.async_read_loop():
                if (
                    event.type == ecodes.EV_KEY
                    and event.code == ecodes.KEY_RIGHTSHIFT
                ):
                    if event.value == 1:  # press (2 = autorepeat, ignored)
                        was_pressed = bool(self.pressed_devices)
                        self.pressed_devices.add(path)
                        if not was_pressed:
                            self._on_press()
                    elif event.value == 0:  # release
                        self._release_device(path)
        except (OSError, asyncio.CancelledError):
            pass  # device unplugged or shutdown
        finally:
            device.close()
            self._release_device(path)
            if self.watch_tasks.get(path) is asyncio.current_task():
                self.watch_tasks.pop(path, None)

    def _release_device(self, path):
        was_pressed = path in self.pressed_devices
        self.pressed_devices.discard(path)
        if was_pressed and not self.pressed_devices and not self.stopping:
            self._on_release()

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
        self.stopping = True
        if self.rescan_task:
            self.rescan_task.cancel()
        for task in list(self.watch_tasks.values()):
            task.cancel()
        self.watch_tasks.clear()
        self.pressed_devices.clear()


def toggle_live_preview() -> str:
    """Flip live preview on/off, save it, and show a short notice."""
    global live_preview_enabled
    live_preview_enabled = not live_preview_enabled
    settings = load_settings()
    settings["live_preview"] = live_preview_enabled
    save_settings(settings)
    state = "on" if live_preview_enabled else "off"
    print(f"👁️  Live preview {state}")
    play_sound(SOUND_START if live_preview_enabled else SOUND_STOP, 0.15)
    if status_notifier:
        status_notifier.set_preview(live_preview_enabled)
        status_notifier.show_notice(
            "即時預覽：開" if live_preview_enabled else "即時預覽：關"
        )
    return f"ok preview {state}"


def toggle_transcribe_mode(app: DictationApp) -> str:
    """Switch between live and batch, save it, and show a short notice."""
    app.mode = "batch" if app.mode == "live" else "live"
    settings = load_settings()
    settings["mode"] = app.mode
    save_settings(settings)
    print(f"🔀 Mode {app.mode} (applies to the next recording)")
    play_sound(SOUND_START if app.mode == "batch" else SOUND_STOP, 0.15)
    show_overlay_notice(
        "模式：錄完再送（batch）" if app.mode == "batch" else "模式：即時（live）"
    )
    return f"ok mode {app.mode}"


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
    elif command == "preview":
        response = toggle_live_preview()
    elif command == "mode":
        response = toggle_transcribe_mode(app)
    elif command == "info":
        response = (
            f"{'recording' if app.is_recording else 'idle'} "
            f"model={app.profile['model_id']} mode={app.mode} "
            f"preview={'on' if live_preview_enabled else 'off'} "
            f"keyterms={len(app.keyterms)}"
        )

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


async def run_daemon(
    chinese: str,
    enable_right_shift_ptt: bool = True,
    model: str = "v3",
    mode: Optional[str] = None,
    pause_media: bool = True,
):
    global event_loop, status_notifier, media_pauser

    if daemon_already_running():
        print(f"ERROR: dictation daemon already running on {SOCKET_PATH}")
        sys.exit(1)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    event_loop = asyncio.get_running_loop()
    # systemctl stop / restart sends SIGTERM; stop the server cleanly so the
    # finally block below runs (resume paused media, remove the socket).
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            event_loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    if pause_media:
        media_pauser = MediaPauser()
        if media_pauser.enabled:
            print("⏯️  Media pausing on: players that are playing pause while you record")

    # No --mode: use the one last picked with Hyper+Shift+M (live the first time).
    if mode is None:
        mode = load_settings().get("mode")
    if mode not in MODES:
        mode = "live"

    status_notifier = create_status_indicator()
    app = DictationApp(chinese=chinese, model=model, mode=mode)
    print(f"👁️  Live preview {'on' if live_preview_enabled else 'off'}")

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
                print(
                    "⚠️  No readable keyboard devices found; Right Shift "
                    "push-to-talk waiting for devices (are you in the `input` group?)"
                )

    server = await asyncio.start_unix_server(
        lambda r, w: handle_control_client(app, r, w),
        path=SOCKET_PATH,
    )

    try:
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            await stop_event.wait()
            print("\n\nShutting down...")
            serve_task.cancel()
    finally:
        # Order matters: media first (most visible if left wrong), then let the
        # recordings already stopped finish and paste, then tear down.
        try:
            if ptt:
                ptt.stop()
            if app.is_recording:
                await app.stop_recording()  # also asks the media pauser to resume
            if media_pauser:
                await asyncio.get_running_loop().run_in_executor(
                    None, media_pauser.restore, SHUTDOWN_MEDIA_SECONDS
                )
            pending = set(app.finalizers)
            if pending:
                print(f"⏳ Finishing {len(pending)} transcription(s) before exit...")
                done, not_done = await asyncio.wait(
                    pending, timeout=SHUTDOWN_FINISH_SECONDS
                )
                if not_done:
                    print(
                        f"⚠️  Gave up on {len(not_done)} transcription(s) after "
                        f"{SHUTDOWN_FINISH_SECONDS:.0f}s; their text was not pasted"
                    )
                    for task in not_done:
                        task.cancel()
        finally:
            app.cleanup()
            hide_overlay()
            if isinstance(status_notifier, DotOverlay):
                status_notifier.shutdown()
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dictation daemon using ElevenLabs Scribe, live or batch (Linux/Wayland)"
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
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PROFILES),
        default="v3",
        help="v3 (default) = Scribe v3 Live / v3 + keyterms.json + OpenCC s2tw; "
        "v2 = Scribe v2 Realtime (old setup)",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="live = stream while you speak; batch = send the whole recording when you stop. "
        "Default: whatever was last picked with Hyper+Shift+M (live the first time)",
    )
    parser.add_argument(
        "--no-pause-media",
        action="store_true",
        help="Do not pause music/videos while recording",
    )
    args = parser.parse_args()

    # Warm up the uinput virtual keyboard so the first paste doesn't pay the
    # device-enumeration delay. Missing /dev/uinput access just means the
    # wtype fallback will handle chords.
    try:
        get_uinput_keyboard()
        print("⌨️  uinput paste keyboard ready")
    except Exception as e:
        print(f"⚠️  uinput unavailable ({e}); paste will use wtype only")

    try:
        asyncio.run(
            run_daemon(
                chinese=args.chinese,
                enable_right_shift_ptt=not args.disable_right_shift_ptt,
                model=args.model,
                mode=args.mode,
                pause_media=not args.no_pause_media,
            )
        )
    except KeyboardInterrupt:
        print("\n\nShutting down...")
