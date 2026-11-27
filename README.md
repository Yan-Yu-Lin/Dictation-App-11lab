# Dictation App - ElevenLabs Scribe v3 (Linux/Wayland)

> **Linux port** (branch `feature/linux-port`): designed for Hyprland on Wayland
> (built on Omarchy/Arch). The macOS version lives on `main`; this port keeps
> the same behaviour and copies its text functions verbatim.

A dictation daemon that transcribes speech with ElevenLabs Scribe v3 (live while
you speak, or the whole recording at once) and pastes the result at your cursor.

## Features

- **Models**: `--model v3` (default) = Scribe v3 Live (live) / Scribe v3 (batch)
  with `keyterms.json` and OpenCC `s2tw`; `--model v2` = the old Scribe v2 Realtime setup
- **Live or batch**: live streams while you speak; batch records first and sends
  one request when you stop. Switch with Hyper+Shift+M (`dictation-ctl.py mode`);
  the choice is saved in `~/.config/dictation-app/settings.json`
- **Hotkey toggle**: Hyper+D (SUPER+CTRL+ALT+D) starts/stops recording via a
  Hyprland keybind talking to the daemon over a unix socket
- **Right Shift push-to-talk**: hold to record, release to stop (passive evdev
  monitoring — Right Shift still works as a normal Shift key); keyboards
  plugged in later are picked up within 0.5 s
- **Floating status dot**: layer-shell overlay top-center; steady red dot
  while recording, orange while finalizing, blue for notices; click-through,
  never takes focus
- **Live preview**: the dot expands into a capsule showing what is being
  transcribed (same text pipeline as the paste), wrapping up to 4 lines.
  Turn it on/off with Hyper+Shift+D (`dictation-ctl.py preview`); saved too
- **One paste per recording**: Scribe Live commits on its own about every 35 s;
  those pieces are held and pasted together when you stop
- **Auto-paste**: result is pasted at the cursor — Ctrl+V in GUI apps,
  Ctrl+Shift+V in terminals (detected via Hyprland window tags)
- **Chinese post-processing**: OpenCC conversion + user-configurable literal
  replacements (`character_replacements.json`) + per-sentence punctuation
  (English sentences get `, . ?`, Chinese ones `，。？`) + dictation artifact
  cleanup (ellipsis noise, trailing 。, cutoff dashes)
- **Keyterms** (`keyterms.json`, up to 50 terms / 1000 chars): biases Scribe
  towards names like Claude Code, omarchy, Hyprland
- **Pauses media while recording**: MPRIS players (browser, Spotify, mpv...)
  that are playing are paused and resumed afterwards (`--no-pause-media` to turn off)
- **No LLM / no local model**: raw Scribe output only (`no_verbatim=true`)
- **Sound feedback** via freedesktop sounds; **clipboard preserved** after paste

## Requirements

- Linux with **Wayland + Hyprland** (uses `hyprctl`, layer-shell)
- **PipeWire** (or PulseAudio) with `paplay`; **PortAudio** for PyAudio
- `wl-clipboard` (`wl-copy`/`wl-paste`), `wtype`, `notify-send`, `gdbus`
- **System packages for the status dot**: `python-gobject`, `gtk4`,
  `gtk4-layer-shell` (the dot runs on the system python3, outside the venv)
- Membership in the **`input` group** for Right Shift push-to-talk
- **If you use keyd**: it collapses right/left modifiers, emitting Right Shift
  as `KEY_LEFTSHIFT` — push-to-talk never fires. Add this to
  `/etc/keyd/default.conf` and `sudo keyd reload`:

  ```ini
  [main]
  rightshift = rightshift
  ```
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

On omarchy it runs as a systemd user service (`systemctl --user restart dictation`,
`journalctl --user -u dictation -f`); don't also start it by hand.

Options: `--chinese {tw,cn}` (default tw), `--disable-right-shift-ptt`,
`--model {v2,v3}` (default v3), `--mode {live,batch}` (default: last saved),
`--no-pause-media`.

Control it from anywhere (this is what the Hyprland binds run):

```bash
python3 dictation-ctl.py {toggle|start|stop|status|preview|mode|info}
```

`info` prints the model, mode, preview state and keyterm count.

### Hyprland wiring (already in ~/.config/hypr/bindings.lua)

```lua
hl.layer_rule({ match = { namespace = "dictation-dot" }, no_anim = true, animation = "none" })
hl.unbind("SUPER + CTRL + ALT + D") -- Omarchy default: Calendar
o.bind(
  "SUPER + CTRL + ALT + D",
  "Dictation toggle",
  "python3 " .. os.getenv("HOME") .. "/Projects/Dictation-App-11lab/dictation-ctl.py toggle"
)
o.bind(
  "SUPER + CTRL + ALT + SHIFT + D",
  "Dictation live preview on/off",
  "python3 " .. os.getenv("HOME") .. "/Projects/Dictation-App-11lab/dictation-ctl.py preview"
)
o.bind(
  "SUPER + CTRL + ALT + SHIFT + M",
  "Dictation live/batch mode",
  "python3 " .. os.getenv("HOME") .. "/Projects/Dictation-App-11lab/dictation-ctl.py mode"
)
```

The `layer_rule` stops Hyprland from animating the preview capsule resizes.

## Architecture

- `dictation.py` — the daemon: audio capture (PyAudio 16kHz PCM) → ElevenLabs
  Scribe (live WebSocket or one batch request) → transcript pipeline → paste.
  Owns the unix socket (`$XDG_RUNTIME_DIR/dictation-app.sock`), the Right Shift
  evdev monitor, the media pauser, and the status dot child process
- `dictation-ctl.py` — stdlib-only client for the socket (fast enough to bind)
- `dictation-dot.py` — GTK4 + gtk4-layer-shell status dot + live preview;
  driven over stdin (`recording|finalizing|hide|quit|partial <text>|notice <text>|preview on|preview off`);
  must run with `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so` (the daemon handles this)
- `eval/linux_mode_check.py` — runs WAV files through the real live/batch code
  without a mic, paste or socket (safe while the daemon runs)

Transcript pipeline when you stop:

```
Scribe text (live pieces joined, or the batch result) → OpenCC (s2tw for v3) →
character_replacements.json → per-sentence punctuation →
artifact strip (…… / trailing 。 / cutoff dashes) → sanitize → paste
```

`keyterms.json` and `character_replacements.json` are shared with the macOS
version; copy them over from `main` when they change there.

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

Scribe v3 / v3 Live cost about $0.20–$0.22 per hour of audio, plus about
$0.05–$0.08 per hour when keyterms are sent (ElevenLabs API pricing, 2026-11).

## License

MIT
