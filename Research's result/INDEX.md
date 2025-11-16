# macOS Hyper Key Detection for Python - Complete Solution Package

## 📦 What's Included

This package provides complete solutions for detecting global hotkeys on macOS that work with hyper key remapping apps like Karabiner-Elements and BetterTouchTool.

### 🎯 Core Solution Files

1. **hyper_key_hotkey.py** ⭐ **RECOMMENDED**
   - Uses NSEvent API (highest level in macOS event pipeline)
   - Detects Cmd+Option+Control+D by default
   - Clean, simple, and reliable
   - **Start here!**

2. **hotkey_template.py** 🎨 **EASIEST TO CUSTOMIZE**
   - Pre-configured template with multiple hotkeys
   - Easy-to-edit configuration section
   - Built-in examples (open apps, paste text, custom actions)
   - Just edit the HOTKEYS list and run!

3. **hyper_key_cgeventtap.py**
   - Alternative approach using CGEventTap at session level
   - More similar to pynput's approach
   - Lower level but still works with transformations

### 📚 Documentation

4. **QUICKSTART.md**
   - Fast setup guide
   - Installation instructions
   - Quick examples
   - **Read this first!**

5. **README.md**
   - Comprehensive documentation
   - Technical explanation of the problem
   - Troubleshooting guide
   - Comparison with pynput

### 🔧 Utility Tools

6. **comparison_demo.py**
   - Educational demonstration
   - Shows the difference between pynput and NSEvent
   - Helps understand why pynput fails
   - Run this to see the problem in action

7. **key_code_discovery.py**
   - Interactive tool to discover key codes
   - Shows modifier combinations
   - Generates Python code snippets
   - Essential for customizing hotkeys

## 🚀 Quick Start (3 Steps)

### Step 1: Install Dependencies
```bash
pip3 install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz --break-system-packages
```

### Step 2: Enable Accessibility
1. System Settings → Privacy & Security → Accessibility
2. Add Terminal
3. Enable the checkbox

### Step 3: Run
```bash
python3 hyper_key_hotkey.py
```
or
```bash
python3 hotkey_template.py  # For multiple hotkeys
```

## 📖 Which File Should I Use?

### For Beginners
→ **Start with:** `QUICKSTART.md` then run `hyper_key_hotkey.py`

### For Quick Customization
→ **Use:** `hotkey_template.py` - Edit the HOTKEYS list, no other code changes needed

### For Learning
→ **Read:** `README.md` then run `comparison_demo.py` to see the problem

### For Debugging
→ **Use:** `key_code_discovery.py` to find your key codes

### For Advanced Users
→ **Try:** `hyper_key_cgeventtap.py` for lower-level control

## 🎓 Understanding the Problem

**The Issue:**
```
pynput detects: Right Command + D
     ❌ (raw key, before transformation)

NSEvent detects: Cmd+Option+Control+D  
     ✅ (transformed key, what you want!)
```

**Why?**
- pynput uses CGEventTap (low level)
- Karabiner transforms at IOKit level (even lower)
- pynput intercepts before transformation completes
- NSEvent operates at application level (high level)
- NSEvent sees events after transformation

**The Solution:**
Use NSEvent API (PyObjC) instead of pynput!

## 📋 File Descriptions

| File | Size | Purpose | Difficulty |
|------|------|---------|------------|
| QUICKSTART.md | 4KB | Fast setup guide | Beginner |
| README.md | 8KB | Full documentation | All levels |
| hyper_key_hotkey.py | 4.5KB | Main solution (single hotkey) | Beginner |
| hotkey_template.py | 6.8KB | Multiple hotkeys template | Beginner |
| hyper_key_cgeventtap.py | 5KB | Alternative CGEvent solution | Intermediate |
| comparison_demo.py | 5KB | Educational demo | All levels |
| key_code_discovery.py | 7KB | Key code finder tool | All levels |

## 🔑 Key Features

✅ Works with Karabiner-Elements hyper key transformations
✅ Works with BetterTouchTool
✅ Detects global hotkeys (works when app not focused)
✅ Pure Python solution using PyObjC
✅ No compiled extensions needed
✅ Multiple hotkey support
✅ Easy to customize
✅ Well documented
✅ Includes debugging tools

## 🛠️ System Requirements

- macOS 10.14 or later
- Python 3.6+
- PyObjC (installed via pip)
- Accessibility permissions
- Karabiner-Elements or BetterTouchTool (optional, but that's what this solves!)

## ⚙️ Configuration Examples

### Single Hotkey (hyper_key_hotkey.py)
```python
# Detect Cmd+Option+Control+D
if key_char.lower() == 'd':
    if cmd and option and control and not shift:
        self.onHotkeyActivated()
```

### Multiple Hotkeys (hotkey_template.py)
```python
HOTKEYS = [
    {
        'name': 'Open Safari',
        'key': 'd',
        'modifiers': {'cmd': True, 'option': True, 'control': True, 'shift': False},
        'action': open_safari
    },
    # Add more hotkeys here...
]
```

## 🐛 Troubleshooting

### No events detected?
1. Check Accessibility permissions
2. Verify Karabiner-Elements is running
3. Run `key_code_discovery.py` to debug

### Wrong modifiers detected?
1. Run `comparison_demo.py` to see what each approach detects
2. Check Karabiner-EventViewer to verify transformations
3. Use `key_code_discovery.py` to see actual key codes

### Module not found?
```bash
pip3 install pyobjc-core pyobjc-framework-Cocoa --break-system-packages
```

## 💡 Pro Tips

1. **Start Simple**: Begin with `hyper_key_hotkey.py` for a single hotkey
2. **Use Templates**: Switch to `hotkey_template.py` for multiple hotkeys
3. **Debug First**: Run `key_code_discovery.py` before customizing
4. **Verify Transforms**: Use Karabiner-EventViewer to confirm your transformations work
5. **Test Incrementally**: Add one hotkey at a time

## 🆚 vs pynput

| Feature | pynput | This Solution |
|---------|--------|---------------|
| Works with hyper keys | ❌ No | ✅ Yes |
| Sees transformed events | ❌ No | ✅ Yes |
| Installation | Easy | Easy |
| Cross-platform | ✅ Yes | macOS only |
| API level | CGEvent (low) | NSEvent (high) |

## 📞 Support

If you have issues:
1. Read `QUICKSTART.md` for common solutions
2. Check the Troubleshooting section in `README.md`
3. Run `comparison_demo.py` to verify the problem
4. Use `key_code_discovery.py` to debug your keys

## 🎉 Success Indicators

You know it's working when:
- ✅ `key_code_discovery.py` shows correct modifiers (Cmd+Option+Control)
- ✅ `comparison_demo.py` shows NSEvent detecting your hyper key
- ✅ Your hotkey triggers your custom action
- ✅ Other apps (like Chrome) still work with the same hotkey

## 📄 License

These example scripts are provided as-is for educational purposes. Use freely in your projects.

## 🙏 Credits

Solution developed through research of:
- Karabiner-Elements source code and documentation
- Apple's CGEvent and NSEvent documentation
- PyObjC documentation
- macOS event pipeline architecture

---

**Start here:** `QUICKSTART.md` → `hyper_key_hotkey.py` → Customize!
