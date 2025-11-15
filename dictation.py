#!/usr/bin/env python3
"""
Real-time dictation app using ElevenLabs Scribe v2 Realtime.
Press hotkey to start/stop recording, transcription streams in real-time.
"""

import argparse
import asyncio
import base64
import os
import sys
import subprocess
import threading
from queue import Queue, Empty
from typing import Optional

import pyaudio
import pyperclip
from dotenv import load_dotenv
from elevenlabs import AudioFormat, CommitStrategy, ElevenLabs, RealtimeEvents, RealtimeAudioOptions
from pynput import keyboard
from pynput.keyboard import Controller, Key

# Load environment variables
load_dotenv()

# Configuration
# Using Right Command + D (for hyper key setup)
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
        import time
        time.sleep(0.05)

        # Restore old clipboard
        pyperclip.copy(old_clipboard)
    except Exception as e:
        # Fallback to typing if paste fails
        keyboard_controller = Controller()
        keyboard_controller.type(text)


class DictationApp:
    def __init__(self, mode='streaming'):
        self.is_recording = False
        self.audio_stream = None
        self.audio_interface = None
        self.connection = None
        self.last_partial_text = ""
        self.keyboard_controller = Controller()
        self.audio_queue = Queue()  # Thread-safe queue for audio chunks
        self.mode = mode  # 'streaming' or 'batch'
        self.session_id = 0  # Track session number to handle parallel cleanup

        # Initialize ElevenLabs client
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            print("ERROR: ELEVENLABS_API_KEY not found in .env file")
            sys.exit(1)

        self.elevenlabs = ElevenLabs(api_key=api_key)

        # Initialize PyAudio
        self.audio_interface = pyaudio.PyAudio()

        print("Dictation App Ready!")
        print(f"Mode: {mode.upper()}")
        if mode == 'streaming':
            print("Text will appear in real-time as you speak")
        else:
            print("Text will appear after you finish speaking")
        print(f"Press Right Command (Hyper Key) + {TRIGGER_KEY.upper()} to start/stop recording\n")

    async def start_recording(self):
        """Start recording audio and connect to ElevenLabs"""
        if self.is_recording:
            return

        self.is_recording = True
        self.last_partial_text = ""

        # Increment session ID for this new session
        self.session_id += 1
        current_session = self.session_id

        # Play start sound
        play_sound(SOUND_START)

        print("\n🎙️  Recording started... Speak now!")

        # Connect to ElevenLabs Realtime API
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

            # Set up event handlers
            new_connection.on(RealtimeEvents.SESSION_STARTED, self.on_session_started)
            new_connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self.on_partial_transcript)
            new_connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, self.on_committed_transcript)
            new_connection.on(RealtimeEvents.ERROR, self.on_error)
            new_connection.on(RealtimeEvents.CLOSE, self.on_close)

            # Only assign to self.connection after successfully creating it
            self.connection = new_connection

        except Exception as e:
            print(f"❌ Error connecting to ElevenLabs: {e}")
            self.is_recording = False
            return

        # Start audio stream
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

            # Start the audio sender task
            asyncio.create_task(self.send_audio_chunks())

        except Exception as e:
            print(f"❌ Error starting audio stream: {e}")
            if self.connection:
                await self.connection.close()
            self.is_recording = False

    async def send_audio_chunks(self):
        """Send audio chunks from the queue to ElevenLabs"""
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

    async def stop_recording(self):
        """Stop recording and commit the transcript"""
        if not self.is_recording:
            return

        # Immediately stop recording to allow new session to start
        self.is_recording = False

        # Play stop sound
        play_sound(SOUND_STOP)

        print("\n🛑 Recording stopped. Finalizing transcription...")

        # Capture references to current session's resources
        old_audio_stream = self.audio_stream
        old_connection = self.connection

        # Clear references immediately so new session can start
        self.audio_stream = None
        self.connection = None

        # Clean up old session asynchronously in background
        asyncio.create_task(self._cleanup_session(old_audio_stream, old_connection))

    async def _cleanup_session(self, audio_stream, connection):
        """Clean up a session's resources in the background"""
        try:
            # Stop audio stream
            if audio_stream:
                audio_stream.stop_stream()
                audio_stream.close()

            # Give the send task a moment to finish
            await asyncio.sleep(0.2)

            # Commit and close connection
            if connection:
                try:
                    await connection.commit()
                    # Give it a moment to receive the committed transcript
                    await asyncio.sleep(0.5)
                    await connection.close()
                except Exception as e:
                    print(f"⚠️  Error closing connection: {e}")

            print("✅ Transcription complete!\n")
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")

    def audio_callback(self, in_data, frame_count, time_info, status):
        """Callback for audio stream - put chunks in queue"""
        if self.is_recording:
            # Put audio data in queue for async processing
            self.audio_queue.put(in_data)

        return (in_data, pyaudio.paContinue)

    def on_session_started(self, data):
        """Called when WebSocket session starts"""
        print("🔌 Connected to ElevenLabs Scribe v2 Realtime")

    def on_partial_transcript(self, data):
        """Called when partial transcript is received - stream text in real-time"""
        new_text = data.get('text', '').strip()

        if not new_text:
            return

        # Only stream in real-time if in streaming mode
        if self.mode == 'streaming':
            # Only type the NEW part that was added (incremental update)
            if new_text != self.last_partial_text:
                # Check if new text starts with old text (append scenario)
                if new_text.startswith(self.last_partial_text):
                    # Only type the new characters added
                    new_chars = new_text[len(self.last_partial_text):]
                    if new_chars:
                        self.keyboard_controller.type(new_chars)
                else:
                    # Text changed completely, delete old and type new
                    if self.last_partial_text:
                        num_backspaces = len(self.last_partial_text)
                        for _ in range(num_backspaces):
                            self.keyboard_controller.press(Key.backspace)
                            self.keyboard_controller.release(Key.backspace)
                    self.keyboard_controller.type(new_text)

                self.last_partial_text = new_text
                print(f"📝 Streaming: {new_text}")
        else:
            # In batch mode, just update internal state and show in console
            self.last_partial_text = new_text
            print(f"📝 Processing: {new_text}")

    def on_committed_transcript(self, data):
        """Called when final transcript is committed"""
        final_text = data.get('text', '').strip()

        if final_text:
            if self.mode == 'streaming':
                # In streaming mode, replace partial text with final text by typing
                if self.last_partial_text and final_text != self.last_partial_text:
                    num_backspaces = len(self.last_partial_text)
                    for _ in range(num_backspaces):
                        self.keyboard_controller.press(Key.backspace)
                        self.keyboard_controller.release(Key.backspace)
                    # Type final text
                    self.keyboard_controller.type(final_text)
                elif not self.last_partial_text:
                    # No partial text, just type final
                    self.keyboard_controller.type(final_text)
                print(f"\n✅ Final: {final_text}\n")
            else:
                # In batch mode, paste everything at once using clipboard (works great!)
                paste_text(final_text)
                print(f"\n✅ Pasted: {final_text}\n")

            self.last_partial_text = ""

    def on_error(self, error):
        """Called when an error occurs"""
        print(f"❌ Error: {error}")

    def on_close(self):
        """Called when connection closes"""
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

# Track pressed keys for hotkey detection
pressed_keys = set()
right_cmd_pressed = False


def on_press(key):
    """Handle key press events"""
    global app, event_loop, pressed_keys, right_cmd_pressed

    # Track right command key (hyper key)
    # On macOS, right command is Key.cmd_r
    if hasattr(key, 'name') and key == keyboard.Key.cmd_r:
        right_cmd_pressed = True

    # Add key to pressed set
    pressed_keys.add(key)

    # Check if trigger key is pressed while right command is held
    if right_cmd_pressed:
        trigger_char = TRIGGER_KEY.lower()
        if hasattr(key, 'char') and key.char and key.char.lower() == trigger_char:
            # Trigger the hotkey action
            if app and event_loop:
                if not app.is_recording:
                    asyncio.run_coroutine_threadsafe(app.start_recording(), event_loop)
                else:
                    asyncio.run_coroutine_threadsafe(app.stop_recording(), event_loop)


def on_release(key):
    """Handle key release events"""
    global pressed_keys, right_cmd_pressed

    # Track right command release
    if hasattr(key, 'name') and key == keyboard.Key.cmd_r:
        right_cmd_pressed = False

    # Remove from pressed set
    if key in pressed_keys:
        pressed_keys.remove(key)


async def main(mode='streaming'):
    """Main event loop"""
    global app, event_loop

    # Store reference to the event loop
    event_loop = asyncio.get_running_loop()

    # Create app instance
    app = DictationApp(mode=mode)

    # Set up keyboard listener (using regular Listener to detect specific keys)
    listener = keyboard.Listener(
        on_press=on_press,
        on_release=on_release
    )
    listener.start()

    print("Listening for hotkey presses...")
    print("Press Ctrl+C to exit\n")

    # Keep the event loop running
    try:
        while True:
            await asyncio.sleep(0.1)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        if app.is_recording:
            await app.stop_recording()
        app.cleanup()
        listener.stop()


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Real-time dictation app using ElevenLabs Scribe v2 Realtime',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                    # Use default batch mode
  %(prog)s --mode batch       # Wait until end to paste text (default)
  %(prog)s --mode streaming   # Real-time text streaming
        """
    )
    parser.add_argument(
        '--mode',
        choices=['streaming', 'batch'],
        default='batch',
        help='Transcription mode: streaming (real-time updates) or batch (paste at end, default)'
    )

    args = parser.parse_args()

    try:
        asyncio.run(main(mode=args.mode))
    except KeyboardInterrupt:
        print("\nExited.")
