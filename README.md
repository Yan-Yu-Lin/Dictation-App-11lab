# Dictation App - ElevenLabs Scribe v2 Realtime

A real-time dictation tool that transcribes your speech as you speak using ElevenLabs Scribe v2 Realtime API.

## Features

- **Real-time streaming transcription**: Text appears as you speak, not after you finish
- **Hotkey control**: Press F9 to start/stop recording
- **Auto-paste**: Transcribed text is automatically typed at your cursor position
- **Live updates**: Partial transcripts stream in real-time, then finalize when you stop recording
- **Low latency**: ~150ms transcription delay using Scribe v2 Realtime

## How It Works

1. Press **F9** to start recording
2. Speak naturally - you'll see partial transcripts streaming in the console
3. The text will be automatically typed at your cursor position in real-time
4. Press **F9** again to stop recording and finalize the transcription
5. The final, polished transcript replaces the partial text

## Installation

### Prerequisites

- Python 3.9+
- ElevenLabs API key ([get it here](https://elevenlabs.io/app/settings/api-keys))
- Microphone access
- **macOS**: PortAudio is required for PyAudio
  ```bash
  brew install portaudio
  ```

### Setup

1. Clone or download this project

2. Install dependencies using UV:
   ```bash
   uv sync
   ```

3. Create a `.env` file with your API key:
   ```bash
   cp .env.example .env
   # Edit .env and add your ELEVENLABS_API_KEY
   ```

## Usage

### Transcription Modes

The app supports two modes:

1. **Streaming mode (default)**: Text appears in real-time as you speak, with live updates
2. **Batch mode**: Text appears only after you finish recording

Run the dictation app:

```bash
# Default streaming mode
uv run dictation.py

# Explicit streaming mode
uv run dictation.py --mode streaming

# Batch mode (paste at end)
uv run dictation.py --mode batch
```

Or activate the virtual environment first:

```bash
source .venv/bin/activate  # On macOS/Linux
# or
.venv\Scripts\activate     # On Windows

python dictation.py --mode streaming  # or --mode batch
```

### Controls

- **Right Command (Hyper Key) + D**: Toggle recording on/off
- **Ctrl+C**: Exit the application

### Sound Feedback

- **Hero sound**: Recording started
- **Glass sound**: Recording stopped

### Tips

- **Speak clearly** for best accuracy
- **Use in any text field**: The app types wherever your cursor is focused
- **Wait for finalization**: Let the recording stop fully before editing
- **Check your mic**: Make sure your microphone is working and has permissions

## Configuration

You can customize the hotkey by editing `dictation.py`:

```python
HOTKEY = keyboard.Key.f9  # Change to any key you prefer
```

Available options:
- `keyboard.Key.f9` (default)
- `keyboard.Key.f10`
- `keyboard.KeyCode.from_char('r')` (for letter keys)
- For combinations, use `GlobalHotKeys` format like `'<ctrl>+<shift>+r'`

## Troubleshooting

### PyAudio installation fails

**macOS**:
```bash
brew install portaudio
uv add pyaudio
```

**Linux**:
```bash
sudo apt-get install portaudio19-dev
uv add pyaudio
```

**Windows**:
- Download the appropriate wheel from [here](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio)
- Install with `pip install PyAudio*.whl`

### No audio is being recorded

1. Check microphone permissions in System Preferences (macOS) or Settings (Windows)
2. Verify your microphone is working with another app
3. Try a different microphone if available

### API key errors

- Make sure `.env` file exists and contains `ELEVENLABS_API_KEY=your_key_here`
- Get your API key from: https://elevenlabs.io/app/settings/api-keys
- Check your ElevenLabs account has available quota

### Transcription is slow or laggy

- Check your internet connection
- Scribe v2 Realtime requires a stable connection
- Lower audio quality environments may need longer processing

## How Streaming Works

The app uses **partial transcripts** from ElevenLabs:

1. **Partial transcripts** arrive in real-time as you speak (~150ms latency)
2. The app types these partial transcripts immediately
3. When new partial text arrives, it deletes the old text (backspace) and types the updated version
4. When you stop recording, a **committed transcript** (final, polished version) replaces the partial text

This creates a smooth, real-time streaming experience where you see your words appear instantly!

## Architecture

- **Hotkey listener**: `pynput` for global hotkey detection
- **Audio capture**: `pyaudio` for real-time microphone recording
- **Transcription**: ElevenLabs Scribe v2 Realtime WebSocket API
- **Auto-paste**: `pynput.keyboard.Controller` for simulating keypresses
- **Async I/O**: `asyncio` for concurrent audio streaming and transcription

## Pricing

Scribe v2 Realtime costs approximately $0.39-$0.63 per hour of audio transcribed, depending on your ElevenLabs plan. See [pricing details](https://elevenlabs.io/pricing).

## License

MIT

## Credits

Built with [ElevenLabs Scribe v2 Realtime](https://elevenlabs.io/docs/api-reference/speech-to-text)
