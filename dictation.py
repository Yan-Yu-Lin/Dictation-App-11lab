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
from queue import Queue, Empty
from typing import Optional

import opencc
import pyaudio
import pyperclip
from dotenv import load_dotenv
from elevenlabs import AudioFormat, CommitStrategy, ElevenLabs, RealtimeEvents, RealtimeAudioOptions
from pynput.keyboard import Controller, Key

# QuickMacHotKey for global hotkey interception (blocks keypress from reaching other apps)
from quickmachotkey import quickHotKey, mask
from quickmachotkey.constants import kVK_ANSI_D, cmdKey, controlKey, optionKey

# PyObjC imports for NSApplication
from AppKit import NSApplication
from Foundation import NSObject
from PyObjCTools import AppHelper

# Load environment variables
load_dotenv()

# Configuration
# Using Cmd+Option+Control+D (hyper key + D)
TRIGGER_KEY = 'd'  # The key to press with hyper key
SAMPLE_RATE = 16000  # 16kHz recommended by ElevenLabs
CHUNK_SIZE = 4096  # Audio chunk size (0.25 seconds at 16kHz)
AUDIO_FORMAT = pyaudio.paInt16  # 16-bit PCM
CHANNELS = 1  # Mono

# Sound effects (macOS system sounds)
SOUND_START = "/System/Library/Sounds/Hero.aiff"  # Sound when recording starts
SOUND_STOP = "/System/Library/Sounds/Glass.aiff"  # Sound when recording stops

# Global state
event_loop = None  # Store reference to the event loop
async_loop_ready = threading.Event()  # Signals when async loop is initialized


def play_sound(sound_path):
    """Play a system sound asynchronously (non-blocking)"""
    try:
        subprocess.Popen(
            ["afplay", sound_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass  # Silently fail if sound can't be played


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
        keyboard_controller.press('v')
        keyboard_controller.release('v')
        keyboard_controller.release(Key.cmd)

        # Small delay to ensure paste completes
        time.sleep(0.2)

        # Restore old clipboard
        pyperclip.copy(old_clipboard)
    except Exception as e:
        # Fallback to typing if paste fails
        keyboard_controller = Controller()
        keyboard_controller.type(text)


class DictationApp:
    def __init__(self, chinese='tw'):
        self.is_recording = False
        self.audio_stream = None
        self.audio_interface = None
        self.connection = None
        self.last_partial_text = ""
        self.audio_queue = None  # Will be created per session
        self.session_id = 0  # Track session number to handle parallel cleanup
        self.current_sender_task = None  # Track the current send_audio_chunks task
        self.session_lock = asyncio.Lock()  # Serialize start/stop
        self.active_session_id: Optional[int] = None  # Identify which session events belong to
        self.cleanup_task: Optional[asyncio.Task] = None

        # Initialize Chinese character converter
        # s2t: Simplified to Traditional, t2s: Traditional to Simplified
        self.chinese_variant = chinese
        if chinese == 'tw':
            self.chinese_converter = opencc.OpenCC('s2t')
        else:
            self.chinese_converter = opencc.OpenCC('t2s')

        # Initialize ElevenLabs client
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            print("ERROR: ELEVENLABS_API_KEY not found in .env file")
            sys.exit(1)

        self.elevenlabs = ElevenLabs(api_key=api_key)

        # Initialize PyAudio
        self.audio_interface = pyaudio.PyAudio()

        print("Dictation App Ready!")
        print(f"Chinese output: {'Traditional (TW)' if chinese == 'tw' else 'Simplified (CN)'}")
        print(f"Press Cmd+Option+Control+{TRIGGER_KEY.upper()} to start/stop recording")
        print("(Or press your Hyper Key + D if you have it configured)\n")

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

            # Create a NEW queue for this session (isolates from previous sessions)
            self.audio_queue = Queue()

            # Play start sound
            play_sound(SOUND_START)

            print("\n🎙️  Recording started... Speak now!")

        # Connect to ElevenLabs Realtime API (outside lock to keep hotkey responsive)
        try:
            new_connection = await self.elevenlabs.speech_to_text.realtime.connect(
                RealtimeAudioOptions(
                    model_id="scribe_v2_realtime",
                    audio_format=AudioFormat.PCM_16000,
                    sample_rate=SAMPLE_RATE,
                    commit_strategy=CommitStrategy.MANUAL,
                    include_timestamps=False,
                )
            )

            # If a stop happened during connect, drop this connection
            if (not self.is_recording) or (self.active_session_id != current_session):
                await new_connection.close()
                return

            # Set up event handlers
            new_connection.on(RealtimeEvents.SESSION_STARTED, self.on_session_started)
            new_connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self.on_partial_transcript)
            new_connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, self.on_committed_transcript)
            new_connection.on(RealtimeEvents.ERROR, self.on_error)
            new_connection.on(RealtimeEvents.CLOSE, self.on_close)

            # Only assign to self.connection after successfully creating it
            # Verify session is still current (protect against stale starts)
            if self.active_session_id != current_session:
                await new_connection.close()
                return

            self.connection = new_connection

        except Exception as e:
            print(f"❌ Error connecting to ElevenLabs: {e}")
            self.is_recording = False
            self.active_session_id = None
            return

        # Start audio stream (outside the lock)
        try:
            self.audio_stream = self.audio_interface.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                stream_callback=self.audio_callback
            )
            self.audio_stream.start_stream()

            # Start the audio sender task and keep reference
            self.current_sender_task = asyncio.create_task(self.send_audio_chunks())

        except Exception as e:
            print(f"❌ Error starting audio stream: {e}")
            if self.connection:
                await self.connection.close()
            self.is_recording = False
            self.active_session_id = None

    async def send_audio_chunks(self):
        """Send audio chunks from the queue to ElevenLabs"""
        try:
            while self.is_recording:
                try:
                    # Get audio chunk from queue (non-blocking with timeout)
                    try:
                        audio_data = self.audio_queue.get(timeout=0.01)
                    except Empty:
                        await asyncio.sleep(0.01)
                        continue

                    # Convert audio to base64
                    audio_base64 = base64.b64encode(audio_data).decode('utf-8')

                    # Send to ElevenLabs
                    if self.connection:
                        await self.connection.send({
                            "audio_base_64": audio_base64,
                            "sample_rate": SAMPLE_RATE
                        })

                except Exception as e:
                    print(f"⚠️  Error sending audio: {e}")
                    await asyncio.sleep(0.01)

        except asyncio.CancelledError:
            # Task was cancelled, clean exit
            pass

    async def stop_recording(self):
        """Stop recording and commit the transcript"""
        async with self.session_lock:
            if not self.is_recording:
                return

            current_session = self.active_session_id

            # Immediately stop recording to allow new session to start
            self.is_recording = False

            # Play stop sound
            play_sound(SOUND_STOP)

            print("\n🛑 Recording stopped. Finalizing transcription...")

            # CRITICAL: Cancel the sender task immediately to stop processing
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
            old_audio_stream = self.audio_stream  # Already stopped, but keep for cleanup
            old_connection = self.connection

            # Clear references immediately so new session can start
            self.audio_stream = None
            self.connection = None
            self.audio_queue = None
            self.current_sender_task = None

            # Clean up old session asynchronously in background and remember task
            self.cleanup_task = asyncio.create_task(
                self._cleanup_session(old_audio_stream, old_connection, current_session)
            )

    async def _cleanup_session(self, audio_stream, connection, session_to_cleanup: Optional[int]):
        """Clean up a session's resources in the background"""
        try:
            # Audio stream is already stopped and sender task is already cancelled in stop_recording()
            # No need to wait - proceed directly to commit

            # Commit and close connection
            if connection:
                try:
                    await connection.commit()
                    # Give it a moment to receive the committed transcript
                    await asyncio.sleep(1.0)
                    await connection.close()
                except Exception as e:
                    print(f"⚠️  Error closing connection: {e}")

            print("✅ Transcription complete!\n")
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")
        finally:
            # Clear active session only if this cleanup belongs to the active one
            if self.active_session_id == session_to_cleanup:
                self.active_session_id = None
            # Reset reference to this cleanup task
            self.cleanup_task = None

    def audio_callback(self, in_data, frame_count, time_info, status):
        """Callback for audio stream - put chunks in queue"""
        # Capture reference locally to prevent race condition
        queue = self.audio_queue
        if self.is_recording and queue is not None:
            queue.put(in_data)

        return (in_data, pyaudio.paContinue)

    def on_session_started(self, data):
        """Called when WebSocket session starts"""
        if self.active_session_id is None:
            return
        print("🔌 Connected to ElevenLabs Scribe v2 Realtime")

    def on_partial_transcript(self, data):
        """Called when partial transcript is received"""
        if self.active_session_id is None:
            return
        new_text = data.get('text', '').strip()

        if not new_text:
            return

        # Update internal state and show progress in console
        self.last_partial_text = new_text
        print(f"📝 Processing: {new_text}")

    def on_committed_transcript(self, data):
        """Called when final transcript is committed"""
        if self.active_session_id is None:
            return
        final_text = data.get('text', '').strip()

        if final_text:
            # Convert Chinese characters if needed
            converted_text = self.chinese_converter.convert(final_text)
            paste_text(converted_text)
            print(f"\n✅ Pasted: {converted_text}\n")
            self.last_partial_text = ""

    def on_error(self, error):
        """Called when an error occurs"""
        if self.active_session_id is None:
            return
        print(f"❌ Error: {error}")

    def on_close(self):
        """Called when connection closes"""
        if self.active_session_id is None:
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


# Global hotkey handler using QuickMacHotKey
# This automatically intercepts and blocks the hotkey from reaching other apps (like terminal)
@quickHotKey(
    virtualKey=kVK_ANSI_D,
    modifierMask=mask(cmdKey, controlKey, optionKey)
)
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
        print("Hotkey monitor started. Press Cmd+Option+Control+D to toggle recording.")
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


def start_app(chinese='tw'):
    """Start the application with NSApplication event loop."""
    # Start asyncio event loop in a separate thread
    async_thread = threading.Thread(target=setup_async_loop, args=(chinese,), daemon=True)
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
        if app:
            app.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Dictation app using ElevenLabs Scribe v2 Realtime'
    )
    parser.add_argument(
        '--chinese',
        choices=['tw', 'cn'],
        default='tw',
        help='Chinese character variant: tw (Traditional, default) or cn (Simplified)'
    )
    args = parser.parse_args()
    start_app(chinese=args.chinese)
