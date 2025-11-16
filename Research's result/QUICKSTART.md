# Quick Start Guide

## TL;DR

**Problem:** pynput sees raw keys before Karabiner-Elements transforms them.

**Solution:** Use NSEvent (higher-level API) instead of pynput.

## Installation (One Command)

```bash
pip3 install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz --break-system-packages
```

## Enable Accessibility

1. **System Settings** → **Privacy & Security** → **Accessibility**
2. Add **Terminal** (or your Python app)
3. Enable the checkbox

## Files Included

### 1. `hyper_key_hotkey.py` ⭐ RECOMMENDED
   - Uses NSEvent API (highest level)
   - Detects Cmd+Option+Control+D by default
   - **Start here!**

### 2. `hyper_key_cgeventtap.py`
   - Uses CGEventTap at session level
   - Alternative approach
   - More similar to pynput

### 3. `comparison_demo.py`
   - Shows the difference between pynput and NSEvent
   - Educational tool
   - Run this to see why pynput fails

### 4. `key_code_discovery.py`
   - Utility to find key codes
   - Shows Python code snippets
   - Useful for customizing hotkeys

### 5. `README.md`
   - Complete documentation
   - Troubleshooting guide
   - Technical explanation

## Quick Test

```bash
# 1. Run the recommended solution
python3 hyper_key_hotkey.py

# 2. Press Right Command + D (if Right Cmd is mapped to hyper in Karabiner)

# Expected output:
# 🎯 HOTKEY TRIGGERED: Cmd+Option+Control+D
# ✅ Hotkey action executed!
```

## Customizing for Your Hotkey

Open `hyper_key_hotkey.py` and find line ~75:

```python
# Change 'd' to your key
if key_char and key_char.lower() == 'd':
    # Adjust modifiers as needed
    if cmd and option and control and not shift:
        print("🎯 HOTKEY TRIGGERED")
        self.onHotkeyActivated()
```

## Common Customizations

### Different Key

```python
if key_char and key_char.lower() == 'x':  # Change to 'x'
```

### Without Shift

```python
if cmd and option and control and not shift:  # Excludes Shift
```

### With Shift (Hyper+Shift+D)

```python
if cmd and option and control and shift:  # Includes Shift
```

### Just Hyper+Cmd (no Option/Control)

```python
if cmd and not option and not control:  # Only Command
```

## Your Custom Action

Find the `onHotkeyActivated()` method and add your code:

```python
def onHotkeyActivated(self):
    """Called when hotkey is detected."""
    
    # Example 1: Open an application
    import subprocess
    subprocess.run(['open', '-a', 'Safari'])
    
    # Example 2: Run a shell command
    subprocess.run(['say', 'Hotkey activated'])
    
    # Example 3: Send keystrokes
    subprocess.run(['osascript', '-e', 
        'tell app "System Events" to keystroke "hello"'])
    
    # Example 4: Your Python code
    print("Do anything with Python here!")
```

## Troubleshooting

### No events detected?
- Check Accessibility permissions
- Make sure Karabiner-Elements is running
- Verify your keyboard is enabled in Karabiner's Devices tab

### Wrong keys detected?
- Run `key_code_discovery.py` to see what keys are actually being sent
- Adjust your code based on the output

### "Module not found"?
```bash
pip3 install pyobjc-core pyobjc-framework-Cocoa --break-system-packages
```

## Why This Works

```
Physical Keyboard
       ↓
[Karabiner intercepts at IOKit level]
       ↓
[Transforms: Right Cmd → Cmd+Option+Control]
       ↓
[Posts via virtual keyboard]
       ↓
CGEvent Level ← pynput listens here (sees RAW key)
       ↓
NSEvent Level ← Our solution listens here (sees TRANSFORMED key) ✅
       ↓
Applications
```

## Need More Help?

1. Read the full `README.md`
2. Run `comparison_demo.py` to see the difference
3. Use `key_code_discovery.py` to debug your keys
4. Check Karabiner-EventViewer to verify transformations

## Key Insight

The secret is using **NSEvent** (AppKit framework) instead of **CGEvent** (Core Graphics framework). NSEvent operates at a higher level where it receives events **after** Karabiner has transformed them, just like regular macOS applications do.

That's why your Chrome shortcuts work but pynput doesn't - Chrome uses NSEvent (or equivalent high-level APIs), while pynput uses CGEvent.
