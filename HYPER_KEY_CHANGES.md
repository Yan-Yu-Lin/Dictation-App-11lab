# Hyper Key Support Implementation

## What Changed

The dictation app now uses **NSEvent API** instead of `pynput` for hotkey detection. This allows it to correctly detect **Cmd+Option+Control+D** after your hyper key app (Karabiner-Elements, BetterTouchTool, etc.) transforms the Right Command key.

## Why This Works

### The Problem with pynput
- `pynput` uses CGEvent API at a **low system level**
- It intercepts key events **before** Karabiner transforms them
- So it saw "Right Command" instead of "Cmd+Option+Control"

### The Solution with NSEvent
- `NSEvent` operates at the **application level** (same as Chrome, Safari, etc.)
- It receives events **after** Karabiner has transformed them
- So it correctly sees "Cmd+Option+Control+D"

### Event Flow
```
Physical Keyboard (Right Cmd + D)
    ↓
Karabiner-Elements intercepts
    ↓
Transforms: Right Cmd → Cmd+Option+Control
    ↓
CGEvent Level ← pynput was here (sees RAW key) ❌
    ↓
NSEvent Level ← Our solution is here (sees TRANSFORMED key) ✅
    ↓
Applications (Chrome, etc.)
```

## Technical Changes

### Dependencies Added
- `pyobjc-core`
- `pyobjc-framework-Cocoa`
- `pyobjc-framework-Quartz`

### Code Changes
1. **Removed**: `pynput.keyboard.Listener`
2. **Added**: `NSEvent.addGlobalMonitorForEventsMatchingMask`
3. **New class**: `HotkeyMonitor(NSObject)` to handle NSEvent callbacks
4. **Event loop changes**:
   - Asyncio loop runs in separate thread
   - NSApplication event loop runs on main thread
   - Both loops communicate via `asyncio.run_coroutine_threadsafe()`

## How to Test

### Prerequisites
1. Make sure you have a hyper key app configured (e.g., Karabiner-Elements)
2. Right Command should be mapped to Cmd+Option+Control
3. Grant Accessibility permissions to Terminal (System Settings → Privacy & Security → Accessibility)

### Testing Steps
```bash
# Run the app
uv run dictation.py

# You should see:
# "Dictation App Ready!"
# "Press Cmd+Option+Control+D to start/stop recording"
# "(Or press your Hyper Key + D if you have it configured)"

# Press your hyper key (Right Command) + D
# Recording should start

# Speak something
# Press hyper key + D again
# Recording should stop and text should be pasted
```

### Expected Behavior
- ✅ Pressing Right Command + D (with hyper key configured) should toggle recording
- ✅ No "raw Right Command" detection issues
- ✅ Works the same as Cmd+Option+Control+D pressed manually

## Troubleshooting

### No events detected?
1. Check Accessibility permissions for Terminal
   - System Settings → Privacy & Security → Accessibility
   - Enable Terminal (or your Python app)

2. Verify Karabiner-Elements is running
   - Check menu bar for Karabiner icon
   - Open Karabiner-EventViewer to verify transformations

### Still detecting wrong keys?
- The app should now correctly detect the transformed combination
- If issues persist, verify your hyper key mapping in Karabiner-Elements

### "Module not found" errors?
```bash
uv sync  # Reinstall dependencies
```

## Architecture Notes

### Thread Model
```
Main Thread:
  ├─ NSApplication event loop (handles hotkey detection)
  └─ HotkeyMonitor receives NSEvent callbacks

Background Thread:
  ├─ asyncio event loop (handles dictation app logic)
  └─ DictationApp runs audio recording, ElevenLabs connection
```

### Communication
- NSEvent callback → `asyncio.run_coroutine_threadsafe()` → asyncio event loop
- This bridges NSApplication (main thread) and asyncio (background thread)

## Credits

Solution researched and documented in `Research's result/` folder based on understanding macOS event pipeline architecture.
