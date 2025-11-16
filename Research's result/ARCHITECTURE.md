# macOS Event Pipeline Architecture

## Visual Guide: Why pynput Fails and NSEvent Succeeds

### The Problem Visualized

```
┌─────────────────────────────────────────────────────────────────┐
│                    PHYSICAL KEYBOARD                             │
│                 (Right Command key pressed)                      │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      IOKit Level                                 │
│                    (Kernel/Driver)                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │    🔴 Karabiner-Elements Intercepts Here                 │   │
│  │    - Seizes device with IOHIDDeviceOpen()                │   │
│  │    - Sees: Right Command (key code 54)                   │   │
│  └──────────────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│               Karabiner Transformation Layer                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │    Transforms: Right Cmd → Cmd + Option + Control        │   │
│  └──────────────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                Virtual Keyboard Driver                           │
│  (Karabiner-DriverKit-VirtualHIDDevice)                         │
│  Posts new events with transformed modifiers                     │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
                ▼                       ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│   CGEvent Level          │  │   CGEvent Level          │
│   (Core Graphics)        │  │   (Higher Tap Point)     │
│                          │  │                          │
│  ❌ pynput CAN be here   │  │  ✅ Our CGEventTap is    │
│  (if taps too early)     │  │  here (session level)    │
│                          │  │                          │
│  Sees: Right Cmd         │  │  Sees: Cmd+Opt+Ctrl      │
│  (Before transformation) │  │  (After transformation)  │
└──────────────────────────┘  └────────┬─────────────────┘
                                       │
                                       ▼
                            ┌──────────────────────────┐
                            │   NSEvent Level          │
                            │   (AppKit/Cocoa)         │
                            │                          │
                            │  ✅ Our solution is here │
                            │                          │
                            │  Sees: Cmd+Opt+Ctrl      │
                            │  (After transformation)  │
                            └────────┬─────────────────┘
                                     │
                                     ▼
                            ┌──────────────────────────┐
                            │     Applications         │
                            │  (Chrome, Safari, etc.)  │
                            │                          │
                            │  See: Cmd+Opt+Ctrl       │
                            │  (After transformation)  │
                            └──────────────────────────┘
```

## Key Levels Explained

### Level 1: IOKit (Lowest - Kernel Level)
**Location:** Kernel/Driver space
**What happens:**
- Raw HID events from physical hardware
- Karabiner-Elements operates here
- Seizes the device exclusively
- Has first access to key events

**APIs:** `IOHIDDeviceOpen`, `IOHIDQueueRegisterValueAvailableCallback`

### Level 2: Virtual Device Driver
**Location:** Kernel extension
**What happens:**
- Karabiner posts transformed events here
- Uses virtual keyboard driver
- Creates new events with modified keycodes/modifiers

**APIs:** `Karabiner-DriverKit-VirtualHIDDevice`

### Level 3: CGEvent (Core Graphics Events)
**Location:** User space, Core Graphics framework
**What happens:**
- Events flow through event tap system
- Multiple tap points available
- Lower tap points may see pre-transformation
- Higher tap points see post-transformation

**APIs:** `CGEventTapCreate` with different tap locations:
- `kCGHIDEventTap` - Very low (may miss transformations)
- `kCGSessionEventTap` - Higher (sees transformations) ✅
- `kCGAnnotatedSessionEventTap` - Highest CG level

**Problem:** pynput may tap at the wrong point!

### Level 4: NSEvent (Highest - Application Level)
**Location:** User space, AppKit framework
**What happens:**
- Events delivered to applications
- Always sees post-transformation events
- Same level as regular app event handling

**APIs:** `NSEvent.addGlobalMonitorForEventsMatchingMask`

**Solution:** This is where our solution operates! ✅

## Timeline of a Key Press

```
Time →

0ms: User presses Right Command + D
     ↓
1ms: IOKit receives raw event
     • Key code: 54 (right_command)
     • Key code: 2 (d)
     ↓
2ms: Karabiner-Elements intercepts
     • Recognizes "right_command"
     • Looks up transformation rule
     ↓
3ms: Karabiner applies transformation
     • Suppresses original right_command event
     • Generates new events:
       - Command flag set
       - Option flag set  
       - Control flag set
       - D key with these flags
     ↓
4ms: Posts to Virtual Keyboard Driver
     ↓
5ms: Events flow to CGEvent level
     ↓  
     ├─→ [pynput here may see old events] ❌
     │
     └─→ [kCGSessionEventTap sees new events] ✅
     ↓
6ms: Events reach NSEvent level
     • NSEvent sees: Cmd+Option+Control+D ✅
     ↓
7ms: Our Python handler receives event
     • Detects hotkey match
     • Triggers action
     ↓
8ms: Event delivered to focused app
     • App also sees: Cmd+Option+Control+D
```

## Why Different APIs See Different Things

### pynput at CGEvent Level (Low)
```python
from pynput import keyboard

def on_press(key):
    print(key)  # May print: Key.cmd_r (WRONG!)

listener = keyboard.Listener(on_press=on_press)
```

**Problem:** 
- Uses `CGEventTapCreate` 
- May tap at `kCGHIDEventTap` or similar low point
- Sees events before Karabiner finishes transformation

### Our Solution at NSEvent Level (High)
```python
from Cocoa import NSEvent

def handler(event):
    modifiers = event.modifierFlags()
    # Sees all three modifiers correctly! ✅
    
NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
    NSKeyDownMask, handler
)
```

**Solution:**
- Uses `NSEvent` (AppKit)
- Operates at application level
- Sees events after complete transformation
- Same view as regular macOS applications

## Event Flow Decision Points

```
                    ┌─── Tap here → See RAW keys ❌
                    │
IOKit ──────────────┤
                    │
                    └─── Karabiner transforms here
                         │
                         ▼
Virtual Device ──────────┬─── Some CGEventTaps here → Maybe RAW ⚠️
                         │
                         └─── Higher CGEventTaps here → See transformed ✅
                              │
                              ▼
NSEvent ─────────────────────┴─── ALWAYS see transformed ✅✅✅
                              │
                              ▼
Applications ─────────────────── See transformed
```

## API Comparison Table

| API | Level | Sees Transformed | Can Modify | Requires Permission |
|-----|-------|------------------|------------|---------------------|
| IOHIDDevice | Kernel | No | Yes | Root + Entitlement |
| CGEventTap (HID) | Low | No | Yes | Accessibility |
| CGEventTap (Session) | Medium | Yes ✅ | Yes | Accessibility |
| NSEvent.addGlobalMonitor | High | Yes ✅ | No | Accessibility |
| NSEvent.addLocalMonitor | High | Yes ✅ | Yes | None (own app) |

## Solution Comparison

### Solution 1: NSEvent (Recommended)
```
├─ Level: Highest (NSEvent/AppKit)
├─ Sees: Transformed events ✅
├─ Reliability: Very High
├─ Complexity: Low
└─ Same as: Regular macOS apps
```

### Solution 2: CGEventTap (Session Level)
```
├─ Level: Medium-High (CGEvent with kCGSessionEventTap)
├─ Sees: Transformed events ✅
├─ Reliability: High
├─ Complexity: Medium
└─ Same as: Some keyboard utilities
```

### pynput (Doesn't Work)
```
├─ Level: Low-Medium (CGEvent, tap point varies)
├─ Sees: May see pre-transformation ❌
├─ Reliability: Low for this use case
├─ Complexity: Low
└─ Issue: Taps too early in pipeline
```

## Real-World Analogy

Think of it like a postal system:

1. **IOKit = Post Office Loading Dock**
   - Raw mail arrives from trucks
   - Karabiner is a sorter who intercepts packages
   - Changes the address labels (transformations)

2. **CGEvent = Sorting Facility**
   - Multiple inspection points
   - Early inspectors see old addresses (pynput) ❌
   - Later inspectors see new addresses (our CGEventTap) ✅

3. **NSEvent = Delivery to Mailbox**
   - Final destination
   - Always has the corrected address (our NSEvent solution) ✅
   - This is what the recipient (app) sees

4. **Application = Recipient**
   - Reads the mail
   - Only sees final address (transformed keys)
   - Works correctly with transformed keys

## Summary

**The Core Issue:**
pynput can intercept events at a point in the pipeline where Karabiner-Elements hasn't finished its transformation yet.

**The Solution:**
Use NSEvent API which operates at the application level, guaranteeing you see events after all transformations are complete.

**Why It Works:**
NSEvent is at the same level as regular macOS applications. If Chrome sees your hyper key correctly, so will NSEvent!
