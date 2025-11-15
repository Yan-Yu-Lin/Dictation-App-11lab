#!/usr/bin/env python3
"""
Real-time dictation app using ElevenLabs Scribe v2 Realtime.
Press hotkey to start/stop recording, transcription streams in real-time.
"""

import asyncio
import base64
import os
import sys
import threading
from queue import Queue, Empty
from typing import Optional

import pyaudio
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

# Global state
event_loop = None  # Store reference to the event loop


class DictationApp:
    def __init__(self):
        self.is_recording = False
        self.audio_stream = None
        self.audio_interface = None
        self.connection = None
        self.last_partial_text = ""
        self.keyboard_controller = Controller()
        self.audio_queue = Queue()  # Thread-safe queue for audio chunks

        # Initialize ElevenLabs client
        api_key = os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            print("ERROR: ELEVENLABS_API_KEY not found in .env file")
            sys.exit(1)

        self.elevenlabs = ElevenLabs(api_key=api_key)

        # Initialize PyAudio
        self.audio_interface = pyaudio.PyAudio()

        print("Dictation App Ready!")
        print(f"Press Right Command (Hyper Key) + {TRIGGER_KEY.upper()} to start/stop recording")
        print("Transcription will stream in real-time as you speak\n")

    async def start_recording(self):
        """Start recording audio and connect to ElevenLabs"""
        if self.is_recording:
            return

        self.is_recording = True
        self.last_partial_text = ""
        print("\n🎙️  Recording started... Speak now!")

        # Connect to ElevenLabs Realtime API
        try:
            self.connection = await self.elevenlabs.speech_to_text.realtime.connect(
                RealtimeAudioOptions(
                    model_id="scribe_v2_realtime",
                    audio_format=AudioFormat.PCM_16000,
                    sample_rate=SAMPLE_RATE,
                    commit_strategy=CommitStrategy.MANUAL,
                    include_timestamps=False,
                )
            )

            # Set up event handlers
            self.connection.on(RealtimeEvents.SESSION_STARTED, self.on_session_started)
            self.connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self.on_partial_transcript)
            self.connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, self.on_committed_transcript)
            self.connection.on(RealtimeEvents.ERROR, self.on_error)
            self.connection.on(RealtimeEvents.CLOSE, self.on_close)

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

        self.is_recording = False
        print("\n🛑 Recording stopped. Finalizing transcription...")

        # Stop audio stream
        if self.audio_stream:
            self.audio_stream.stop_stream()
            self.audio_stream.close()
            self.audio_stream = None

        # Give the send task a moment to finish
        await asyncio.sleep(0.2)

        # Commit and close connection
        if self.connection:
            try:
                await self.connection.commit()
                # Give it a moment to receive the committed transcript
                await asyncio.sleep(0.5)
                await self.connection.close()
            except Exception as e:
                print(f"⚠️  Error closing connection: {e}")
            self.connection = None

        print("✅ Transcription complete!\n")

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

        # Calculate what's new since last update
        if new_text != self.last_partial_text:
            # Delete previous partial text by simulating backspaces
            if self.last_partial_text:
                num_backspaces = len(self.last_partial_text)
                for _ in range(num_backspaces):
                    self.keyboard_controller.press(Key.backspace)
                    self.keyboard_controller.release(Key.backspace)

            # Type the new partial text
            self.keyboard_controller.type(new_text)
            self.last_partial_text = new_text

            print(f"📝 Streaming: {new_text}")

    def on_committed_transcript(self, data):
        """Called when final transcript is committed"""
        final_text = data.get('text', '').strip()

        if final_text:
            # Replace partial text with final text
            if self.last_partial_text:
                num_backspaces = len(self.last_partial_text)
                for _ in range(num_backspaces):
                    self.keyboard_controller.press(Key.backspace)
                    self.keyboard_controller.release(Key.backspace)

            # Type final text
            self.keyboard_controller.type(final_text)
            print(f"\n✅ Final: {final_text}\n")
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


async def main():
    """Main event loop"""
    global app, event_loop

    # Store reference to the event loop
    event_loop = asyncio.get_running_loop()

    # Create app instance
    app = DictationApp()

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExited.")
