#!/usr/bin/env python3
"""
Comparison Demo: pynput vs NSEvent for Hyper Key Detection

This script demonstrates why pynput fails to detect hyper key transformations
while NSEvent succeeds.

Run this script and press your hyper key combination (e.g., Right Command + D)
to see what each approach detects.

Requirements:
    pip install pynput pyobjc-core pyobjc-framework-Cocoa --break-system-packages
"""

import threading
import time
from pynput import keyboard as pynput_keyboard
from AppKit import NSApplication, NSApp
from Foundation import NSObject
from Cocoa import (
    NSEvent,
    NSKeyDownMask,
    NSEventModifierFlagCommand,
    NSEventModifierFlagOption,
    NSEventModifierFlagControl,
)
from PyObjCTools import AppHelper


class ComparisonMonitor(NSObject):
    """Monitor that uses NSEvent to detect keys."""
    
    def init(self):
        self = super(ComparisonMonitor, self).init()
        return self
    
    def applicationDidFinishLaunching_(self, notification):
        """Set up NSEvent monitoring."""
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask,
            self.handleKeyEvent_
        )
    
    def handleKeyEvent_(self, event):
        """Handle NSEvent key presses."""
        key_char = event.charactersIgnoringModifiers()
        modifiers = event.modifierFlags()
        
        cmd = bool(modifiers & NSEventModifierFlagCommand)
        option = bool(modifiers & NSEventModifierFlagOption)
        control = bool(modifiers & NSEventModifierFlagControl)
        
        # Only print if it's a D key with modifiers
        if key_char and key_char.lower() == 'd' and (cmd or option or control):
            modifier_str = []
            if cmd:
                modifier_str.append("Cmd")
            if option:
                modifier_str.append("Option")
            if control:
                modifier_str.append("Control")
            
            print(f"  ✅ NSEvent detected: {'+'.join(modifier_str)}+D")
            
            if cmd and option and control:
                print("  🎯 NSEvent: HYPER KEY DETECTED!")


def pynput_listener():
    """Run pynput listener in a separate thread."""
    
    current_modifiers = {
        'cmd': False,
        'option': False,
        'control': False,
    }
    
    def on_press(key):
        """Handle pynput key presses."""
        # Track modifiers
        if key == pynput_keyboard.Key.cmd or key == pynput_keyboard.Key.cmd_r:
            current_modifiers['cmd'] = True
        if key == pynput_keyboard.Key.alt or key == pynput_keyboard.Key.alt_r:
            current_modifiers['option'] = True
        if key == pynput_keyboard.Key.ctrl or key == pynput_keyboard.Key.ctrl_r:
            current_modifiers['control'] = True
        
        # Check for D key
        try:
            if hasattr(key, 'char') and key.char and key.char.lower() == 'd':
                if any(current_modifiers.values()):
                    active_mods = [k.title() for k, v in current_modifiers.items() if v]
                    print(f"  ❌ pynput detected: {'+'.join(active_mods)}+D")
                    
                    if all(current_modifiers.values()):
                        print("  ⚠️  pynput: Claims hyper detected (but may be wrong!)")
        except AttributeError:
            pass
    
    def on_release(key):
        """Handle pynput key releases."""
        if key == pynput_keyboard.Key.cmd or key == pynput_keyboard.Key.cmd_r:
            current_modifiers['cmd'] = False
        if key == pynput_keyboard.Key.alt or key == pynput_keyboard.Key.alt_r:
            current_modifiers['option'] = False
        if key == pynput_keyboard.Key.ctrl or key == pynput_keyboard.Key.ctrl_r:
            current_modifiers['control'] = False
    
    listener = pynput_keyboard.Listener(
        on_press=on_press,
        on_release=on_release
    )
    listener.start()


def main():
    """Main entry point."""
    print("=" * 70)
    print("COMPARISON: pynput vs NSEvent for Hyper Key Detection")
    print("=" * 70)
    print()
    print("This demo shows what each approach detects.")
    print()
    print("INSTRUCTIONS:")
    print("1. Make sure Karabiner-Elements is running")
    print("2. Configure Right Command → Cmd+Option+Control in Karabiner")
    print("3. Press Right Command + D (your hyper key + D)")
    print()
    print("EXPECTED RESULTS:")
    print("❌ pynput should detect: Right Cmd+D (RAW key, before transformation)")
    print("✅ NSEvent should detect: Cmd+Option+Control+D (TRANSFORMED key)")
    print()
    print("=" * 70)
    print()
    
    # Start pynput listener in background thread
    print("Starting pynput listener...")
    pynput_thread = threading.Thread(target=pynput_listener, daemon=True)
    pynput_thread.start()
    time.sleep(0.5)
    
    print("Starting NSEvent listener...")
    print()
    print("Ready! Press your hyper key combination now...")
    print("(Press Ctrl+C to exit)")
    print()
    
    # Start NSEvent listener (blocks in event loop)
    app = NSApplication.sharedApplication()
    delegate = ComparisonMonitor.alloc().init()
    app.setDelegate_(delegate)
    
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        print("\n\nShutting down...")


if __name__ == '__main__':
    main()
