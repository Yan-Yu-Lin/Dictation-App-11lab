# Dictation App - ElevenLabs Scribe v2 Realtime (Linux/Wayland)

> **Linux port** (branch `feature/linux-port`): designed for Hyprland on Wayland
> (built on Omarchy/Arch). The original macOS version lives on `main` and the
> other feature branches.

A real-time dictation daemon that transcribes speech as you speak using the
ElevenLabs Scribe v2 Realtime API and pastes the result at your cursor.

## Features

- **Hotkey toggle**: Hyper+D (SUPER+CTRL+ALT+D) starts/stops recording via a
  Hyprland keybind talking to the daemon over a unix socket
- **Right Shift push-to-talk**: hold to record, release to stop (passive evdev
  monitoring — Right Shift still works as a normal Shift key)
- **Floating status dot**: layer-shell overlay top-center; pulsing red dot
  while recording, orange while finalizing; click-through, never takes focus
- **Live preview**: the dot expands into a capsule showing partial transcripts
  as you speak, already converted to Traditional Chinese
- **Auto-paste**: result is pasted at the cursor — Ctrl+V in GUI apps,
  Ctrl+Shift+V in terminals (detected via Hyprland window tags)
- **Chinese post-processing**: OpenCC s2t conversion + user-configurable
  literal replacements (`character_replacements.json`) + dictation artifact
  cleanup (ellipsis noise, trailing 。, cutoff dashes)
- **No LLM / no local model**: raw Scribe v2 output only (`no_verbatim=true`)
- **Sound feedback** via freedesktop sounds; **clipboard preserved** after paste

## Requirements

- Linux with **Wayland + Hyprland** (uses `hyprctl`, layer-shell)
- **PipeWire** (or PulseAudio) with `paplay`; **PortAudio** for PyAudio
- `wl-clipboard` (`wl-copy`/`wl-paste`), `wtype`, `notify-send`, `gdbus`
- **System packages for the status dot**: `python-gobject`, `gtk4`,
  `gtk4-layer-shell` (the dot runs on the system python3, outside the venv)
- Membership in the **`input` group** for Right Shift push-to-talk
- **Python 3.13+** managed by uv
- **ElevenLabs API key**

## Setup

```bash
uv sync
cp .env.example .env   # then put your ELEVENLABS_API_KEY in it
```

## Usage

Start the daemon (keeps running; Ctrl+C to quit):

```bash
uv run dictation.py
```

Options: `--chinese {tw,cn}` (default tw), `--disable-right-shift-ptt`.

Control it from anywhere (this is what the Hyprland bind runs):

```bash
python3 dictation-ctl.py {toggle|start|stop|status}
```

### Hyprland wiring (already in ~/.config/hypr/bindings.lua)

```lua
hl.layer_rule({ match = { namespace = "dictation-dot" }, no_anim = true, animation = "none" })
hl.unbind("SUPER + CTRL + ALT + D") -- Omarchy default: Calendar
o.bind(
  "SUPER + CTRL + ALT + D",
  "Dictation toggle",
  "python3 " .. os.getenv("HOME") .. "/Projects/Dictation-App-11lab/dictation-ctl.py toggle"
)
```

The `layer_rule` stops Hyprland from animating the preview capsule resizes.

## Architecture

- `dictation.py` — the daemon: audio capture (PyAudio 16kHz PCM) → ElevenLabs
  Scribe v2 Realtime WebSocket → transcript pipeline → paste. Owns the unix
  socket (`$XDG_RUNTIME_DIR/dictation-app.sock`), the Right Shift evdev
  monitor, and the status dot child process
- `dictation-ctl.py` — stdlib-only client for the socket (fast enough to bind)
- `dictation-dot.py` — GTK4 + gtk4-layer-shell status dot + live preview;
  driven over stdin (`recording|finalizing|hide|quit|partial <text>`); must run
  with `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so` (the daemon handles this)

Transcript pipeline on commit:

```
Scribe v2 commit → OpenCC s2t → character_replacements.json →
artifact strip (…… / trailing 。 / cutoff dashes) → sanitize → paste
```

Session management is inherited from the macOS version: per-session IDs and
queues, async cleanup with buffered-audio flush, commit-event handshake, and
dedupe of repeated commit events.

### Why a daemon + socket?

Wayland does not let apps grab global hotkeys (that was QuickMacHotKey's job
on macOS). Only the compositor can own keys, so Hyprland binds the key and
pokes the daemon through the socket instead.

## Troubleshooting

- **No paste in some app**: XWayland apps may ignore wtype's virtual keyboard;
  the text is still on the clipboard — paste manually
- **Right Shift PTT dead**: check you're in the `input` group (`groups`),
  and that the daemon printed the "Right Shift push-to-talk enabled" line
- **Dot doesn't appear**: daemon falls back to notifications if the GTK
  packages are missing — check the startup log line
- **`already running` error**: another daemon owns the socket; stop it first
  (`python3 dictation-ctl.py status` to confirm it's alive)

## Pricing

Scribe v2 Realtime costs approximately $0.39–$0.63 per hour of audio
transcribed, depending on your ElevenLabs plan.

## License

MIT
