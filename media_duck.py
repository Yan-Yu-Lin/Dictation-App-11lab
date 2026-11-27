"""Quiet the Mac's own audio while dictating, and bring it back afterwards.

When a recording starts and something is playing:
  * speakers:   fade the output volume down to silence, then pause the "now playing"
                app (Music, a browser tab, ...) so it does not run on unheard.
  * headphones: the mic cannot hear them, so only lower the volume a little.
When the recording stops, playback is resumed and the volume fades back in.

Nothing is touched when the output is muted or nothing is playing.

The volume is the system output volume (CoreAudio, through ctypes). Pause/resume goes
through the `media-control` command-line tool (`brew install media-control`); without it
only the volume is changed.

Try it without the dictation app:
    uv run python media_duck.py --status          # read-only: what would happen now
    uv run python media_duck.py --test            # duck for 3 s, then restore
    uv run python media_duck.py --test --as speakers   # try the speaker behaviour on headphones
"""

import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

DUCK_DELAY_SECONDS = 0.15  # a recording shorter than this (a stray key tap) changes nothing
FADE_OUT_SECONDS = 0.8  # how long the volume takes to go down when a recording starts
FADE_IN_SECONDS = 1.5  # how long it takes to come back after the recording stops
FADE_IN_JUMP = 0.35  # speakers: come back to this share of the volume quickly, fade the rest
FADE_IN_JUMP_SECONDS = 0.12
HEADPHONE_DUCK = 0.5  # headphones: lower to this share of the current volume
STEP_SECONDS = 0.03
VOLUME_TOLERANCE = 0.02  # the volume differs from ours by more than this = the user changed it

# Processes that keep an output stream open all the time without playing anything.
DEFAULT_IGNORED_APPS = ("com.apple.CoreSpeech", "com.joytap.app-setapp")

STATE_PATH = os.path.expanduser(
    "~/Library/Application Support/Dictation-App-11lab/media_duck_state.json"
)

_ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
_cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))


class _Addr(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


def _cc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


def _uncc(value) -> str:
    try:
        return int(value).to_bytes(4, "big").decode("ascii")
    except (TypeError, ValueError, UnicodeDecodeError, OverflowError):
        return ""


_GLOBAL, _OUTPUT = _cc("glob"), _cc("outp")
_SYSTEM_OBJECT = 1

_ca.AudioObjectGetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
_ca.AudioObjectGetPropertyDataSize.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint32),
]
_ca.AudioObjectSetPropertyData.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_void_p,
]
_ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr)]
_ca.AudioObjectHasProperty.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [
    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFRelease.argtypes = [ctypes.c_void_p]


def _get(obj: int, selector: str, scope: int = _GLOBAL, ctype=ctypes.c_uint32):
    addr = _Addr(_cc(selector), scope, 0)
    if not _ca.AudioObjectHasProperty(obj, ctypes.byref(addr)):
        return None
    value = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(value))
    status = _ca.AudioObjectGetPropertyData(
        obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value)
    )
    return value.value if status == 0 else None


def _set(obj: int, selector: str, value, ctype) -> bool:
    addr = _Addr(_cc(selector), _OUTPUT, 0)
    if not _ca.AudioObjectHasProperty(obj, ctypes.byref(addr)):
        return False
    boxed = ctype(value)
    status = _ca.AudioObjectSetPropertyData(
        obj, ctypes.byref(addr), 0, None, ctypes.sizeof(boxed), ctypes.byref(boxed)
    )
    return status == 0


def _get_string(obj: int, selector: str) -> str:
    ref = _get(obj, selector, _GLOBAL, ctypes.c_void_p)
    if not ref:
        return ""
    buffer = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(ref, buffer, 512, 0x08000100)  # UTF-8
    _cf.CFRelease(ref)
    return buffer.value.decode("utf-8", "replace") if ok else ""


def _get_ids(obj: int, selector: str) -> list[int]:
    addr = _Addr(_cc(selector), _GLOBAL, 0)
    if not _ca.AudioObjectHasProperty(obj, ctypes.byref(addr)):
        return []
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return []
    ids = (ctypes.c_uint32 * (size.value // 4))()
    if _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ids):
        return []
    return list(ids)[: size.value // 4]


def default_output_device() -> Optional[int]:
    return _get(_SYSTEM_OBJECT, "dOut") or None


def get_volume(device: int) -> Optional[float]:
    """Output volume 0.0-1.0, or None when the device has no volume control (HDMI)."""
    return _get(device, "vmvc", _OUTPUT, ctypes.c_float)


def set_volume(device: int, volume: float) -> bool:
    return _set(device, "vmvc", max(0.0, min(1.0, volume)), ctypes.c_float)


def is_muted(device: int) -> bool:
    return _get(device, "mute", _OUTPUT) == 1


def device_kind(device: int) -> str:
    """'headphones' when the mic cannot hear the output, otherwise 'speakers'."""
    transport = _uncc(_get(device, "tran"))
    source = _uncc(_get(device, "ssrc", _OUTPUT))
    name = _get_string(device, "lnam").lower()
    if transport in ("blue", "blea"):  # AirPods and other Bluetooth audio
        return "headphones"
    if source == "hdpn" or "headphone" in name or "airpods" in name:
        return "headphones"
    return "speakers"


def apps_playing_audio(ignored: tuple[str, ...] = DEFAULT_IGNORED_APPS) -> list[str]:
    """Bundle ids of apps with an output stream running right now."""
    apps = []
    for process in _get_ids(_SYSTEM_OBJECT, "prs#"):
        if not _get(process, "piro"):
            continue
        bundle = _get_string(process, "pbid")
        # No bundle id = a command-line player, e.g. our own start/stop sound (afplay).
        if bundle and bundle not in ignored and bundle not in apps:
            apps.append(bundle)
    return apps


def _media_control_path() -> Optional[str]:
    return shutil.which("media-control") or next(
        (p for p in ("/opt/homebrew/bin/media-control", "/usr/local/bin/media-control")
         if os.path.exists(p)),
        None,
    )


def now_playing() -> Optional[dict]:
    """{'bundle': ..., 'playing': bool} for the system's "now playing" app, or None."""
    tool = _media_control_path()
    if not tool:
        return None
    try:
        out = subprocess.run(
            [tool, "get", "--no-artwork"], capture_output=True, text=True, timeout=2
        )
        info = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict):
        return None
    return {
        "bundle": info.get("bundleIdentifier") or "",
        "playing": bool(info.get("playing")),
    }


def _media_command(command: str) -> bool:
    tool = _media_control_path()
    if not tool:
        return False
    try:
        return subprocess.run([tool, command], capture_output=True, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class MediaDucker:
    """Runs the fades on its own thread; `set_recording` never blocks the caller."""

    def __init__(self, ignored_apps: tuple[str, ...] = DEFAULT_IGNORED_APPS, log=print):
        self.ignored_apps = tuple(ignored_apps)
        self.log = log
        self._cond = threading.Condition()
        self._want = False  # True while a recording is running
        # What the worker last finished: True = quieted, False = restored,
        # None = a fade was cut short by a start/stop and has to be redone.
        self._at: Optional[bool] = False
        self._busy = False  # the worker is in the middle of a fade
        self._saved: Optional[dict] = None  # what to put back
        self._last_set: Optional[float] = None  # the volume we set last
        self._dead = False
        recover_stale_state(log)
        threading.Thread(target=self._run, name="media-duck", daemon=True).start()

    def set_recording(self, recording: bool):
        with self._cond:
            self._want = bool(recording)
            self._cond.notify_all()

    def is_idle(self) -> bool:
        with self._cond:
            return not self._want and self._at is False and not self._busy

    # -- worker -------------------------------------------------------------

    def _run(self):
        while True:
            with self._cond:
                while self._want == self._at and not self._dead:
                    self._cond.wait()
                if self._dead:
                    return
                if self._want and self._at is False:
                    # Wait a moment before quieting anything: a stray tap of the
                    # push-to-talk key should not make the music dip.
                    self._cond.wait(DUCK_DELAY_SECONDS)
                    if not self._want or self._dead:
                        continue
                want = self._want
                self._busy = True
            try:
                finished = self._duck() if want else self._restore()
                result = want if finished else None
            except Exception as e:  # never let a bad audio call kill dictation
                self.log(f"⚠️  Media duck error: {e}")
                result = want
            with self._cond:
                self._at = result
                self._busy = False

    def _set_volume(self, device: int, volume: float):
        if not self._dead and set_volume(device, volume):
            self._last_set = get_volume(device)

    def _ramp(self, device: int, end: float, seconds: float, want: bool) -> bool:
        """Fade to `end`. Returns False when a start/stop arrived mid-fade."""
        start = get_volume(device)
        if start is None:
            return True
        # Timed by the clock, not by step count: setting the volume on Bluetooth
        # headphones is slow, and the fade must still take `seconds`.
        began = time.monotonic()
        while True:
            if self._want != want or self._dead:
                return False
            t = min(1.0, (time.monotonic() - began) / seconds) if seconds > 0 else 1.0
            eased = t * t * (3 - 2 * t)  # slow start, slow end
            self._set_volume(device, start + (end - start) * eased)
            if t >= 1.0:
                return True
            time.sleep(STEP_SECONDS)

    def _duck(self) -> bool:
        """Returns True when done; False when the recording stopped mid-fade."""
        if self._saved is None:
            device = default_output_device()
            if device is None:
                return True
            volume = get_volume(device)
            if is_muted(device) or volume == 0:
                return True  # nothing can be heard anyway
            media = now_playing()
            others = apps_playing_audio(self.ignored_apps)
            if not (media and media["playing"]) and not others:
                return True  # nothing is playing
            self._saved = {
                "device": device,
                "uid": _get_string(device, "uid "),
                "kind": device_kind(device),
                "volume": volume,
                "muted_by_us": False,
                "paused": "",
            }
            self._last_set = volume
            self._write_state()
            self.log(
                f"🔉 Quieting playback for dictation ({self._saved['kind']}, "
                f"volume {'n/a' if volume is None else round(volume, 2)}, "
                f"playing: {', '.join(others) or (media or {}).get('bundle', '?')})"
            )
        saved = self._saved
        device, speakers = saved["device"], saved["kind"] == "speakers"
        if saved["volume"] is not None:
            target = 0.0 if speakers else saved["volume"] * HEADPHONE_DUCK
            if not self._ramp(device, target, FADE_OUT_SECONDS, want=True):
                return False  # stopped mid-fade; _restore brings the volume back
            if speakers and is_muted(device):
                saved["muted_by_us"] = True  # some devices mute themselves at volume 0
        if speakers and self._want and not saved["paused"]:
            media = now_playing()
            if media and media["playing"] and _media_command("pause"):
                saved["paused"] = media["bundle"] or "?"
                self._write_state()
        return self._want

    def _restore(self) -> bool:
        """Returns True when everything is back; False when a new recording cut in."""
        saved = self._saved
        if saved is None:
            return True
        device, original = saved["device"], saved["volume"]
        ours = (
            original is not None
            and self._last_set is not None
            and (get_volume(device) is not None)
            and abs(get_volume(device) - self._last_set) <= VOLUME_TOLERANCE
        )
        if original is not None and not ours:
            self.log("🔉 Volume was changed during dictation; leaving it as it is")
        if saved["muted_by_us"] and is_muted(device):
            _set(device, "mute", 0, ctypes.c_uint32)
        saved["muted_by_us"] = False
        if ours and saved["kind"] == "speakers":
            floor = original * FADE_IN_JUMP
            if get_volume(device) < floor:
                if not self._ramp(device, floor, FADE_IN_JUMP_SECONDS, want=False):
                    return False
        if saved["paused"]:
            media = now_playing()
            if media and media["bundle"] == saved["paused"] and not media["playing"]:
                _media_command("play")
            saved["paused"] = ""
        if ours and not self._ramp(device, original, FADE_IN_SECONDS, want=False):
            return False
        self._saved = None
        self._last_set = None
        self._clear_state()
        self.log("🔊 Playback restored")
        return True

    # -- safety net ---------------------------------------------------------

    def emergency_restore(self):
        """Put everything back at once (the app is quitting). Safe to call twice."""
        self._dead = True
        with self._cond:
            self._cond.notify_all()
        saved, self._saved = self._saved, None
        if saved is None:
            return
        try:
            device, original = saved["device"], saved["volume"]
            current = get_volume(device)
            if (
                original is not None
                and current is not None
                and self._last_set is not None
                and abs(current - self._last_set) <= VOLUME_TOLERANCE + 0.03
            ):
                set_volume(device, original)
            if saved["muted_by_us"] and is_muted(device):
                _set(device, "mute", 0, ctypes.c_uint32)
            if saved["paused"]:
                media = now_playing()
                if media and media["bundle"] == saved["paused"] and not media["playing"]:
                    _media_command("play")
        finally:
            self._clear_state()

    def _write_state(self):
        saved = self._saved
        if not saved or saved["volume"] is None:
            return
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as file:
                json.dump({"uid": saved["uid"], "volume": saved["volume"]}, file)
        except OSError:
            pass

    @staticmethod
    def _clear_state():
        try:
            os.remove(STATE_PATH)
        except OSError:
            pass


def recover_stale_state(log=print):
    """If the app was killed while the volume was down, put the volume back."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as file:
            state = json.load(file)
        uid, original = state["uid"], float(state["volume"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        for device in _get_ids(_SYSTEM_OBJECT, "dev#"):
            if _get_string(device, "uid ") != uid:
                continue
            current = get_volume(device)
            if current is not None and current < original - VOLUME_TOLERANCE:
                set_volume(device, original)
                log(f"🔊 Restored output volume left down by a previous run ({original:.2f})")
            break
    finally:
        MediaDucker._clear_state()


def _status():
    device = default_output_device()
    if device is None:
        print("no output device")
        return
    volume = get_volume(device)
    print(
        f"output: {_get_string(device, 'lnam')!r} -> {device_kind(device)}, "
        f"volume {'no control' if volume is None else round(volume, 3)}, "
        f"muted {is_muted(device)}"
    )
    print("now playing:", now_playing(), "| media-control:", _media_control_path())
    print("apps playing audio:", apps_playing_audio() or "none")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Try the dictation media ducking on its own")
    parser.add_argument("--status", action="store_true", help="read-only: show what is playing")
    parser.add_argument("--test", action="store_true", help="duck, hold, then restore")
    parser.add_argument("--hold", type=float, default=3.0, help="seconds to stay ducked")
    parser.add_argument(
        "--as", dest="kind", choices=("speakers", "headphones"),
        help="with --test: behave as if the output were this kind of device",
    )
    args = parser.parse_args()
    _status()
    if args.test:
        if args.kind:
            device_kind = lambda device: args.kind  # noqa: E731
        ducker = MediaDucker()
        device = default_output_device()
        started = time.monotonic()
        ducker.set_recording(True)
        time.sleep(FADE_OUT_SECONDS + 0.6)
        print(f"ducked: volume {get_volume(device)}, now playing {now_playing()}")
        time.sleep(max(0.0, args.hold - (time.monotonic() - started)))
        ducker.set_recording(False)
        while not ducker.is_idle():
            time.sleep(0.05)
        time.sleep(0.4)
        print(f"after: volume {get_volume(device)}, now playing {now_playing()}")
