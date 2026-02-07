#!/usr/bin/env python3
"""
Dictation app using ElevenLabs Scribe v2 Realtime.
Press hotkey to start/stop recording, text is pasted when you finish speaking.
"""

import argparse
import asyncio
import base64
import os
import sys
import subprocess
import threading
import time
from queue import Queue, Empty, Full
from typing import Optional

import opencc
import pyaudio
import pyperclip
from dotenv import load_dotenv
from elevenlabs import (
    AudioFormat,
    CommitStrategy,
    ElevenLabs,
    RealtimeEvents,
    RealtimeAudioOptions,
)
from funasr import AutoModel
from pynput.keyboard import Controller, Key

# QuickMacHotKey for global hotkey interception (blocks keypress from reaching other apps)
from quickmachotkey import quickHotKey, mask
from quickmachotkey.constants import (
    kVK_ANSI_D,
    kVK_RightShift,
    cmdKey,
    controlKey,
    optionKey,
)

# PyObjC imports for NSApplication
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSEventMaskFlagsChanged,
    NSEventModifierFlagShift,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorTransient,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRect, NSObject
from PyObjCTools import AppHelper

# Load environment variables
load_dotenv()

# Configuration
# Using Cmd+Option+Control+D (hyper key + D)
TRIGGER_KEY = "d"  # The key to press with hyper key
SAMPLE_RATE = 16000  # 16kHz recommended by ElevenLabs
CHUNK_SIZE = 4096  # Audio chunk size (0.25 seconds at 16kHz)
AUDIO_FORMAT = pyaudio.paInt16  # 16-bit PCM
CHANNELS = 1  # Mono
MAX_AUDIO_QUEUE_CHUNKS = 120  # ~30s of buffered audio at CHUNK_SIZE=4096
CONNECT_TIMEOUT_SECONDS = 8.0
FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 2.5
LOCAL_PUNC_MODEL_ID = "ct-punc"

# Sound effects (macOS system sounds)
SOUND_START = "/System/Library/Sounds/Pop.aiff"  # Sound when recording starts
SOUND_STOP = "/System/Library/Sounds/Tink.aiff"  # Sound when recording stops
SOUND_START_VOLUME = 0.25
SOUND_STOP_VOLUME = 0.25

# Global state
event_loop = None  # Store reference to the event loop
async_loop_ready = threading.Event()  # Signals when async loop is initialized
status_overlay = None


def play_sound(sound_path, volume=0.25):
    """Play a system sound asynchronously (non-blocking)"""
    try:
        safe_volume = max(0.0, min(1.0, float(volume)))
        subprocess.Popen(
            ["afplay", "-v", str(safe_volume), sound_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # Silently fail if sound can't be played


def set_overlay_recording():
    global status_overlay
    if status_overlay:
        AppHelper.callAfter(status_overlay.show_recording)


def set_overlay_finalizing():
    global status_overlay
    if status_overlay:
        AppHelper.callAfter(status_overlay.show_finalizing)


def hide_overlay():
    global status_overlay
    if status_overlay:
        AppHelper.callAfter(status_overlay.hide)


class StatusOverlay(NSObject):
    """Small always-on-top circular indicator for dictation state."""

    WIDTH = 30
    HEIGHT = 30
    TOP_MARGIN = 18
    DOT_SIZE = 10

    def init(self):
        self = super().init()
        if self is None:
            return None

        self.panel = None
        self.dot_view = None
        self._create_panel()
        return self

    def _screen_rect(self):
        point = NSEvent.mouseLocation()
        screen = None
        for candidate in NSScreen.screens():
            frame = candidate.frame()
            min_x = frame.origin.x
            min_y = frame.origin.y
            max_x = frame.origin.x + frame.size.width
            max_y = frame.origin.y + frame.size.height
            if min_x <= point.x <= max_x and min_y <= point.y <= max_y:
                screen = candidate
                break

        if screen is None:
            screen = NSScreen.mainScreen()

        if screen is None:
            return NSMakeRect(40, 40, self.WIDTH, self.HEIGHT)
        frame = screen.visibleFrame()
        x = frame.origin.x + (frame.size.width - self.WIDTH) / 2
        y = frame.origin.y + frame.size.height - self.HEIGHT - self.TOP_MARGIN
        return NSMakeRect(x, y, self.WIDTH, self.HEIGHT)

    def _create_panel(self):
        frame = self._screen_rect()
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setFloatingPanel_(True)
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorTransient
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorIgnoresCycle
        )
        self.panel.setOpaque_(False)
        self.panel.setHasShadow_(True)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setIgnoresMouseEvents_(True)
        self.panel.setHidesOnDeactivate_(False)

        content = self.panel.contentView()
        content.setWantsLayer_(True)
        layer = content.layer()
        layer.setCornerRadius_(self.WIDTH / 2)
        layer.setMasksToBounds_(True)
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.02, 0.02, 0.03, 0.96
            ).CGColor()
        )

        dot_x = (self.WIDTH - self.DOT_SIZE) / 2
        dot_y = (self.HEIGHT - self.DOT_SIZE) / 2
        self.dot_view = NSView.alloc().initWithFrame_(
            NSMakeRect(dot_x, dot_y, self.DOT_SIZE, self.DOT_SIZE)
        )
        self.dot_view.setWantsLayer_(True)
        dot_layer = self.dot_view.layer()
        dot_layer.setCornerRadius_(self.DOT_SIZE / 2)
        dot_layer.setBackgroundColor_(NSColor.systemRedColor().CGColor())
        content.addSubview_(self.dot_view)

        self.hide()

    def _set_dot_color(self, color):
        if not self.dot_view:
            return
        dot_layer = self.dot_view.layer()
        if dot_layer:
            dot_layer.setBackgroundColor_(color.CGColor())

    def show_recording(self):
        if not self.panel:
            return
        self._set_dot_color(NSColor.systemRedColor())
        self.panel.setFrame_display_(self._screen_rect(), True)
        self.panel.orderFrontRegardless()

    def show_finalizing(self):
        if not self.panel:
            return
        self._set_dot_color(NSColor.systemOrangeColor())
        self.panel.setFrame_display_(self._screen_rect(), True)
        self.panel.orderFrontRegardless()

    def hide(self):
        if self.panel:
            self.panel.orderOut_(None)


def paste_text(text):
    """Paste text using clipboard (much faster than typing)"""
    try:
        # Save current clipboard
        old_clipboard = pyperclip.paste()

        # Copy text to clipboard
        pyperclip.copy(text)

        # Simulate Cmd+V to paste
        keyboard_controller = Controller()
        keyboard_controller.press(Key.cmd)
        keyboard_controller.press("v")
        keyboard_controller.release("v")
        keyboard_controller.release(Key.cmd)

        # Delay before restoring clipboard (gives time for paste and clipboard managers)
        time.sleep(0.6)

        # Restore old clipboard
        pyperclip.copy(old_clipboard)
    except Exception as e:
        # Fallback to typing if paste fails
        keyboard_controller = Controller()
        keyboard_controller.type(text)


def contains_chinese(text: str) -> bool:
    """Check if text contains Chinese characters."""
    for char in text:
        if "\u4e00" <= char <= "\u9fff":  # CJK Unified Ideographs
            return True
    return False


class DictationApp:
    def __init__(self, chinese="tw"):
        self.is_recording = False
        self.audio_stream = None
        self.audio_interface = None
        self.connection = None
        self.last_partial_text = ""
        self.audio_queue = None  # Will be created per session
        self.session_id = 0  # Track session number to handle parallel cleanup
        self.current_sender_task = None  # Track the current send_audio_chunks task
        self.session_lock = asyncio.Lock()  # Serialize start/stop
        self.active_session_id: Optional[int] = (
            None  # Identify which session events belong to
        )
        self.cleanup_task: Optional[asyncio.Task] = None
        self.commit_events: dict[int, asyncio.Event] = {}
        self.last_committed_text_by_session: dict[int, tuple[str, float]] = {}
        self.paste_lock = asyncio.Lock()

        # Initialize Chinese character converter
        # s2t: Simplified to Traditional, t2s: Traditional to Simplified
        self.chinese_variant = chinese
        if chinese == "tw":
            self.chinese_converter = opencc.OpenCC("s2t")
        else:
            self.chinese_converter = opencc.OpenCC("t2s")

        # Initialize ElevenLabs client
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
        if not elevenlabs_key:
            print("ERROR: ELEVENLABS_API_KEY not found in .env file")
            sys.exit(1)

        self.elevenlabs = ElevenLabs(api_key=elevenlabs_key)

        # Initialize local punctuation model at startup (blocking by design)
        self.local_punc_model = self._load_local_punctuation_model()

        print(f"ElevenLabs API Key: ...{elevenlabs_key[-4:]}")

        # Initialize PyAudio
        self.audio_interface = pyaudio.PyAudio()

        print("Dictation App Ready!")
        print(
            f"Chinese output: {'Traditional (TW)' if chinese == 'tw' else 'Simplified (CN)'}"
        )
        print(f"Press Cmd+Option+Control+{TRIGGER_KEY.upper()} to start/stop recording")
        print("(Or press your Hyper Key + D if you have it configured)\n")

    def _load_local_punctuation_model(self):
        """Load local punctuation model once so it is ready for every Chinese transcript."""
        try:
            print(
                f"Loading local punctuation model '{LOCAL_PUNC_MODEL_ID}'... "
                "(first run may download model files)"
            )
            model = AutoModel(
                model=LOCAL_PUNC_MODEL_ID,
                trust_remote_code=False,
                disable_update=True,
                device="cpu",
            )
            print("✅ Local punctuation model loaded")
            return model
        except Exception as e:
            print(
                f"ERROR: Failed to load local punctuation model '{LOCAL_PUNC_MODEL_ID}': {e}"
            )
            sys.exit(1)

    @staticmethod
    def _extract_punctuation_output(result, fallback_text: str) -> str:
        """Extract punctuated text from FunASR output payload."""
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                text = first.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

        if isinstance(result, str) and result.strip():
            return result.strip()

        return fallback_text

    def add_local_chinese_punctuation(self, text: str) -> str:
        """Use local CT-Punc model to add punctuation."""
        if not text.strip():
            return text

        started = time.perf_counter()
        try:
            result = self.local_punc_model.generate(input=text, disable_pbar=True)
            punctuated = self._extract_punctuation_output(result, text)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            print(f"✍️  Local Chinese punctuation added ({elapsed_ms}ms)")
            return punctuated
        except Exception as e:
            print(f"⚠️  Local punctuation error, using original text: {e}")
            return text

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

            # Create a NEW queue for this session (isolates from previous sessions)
            self.audio_queue = Queue(maxsize=MAX_AUDIO_QUEUE_CHUNKS)

        # Start audio stream first to avoid dropping the beginning while websocket connects
        try:
            if not self.audio_interface:
                raise RuntimeError("Audio interface is not initialized")

            self.audio_stream = self.audio_interface.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                stream_callback=self.audio_callback,
            )
            self.audio_stream.start_stream()

            # Only play start sound after mic is actually active
            play_sound(SOUND_START, SOUND_START_VOLUME)
            set_overlay_recording()
            print("\n🎙️  Recording started. Listening now...")
            print("🔄 Connecting to ElevenLabs realtime...")

        except Exception as e:
            print(f"❌ Error starting audio stream: {e}")
            hide_overlay()
            async with self.session_lock:
                if self.active_session_id == current_session:
                    self.is_recording = False
                    self.active_session_id = None
                    self.audio_stream = None
                    self.audio_queue = None
                self.commit_events.pop(current_session, None)
            return

        # Connect to ElevenLabs Realtime API (outside lock to keep hotkey responsive)
        try:
            realtime_client = getattr(self.elevenlabs.speech_to_text, "realtime", None)
            if realtime_client is None:
                raise RuntimeError(
                    "Realtime client unavailable in current ElevenLabs SDK"
                )

            new_connection = await asyncio.wait_for(
                realtime_client.connect(
                    RealtimeAudioOptions(
                        model_id="scribe_v2_realtime",
                        audio_format=AudioFormat.PCM_16000,
                        sample_rate=SAMPLE_RATE,
                        commit_strategy=CommitStrategy.MANUAL,
                        include_timestamps=False,
                    )
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )

            # If a stop happened during connect, drop this connection
            if (not self.is_recording) or (self.active_session_id != current_session):
                await new_connection.close()
                return

            # Set up session-scoped event handlers to prevent stale callbacks crossing sessions
            new_connection.on(
                RealtimeEvents.SESSION_STARTED,
                lambda data, sid=current_session: self.on_session_started(data, sid),
            )
            new_connection.on(
                RealtimeEvents.PARTIAL_TRANSCRIPT,
                lambda data, sid=current_session: self.on_partial_transcript(data, sid),
            )
            new_connection.on(
                RealtimeEvents.COMMITTED_TRANSCRIPT,
                lambda data, sid=current_session: self.on_committed_transcript(
                    data, sid
                ),
            )
            new_connection.on(
                RealtimeEvents.ERROR,
                lambda error, sid=current_session: self.on_error(error, sid),
            )
            new_connection.on(
                RealtimeEvents.CLOSE, lambda *_, sid=current_session: self.on_close(sid)
            )

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
            hide_overlay()
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            self.audio_stream = None
            self.audio_queue = None
            self.current_sender_task = None
            self.connection = None
            if self.active_session_id == current_session:
                self.is_recording = False
                self.active_session_id = None
            self.commit_events.pop(current_session, None)
            return
        except Exception as e:
            print(f"❌ Error connecting to ElevenLabs: {e}")
            hide_overlay()
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            self.audio_stream = None
            self.audio_queue = None
            self.current_sender_task = None
            self.connection = None
            if self.active_session_id == current_session:
                self.is_recording = False
                self.active_session_id = None
            self.commit_events.pop(current_session, None)

    async def send_audio_chunks(self, connection, audio_queue: Queue, session_id: int):
        """Send audio chunks from the queue to ElevenLabs for one session."""
        try:
            while self.is_recording:
                try:
                    # Get audio chunk from queue (non-blocking with timeout)
                    try:
                        audio_data = audio_queue.get(timeout=0.01)
                    except Empty:
                        await asyncio.sleep(0.01)
                        continue

                    # Convert audio to base64
                    audio_base64 = base64.b64encode(audio_data).decode("utf-8")

                    # Send to ElevenLabs
                    await connection.send(
                        {"audio_base_64": audio_base64, "sample_rate": SAMPLE_RATE}
                    )

                except Exception as e:
                    print(f"⚠️  Error sending audio (session {session_id}): {e}")
                    await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            # Task was cancelled, clean exit
            pass

    async def stop_recording(self):
        """Stop recording and commit the transcript"""
        async with self.session_lock:
            if not self.is_recording:
                return

            current_session = self.active_session_id or self.session_id

            # Immediately stop recording to allow new session to start
            self.is_recording = False

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

            # CRITICAL: Immediately stop the audio stream to prevent callback pollution
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()

            # Capture references to current session's resources
            old_connection = self.connection
            old_audio_queue = self.audio_queue

            # Clear references immediately so new session can start
            self.audio_stream = None
            self.connection = None
            self.audio_queue = None
            self.current_sender_task = None

            # Clean up old session asynchronously in background and remember task
            self.cleanup_task = asyncio.create_task(
                self._cleanup_session(old_connection, old_audio_queue, current_session)
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

        if flushed > 0:
            print(
                f"📤 Flushed {flushed} buffered chunks (session {session_to_cleanup})"
            )

    async def _cleanup_session(
        self,
        connection,
        audio_queue: Optional[Queue],
        session_to_cleanup: int,
    ):
        """Clean up a session's resources in the background"""
        try:
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
            hide_overlay()
            # Clear active session only if this cleanup belongs to the active one
            if self.active_session_id == session_to_cleanup:
                self.active_session_id = None
            # Reset reference to this cleanup task
            self.cleanup_task = None

    async def _process_final_transcript(self, text: str, session_id: int):
        """Process final transcript: convert characters, add punctuation, paste."""
        async with self.paste_lock:
            # Step 1: OpenCC conversion (sync, fast)
            converted_text = self.chinese_converter.convert(text)

            # Step 2: Add punctuation if Chinese (run in executor to keep event loop responsive)
            if contains_chinese(converted_text):
                loop = asyncio.get_running_loop()
                converted_text = await loop.run_in_executor(
                    None, self.add_local_chinese_punctuation, converted_text
                )

            # Step 3: Paste
            paste_text(converted_text)
            print(f"\n✅ Pasted: {converted_text}\n")
            self.last_partial_text = ""

    def audio_callback(self, in_data, frame_count, time_info, status):
        """Callback for audio stream - put chunks in queue"""
        # Capture reference locally to prevent race condition
        queue = self.audio_queue
        if self.is_recording and queue is not None:
            try:
                queue.put_nowait(in_data)
            except Full:
                # Keep newest audio if buffer is full
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

        # Update internal state and show progress in console
        self.last_partial_text = new_text
        print(f"📝 Processing: {new_text}")

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

        # Deduplicate rapid repeated committed events for the same segment.
        # Keep only near-identical repeats, allow same text later in long sessions.
        now = time.monotonic()
        last_committed = self.last_committed_text_by_session.get(session_id)
        if last_committed:
            last_text, last_time = last_committed
            if last_text == final_text and (now - last_time) < 1.5:
                return
        self.last_committed_text_by_session[session_id] = (final_text, now)

        if event_loop:
            # Schedule async processing (OpenCC + local Chinese punctuation + paste)
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

    def cleanup(self):
        """Clean up resources"""
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
        if self.audio_interface:
            self.audio_interface.terminate()


# Global app instance
app = None
right_shift_ptt_monitor = None
right_shift_ptt_enabled = True


class RightShiftPTTMonitor:
    """Global right-shift push-to-talk monitor."""

    def __init__(self):
        self.monitor_token = None
        self.right_shift_down = False
        self.started_session = False

    def start(self):
        if self.monitor_token is not None:
            return
        self.monitor_token = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskFlagsChanged,
            self._handle_flags_changed,
        )
        print("Right Shift push-to-talk enabled (hold to record)")

    def stop(self):
        if self.monitor_token is not None:
            NSEvent.removeMonitor_(self.monitor_token)
            self.monitor_token = None
        self.right_shift_down = False
        self.started_session = False

    async def _stop_when_possible(self):
        """Stop recording after a right-shift release, even if start is still in flight."""
        global app
        for _ in range(25):
            if self.right_shift_down:
                return
            if app and app.is_recording:
                await app.stop_recording()
                return
            await asyncio.sleep(0.02)

    def _handle_flags_changed(self, event):
        """Handle global modifier changes and detect right-shift hold/release."""
        global app, event_loop

        try:
            if event.keyCode() != kVK_RightShift:
                return

            # NSEvent exposes Shift as a combined modifier flag.
            # keyCode() above scopes this transition specifically to Right Shift.
            is_down = bool(event.modifierFlags() & NSEventModifierFlagShift)
            if is_down == self.right_shift_down:
                return

            self.right_shift_down = is_down

            if not app or not event_loop:
                return

            if is_down:
                if not app.is_recording:
                    self.started_session = True
                    asyncio.run_coroutine_threadsafe(app.start_recording(), event_loop)
                else:
                    self.started_session = False
            else:
                if self.started_session:
                    self.started_session = False
                    asyncio.run_coroutine_threadsafe(
                        self._stop_when_possible(),
                        event_loop,
                    )
        except Exception as e:
            print(f"⚠️  Right Shift PTT monitor error: {e}")


# Global hotkey handler using QuickMacHotKey
# This automatically intercepts and blocks the hotkey from reaching other apps (like terminal)
@quickHotKey(virtualKey=kVK_ANSI_D, modifierMask=mask(cmdKey, controlKey, optionKey))
def handle_hotkey():
    """
    Handle the global hotkey Cmd+Option+Control+D.
    QuickMacHotKey automatically consumes the keypress, preventing it from reaching other apps.
    """
    global app, event_loop

    if app and event_loop:
        if not app.is_recording:
            asyncio.run_coroutine_threadsafe(app.start_recording(), event_loop)
        else:
            asyncio.run_coroutine_threadsafe(app.stop_recording(), event_loop)


class AppDelegate(NSObject):
    """Simple app delegate for NSApplication."""

    def applicationDidFinishLaunching_(self, notification):
        """Set up when app finishes launching."""
        global right_shift_ptt_monitor, right_shift_ptt_enabled, status_overlay

        status_overlay = StatusOverlay.alloc().init()

        if right_shift_ptt_enabled:
            right_shift_ptt_monitor = RightShiftPTTMonitor()
            right_shift_ptt_monitor.start()

        print("Hotkey monitor started. Press Cmd+Option+Control+D to toggle recording.")
        if right_shift_ptt_enabled:
            print("Hold Right Shift for push-to-talk.")
        print("(QuickMacHotKey will intercept the keypress - terminal won't see it)")
        print("Press Ctrl+C to exit.\n")


def setup_async_loop(chinese):
    """Set up the async event loop in a separate thread."""
    global app, event_loop

    # Create new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    event_loop = loop

    # Create app instance
    app = DictationApp(chinese=chinese)

    # Signal that initialization is complete
    async_loop_ready.set()

    # Run the event loop forever
    loop.run_forever()


def start_app(chinese="tw", enable_right_shift_ptt=True):
    """Start the application with NSApplication event loop."""
    global right_shift_ptt_enabled, right_shift_ptt_monitor

    right_shift_ptt_enabled = enable_right_shift_ptt
    right_shift_ptt_monitor = None

    # Start asyncio event loop in a separate thread
    async_thread = threading.Thread(
        target=setup_async_loop, args=(chinese,), daemon=True
    )
    async_thread.start()

    # Wait for the async thread to initialize
    async_loop_ready.wait()

    # Create the NSApplication
    ns_app = NSApplication.sharedApplication()

    # Create and set the delegate
    delegate = AppDelegate.alloc().init()
    ns_app.setDelegate_(delegate)

    # Run the NSApplication event loop (blocks until app quits)
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        if app and app.is_recording:
            # Schedule cleanup on the async loop
            if event_loop:
                asyncio.run_coroutine_threadsafe(app.stop_recording(), event_loop)
                time.sleep(1)  # Give time for cleanup
    finally:
        global status_overlay
        if right_shift_ptt_monitor:
            right_shift_ptt_monitor.stop()
        hide_overlay()
        status_overlay = None
        if app:
            app.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dictation app using ElevenLabs Scribe v2 Realtime"
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
        help="Disable hold-to-talk on Right Shift (Cmd+Option+Control+D toggle stays enabled)",
    )
    args = parser.parse_args()
    start_app(
        chinese=args.chinese,
        enable_right_shift_ptt=not args.disable_right_shift_ptt,
    )
