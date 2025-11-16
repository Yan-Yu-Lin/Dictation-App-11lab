# Dictation App - ElevenLabs Scribe v2 Realtime

> **⚠️ macOS Only**: This app is designed and tested exclusively for macOS. It uses macOS-specific APIs (QuickMacHotKey, NSApplication, PyObjC) and will not work on Windows or Linux.

A real-time dictation tool that transcribes your speech as you speak using ElevenLabs Scribe v2 Realtime API.

## Features

- **Two transcription modes**:
  - **Batch mode (default)**: Text appears after you finish speaking - fast and reliable
  - **Streaming mode**: Text appears in real-time as you speak with live updates (Kinda buggy and slow, not recommended.)
- **Global hotkey control**: Press Cmd+Option+Control+D to start/stop recording (works anywhere)
- **Hotkey interception**: The hotkey is consumed by the app and won't reach other applications (no terminal escape sequences)
- **Auto-paste**: Transcribed text is automatically pasted at your cursor position
- **Low latency**: ~150ms transcription delay using Scribe v2 Realtime
- **Sound feedback**: Audio cues when recording starts/stops

## How It Works

1. Press **Cmd+Option+Control+D** (or your configured Hyper Key + D) to start recording
2. Speak naturally - you'll see partial transcripts streaming in the console
3. In **batch mode**: Text is pasted all at once when you finish
4. In **streaming mode**: Text appears in real-time as you speak
5. Press **Cmd+Option+Control+D** again to stop recording and finalize the transcription
6. The final, polished transcript is inserted at your cursor position

## Installation

### Prerequisites

- **macOS** (required for QuickMacHotKey and NSApplication integration)
- **Python 3.13+** (managed by uv)
- **ElevenLabs API key** ([get it here](https://elevenlabs.io/app/settings/api-keys))
- **PortAudio** for PyAudio:
  ```bash
  brew install portaudio
  ```

### System Permissions Required

Your terminal app (e.g., Ghostty, iTerm2, Terminal.app) needs the following permissions:

1. **Microphone** - to record your voice
   - Path: System Settings > Privacy & Security > Microphone

2. **Input Monitoring** - to detect the global hotkey
   - Path: System Settings > Privacy & Security > Input Monitoring

3. **Accessibility** - to type/paste text into other applications
   - Path: System Settings > Privacy & Security > Accessibility

macOS will prompt you for these permissions when you first run the app.

### Setup

1. Clone or download this project

2. Install dependencies using uv:
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

1. **Batch mode (default, recommended)**: Text appears only after you finish recording - fast and reliable using clipboard paste
2. **Streaming mode**: Text appears in real-time as you speak, character by character

Run the dictation app:

```bash
# Default batch mode (recommended)
uv run dictation.py

# Explicit batch mode
uv run dictation.py --mode batch

# Streaming mode (real-time updates)
uv run dictation.py --mode streaming
```

Or activate the virtual environment first:

```bash
source .venv/bin/activate  # On macOS

python dictation.py --mode streaming  # or --mode batch
```

### Controls

- **Cmd+Option+Control+D**: Toggle recording on/off (also works as Hyper Key + D if you have it configured)
- **Ctrl+C**: Exit the application

### Sound Feedback

- **Hero sound** (Hero.aiff): Recording started
- **Glass sound** (Glass.aiff): Recording stopped

### Tips

- **Speak clearly** for best accuracy
- **Use in any text field**: The app pastes wherever your cursor is focused
- **Wait for finalization**: Let the recording stop fully before editing
- **Check your mic**: Make sure your microphone is working and has permissions
- **Batch mode is faster**: Uses clipboard paste instead of typing character-by-character

## Configuration

### Hotkey Customization

The app uses Cmd+Option+Control+D by default. To change it, edit the `@quickHotKey` decorator in `dictation.py`:

```python
@quickHotKey(
    virtualKey=kVK_ANSI_D,  # Change this to another key code
    modifierMask=mask(cmdKey, controlKey, optionKey)  # Customize modifiers
)
```

Available virtual key codes are defined in `quickmachotkey.constants`. Common examples:
- `kVK_ANSI_D` - D key
- `kVK_ANSI_R` - R key
- `kVK_F1` through `kVK_F12` - Function keys

For a complete list, see the [QuickMacHotKey documentation](https://github.com/vmdhhh/QuickMacHotKey).

### Hyper Key Support

If you've configured a Hyper Key (Caps Lock remapped to Cmd+Option+Control+Shift), the app works perfectly with "Hyper + D". See `HYPER_KEY_CHANGES.md` for implementation details.

## Troubleshooting

### PyAudio installation fails

**macOS**:
```bash
brew install portaudio
uv sync
```

### Hotkey not working

1. Check that your terminal app has **Input Monitoring** permissions
2. Make sure no other app is using the same hotkey combination
3. Try restarting the terminal app after granting permissions

### Text not being typed/pasted

1. Check that your terminal app has **Accessibility** permissions
2. Verify the cursor is focused in a text field
3. Try batch mode if streaming mode has issues

### No audio is being recorded

1. Check **Microphone** permissions for your terminal app
2. Verify your microphone is working with another app
3. Check the console for audio-related error messages

### API key errors

- Make sure `.env` file exists and contains `ELEVENLABS_API_KEY=your_key_here`
- Get your API key from: https://elevenlabs.io/app/settings/api-keys
- Check your ElevenLabs account has available quota

### Transcription is slow or laggy

- Check your internet connection
- Scribe v2 Realtime requires a stable connection
- Lower audio quality environments may need longer processing

### Terminal shows escape sequences when pressing hotkey

If you see characters like `[8706;7u` in your terminal when pressing the hotkey, it means you're on an older version. Update to the latest version which uses QuickMacHotKey to properly intercept and consume the hotkey.

## How It Works (Technical Details)

### Batch Mode (Default)
1. Records audio while you speak
2. Streams audio to ElevenLabs in real-time via WebSocket
3. Receives partial transcripts (shown in console only)
4. When you stop recording, commits the final transcript
5. **Pastes the text using clipboard** (Cmd+V) - much faster than typing
6. Restores your previous clipboard contents

### Streaming Mode
1. Records audio while you speak
2. Streams audio to ElevenLabs in real-time via WebSocket
3. **Receives partial transcripts and types them immediately**
4. Updates text incrementally (only types new characters)
5. When you stop recording, replaces partial text with final polished version

### Session Management
- Uses unique session IDs to prevent race conditions
- Async cleanup allows immediate restart without waiting
- Protects against audio stream pollution from abandoned sessions
- Thread-safe start/stop operations with `asyncio.Lock`

## Architecture

- **Hotkey interception**: QuickMacHotKey for global hotkey detection and consumption
- **Audio capture**: PyAudio for real-time microphone recording (16kHz PCM)
- **Transcription**: ElevenLabs Scribe v2 Realtime WebSocket API
- **Text insertion**:
  - Batch mode: Clipboard paste via `pyperclip` + `pynput` (Cmd+V simulation)
  - Streaming mode: Character-by-character typing via `pynput.keyboard.Controller`
- **Async I/O**: `asyncio` for concurrent audio streaming and transcription
- **Event loop**: NSApplication event loop with asyncio running in separate thread

## Pricing

Scribe v2 Realtime costs approximately $0.39-$0.63 per hour of audio transcribed, depending on your ElevenLabs plan. See [pricing details](https://elevenlabs.io/pricing).

## Known Limitations

- **macOS only**: Uses macOS-specific APIs (QuickMacHotKey, NSApplication, PyObjC)
- **Terminal permissions**: The terminal app running the script needs Microphone, Input Monitoring, and Accessibility permissions
- **Single language per session**: Language is auto-detected but doesn't change mid-session
- **Streaming mode backspace limitations**: Character-by-character replacement can be slow for long transcripts

## Credits

Built with:
- [ElevenLabs Scribe v2 Realtime](https://elevenlabs.io/docs/api-reference/speech-to-text)
- [QuickMacHotKey](https://github.com/vmdhhh/QuickMacHotKey) - Global hotkey interception
- PyAudio - Audio recording
- pynput - Keyboard control
- pyperclip - Clipboard management

## License

MIT
