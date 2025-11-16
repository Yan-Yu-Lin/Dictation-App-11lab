# Detecting Hyper Key Hotkeys on macOS with Python

## The Problem

When using hyper key remapping apps like Karabiner-Elements or BetterTouchTool, libraries like `pynput` detect the **raw key press** (e.g., "Right Command") **BEFORE** the transformation to Cmd+Option+Control occurs. This happens because pynput operates at the CGEvent level, which can intercept events before hyper key apps transform them.

## Why This Happens

**macOS Event Pipeline:**

```
1. IOKit (Lowest)
   ↓
   [Karabiner-Elements intercepts and transforms here]
   ↓
2. Virtual Keyboard Driver
   ↓ [Posts transformed events]
   ↓
3. CGEvent Level ← pynput operates here (can see pre-transformation)
   ↓
4. NSEvent Level (Highest) ← Regular macOS apps see events here
```

Karabiner-Elements:
- Seizes the physical keyboard at the IOKit level using `IOHIDDeviceOpen(kIOHIDOptionsTypeSeizeDevice)`
- Transforms keys (e.g., Right Command → Cmd+Option+Control)
- Posts new events via a virtual keyboard driver

The problem: pynput's CGEventTap can intercept at different points. If it taps too early, it sees pre-transformation events.

## Solutions

I provide **two solutions** that operate at higher levels in the event pipeline:

### Solution 1: NSEvent.addGlobalMonitorForEvents (Recommended)

**File:** `hyper_key_hotkey.py`

Uses NSEvent API (highest level) which definitely sees transformed events.

**Pros:**
- Operates at the highest level in the event pipeline
- Cleanest and most reliable approach
- Works like regular macOS applications
- Easy to understand and maintain

**Cons:**
- Requires PyObjC and running an event loop
- Cannot modify events (read-only)
- Requires Accessibility permissions

### Solution 2: CGEventTap at Session Level

**File:** `hyper_key_cgeventtap.py`

Uses CGEventTap at `kCGSessionEventTap` level (higher than HID level).

**Pros:**
- Also higher level than where pynput typically taps
- More similar to pynput's approach
- Can potentially modify events if needed

**Cons:**
- More complex code
- Still lower level than NSEvent
- Requires Accessibility permissions

## Installation

### 1. Install PyObjC

```bash
# macOS comes with Python 3, but you need PyObjC
pip3 install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz --break-system-packages

# Or if using a virtual environment:
python3 -m venv venv
source venv/bin/activate
pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

### 2. Grant Accessibility Permissions

Both solutions require Accessibility permissions:

1. Go to **System Settings** → **Privacy & Security** → **Accessibility**
2. Click the lock icon to make changes
3. Add your Terminal app (or Python executable) to the list
4. Enable the checkbox

**Important:** If running from Terminal, you need to add Terminal. If running as a standalone app, add that app.

### 3. Configure Karabiner-Elements

Make sure your hyper key is configured. Example configuration for Right Command → Hyper:

1. Open Karabiner-Elements Settings
2. Go to **Simple Modifications**
3. Add: `right_command` → `command+option+control`

Or use Complex Modifications for more advanced mappings.

## Usage

### Solution 1 (Recommended):

```bash
python3 hyper_key_hotkey.py
```

You'll see output like:
```
Hyper Key Hotkey Monitor for macOS
==================================================
Target hotkey: Cmd+Option+Control+D
Press Right Command + D to trigger
```

### Solution 2:

```bash
python3 hyper_key_cgeventtap.py
```

### Customizing the Hotkey

Both scripts detect **Cmd+Option+Control+D** by default. To change this:

#### In `hyper_key_hotkey.py`:

Find this section (around line 75):
```python
if key_char and key_char.lower() == 'd':
    if cmd and option and control and not shift:
        print("🎯 HOTKEY TRIGGERED: Cmd+Option+Control+D")
```

Change `'d'` to your desired key and adjust the modifier check.

#### In `hyper_key_cgeventtap.py`:

Find this section (around line 71):
```python
# D key has keycode 2
if key_code == 2 and cmd and option and control and not shift:
```

Change the key code and modifiers as needed.

**Common macOS Key Codes:**
- A = 0, S = 1, D = 2, F = 3, H = 4, G = 5, Z = 6, X = 7, C = 8, V = 9, B = 11
- Q = 12, W = 13, E = 14, R = 15, Y = 16, T = 17
- 1 = 18, 2 = 19, 3 = 20, 4 = 21, 6 = 22, 5 = 23
- Return = 36, Space = 49, Delete = 51, Escape = 53

### Adding Your Custom Action

Find the `onHotkeyActivated()` method (Solution 1) or the hotkey trigger section (Solution 2) and add your code:

```python
def onHotkeyActivated(self):
    """Called when the hotkey combination is detected."""
    print("✅ Hotkey action executed!")
    
    # Add your custom actions here:
    import subprocess
    subprocess.run(['open', '-a', 'Safari'])  # Open Safari
    # or
    subprocess.run(['osascript', '-e', 'tell app "System Events" to keystroke "c" using command down'])
    # or any other Python code
```

## Troubleshooting

### "Failed to create event tap" or No events detected

**Problem:** Accessibility permissions not granted.

**Solution:**
1. System Settings → Privacy & Security → Accessibility
2. Remove Terminal (or your app) from the list if it's there
3. Re-add it and make sure it's enabled
4. Restart your Python script

### Events are detected but modifiers are wrong

**Problem:** Karabiner might not be transforming correctly, or you're using the wrong keyboard.

**Solution:**
1. Open Karabiner-EventViewer (comes with Karabiner-Elements)
2. Press your hyper key combination
3. Verify it shows `left_command`, `left_option`, `left_control` (or right_ versions)
4. Make sure Karabiner is enabled for your keyboard in the Devices tab

### Script detects raw Right Command instead of transformed key

**Problem:** You might be using a library or method that taps too low in the event pipeline.

**Solution:** Use the provided NSEvent-based solution (`hyper_key_hotkey.py`). This is guaranteed to work at the correct level.

### "Module not found" errors

**Problem:** PyObjC not installed correctly.

**Solution:**
```bash
pip3 install --upgrade pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz --break-system-packages
```

### Script runs but hotkey doesn't trigger

**Problem:** 
1. Wrong key code or modifiers
2. Another app is consuming the hotkey first
3. Karabiner isn't running

**Solution:**
1. Run the script - it prints ALL keys with their modifiers
2. Press your intended hotkey and see what gets printed
3. Adjust the code to match what's actually being sent
4. Make sure Karabiner-Elements is running and enabled

## Comparison with pynput

### Why pynput doesn't work:

```python
from pynput import keyboard

def on_press(key):
    # This sees the RAW key before transformation
    print(key)  # Shows Key.cmd_r instead of cmd+option+control

listener = keyboard.Listener(on_press=on_press)
listener.start()
```

pynput uses CGEventTap at a level that can intercept events before Karabiner transforms them.

### Why NSEvent works:

NSEvent operates at the application level, where it receives events **after** they've been:
1. Captured by Karabiner at IOKit level
2. Transformed by Karabiner's logic
3. Posted back to the system via virtual keyboard
4. Propagated up to the NSEvent level

## Alternative: Using Karabiner's Complex Modifications

If you just need to trigger shell commands or specific actions, you can use Karabiner's built-in functionality without Python:

```json
{
  "description": "Hyper+D opens Terminal",
  "manipulators": [
    {
      "type": "basic",
      "from": {
        "key_code": "d",
        "modifiers": {
          "mandatory": ["command", "option", "control"]
        }
      },
      "to": [
        {
          "shell_command": "open -a Terminal"
        }
      ]
    }
  ]
}
```

This approach works entirely within Karabiner and doesn't require Python.

## Credits

Research based on:
- Karabiner-Elements documentation and source code
- macOS CGEvent and NSEvent documentation  
- PyObjC documentation

## License

These example scripts are provided as-is for educational purposes. Use freely in your projects.

## Need Help?

If you have issues:
1. Check the Troubleshooting section above
2. Run the script and post the debug output
3. Verify Karabiner-EventViewer shows the transformed keys correctly
4. Make sure you have Accessibility permissions enabled
