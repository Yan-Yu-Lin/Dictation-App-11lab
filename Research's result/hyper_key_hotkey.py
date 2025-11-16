#!/usr/bin/env python3
"""
Global Hotkey Detection with Hyper Key Support for macOS
Uses PyObjC with NSEvent to detect hotkeys AFTER Karabiner-Elements transformation.

Requirements:
    pip install pyobjc-core pyobjc-framework-Cocoa --break-system-packages

This script detects Cmd+Option+Control+D (hyper key + D) combination.
"""

from AppKit import NSApplication, NSApp
from Foundation import NSObject
from Cocoa import (
    NSEvent,
    NSKeyDownMask,
    NSFlagsChangedMask,
    NSEventModifierFlagCommand,
    NSEventModifierFlagOption,
    NSEventModifierFlagControl,
    NSEventModifierFlagShift
)
from PyObjCTools import AppHelper


class HotkeyMonitor(NSObject):
    """Monitor for global hotkeys using NSEvent."""
    
    def init(self):
        """Initialize the hotkey monitor."""
        self = super(HotkeyMonitor, self).init()
        if self is None:
            return None
        
        # Track currently pressed modifiers
        self.cmd_pressed = False
        self.option_pressed = False
        self.control_pressed = False
        self.shift_pressed = False
        
        return self
    
    def applicationDidFinishLaunching_(self, notification):
        """Set up event monitoring when app finishes launching."""
        print("Hotkey monitor started. Press Cmd+Option+Control+D to trigger.")
        print("Press Cmd+Q to quit.\n")
        
        # Monitor key down events
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask,
            self.handleKeyEvent_
        )
        
        # Monitor modifier flag changes
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSFlagsChangedMask,
            self.handleFlagsChanged_
        )
    
    def handleFlagsChanged_(self, event):
        """Handle modifier key changes."""
        modifiers = event.modifierFlags()
        
        self.cmd_pressed = bool(modifiers & NSEventModifierFlagCommand)
        self.option_pressed = bool(modifiers & NSEventModifierFlagOption)
        self.control_pressed = bool(modifiers & NSEventModifierFlagControl)
        self.shift_pressed = bool(modifiers & NSEventModifierFlagShift)
    
    def handleKeyEvent_(self, event):
        """Handle key down events and check for hotkey combination."""
        # Get the key that was pressed
        key_char = event.charactersIgnoringModifiers()
        key_code = event.keyCode()
        modifiers = event.modifierFlags()
        
        # Extract modifier flags
        cmd = bool(modifiers & NSEventModifierFlagCommand)
        option = bool(modifiers & NSEventModifierFlagOption)
        control = bool(modifiers & NSEventModifierFlagControl)
        shift = bool(modifiers & NSEventModifierFlagShift)
        
        # Check for Cmd+Option+Control+D (hyper key + D)
        if key_char and key_char.lower() == 'd':
            if cmd and option and control and not shift:
                print("🎯 HOTKEY TRIGGERED: Cmd+Option+Control+D")
                self.onHotkeyActivated()
                return
        
        # Debug: Print all key events with their modifiers
        modifier_str = []
        if cmd:
            modifier_str.append("Cmd")
        if option:
            modifier_str.append("Option")
        if control:
            modifier_str.append("Control")
        if shift:
            modifier_str.append("Shift")
        
        mod_display = "+".join(modifier_str) if modifier_str else "No modifiers"
        print(f"Key: {key_char} (code: {key_code}) | Modifiers: {mod_display}")
    
    def onHotkeyActivated(self):
        """Called when the hotkey combination is detected."""
        print("✅ Hotkey action executed!")
        print("=" * 50)
        # Add your custom action here
        # For example: trigger a notification, run a command, etc.


def main():
    """Main entry point."""
    print("=" * 60)
    print("Hyper Key Hotkey Monitor for macOS")
    print("=" * 60)
    print("\nThis script uses NSEvent to detect global hotkeys.")
    print("It works with Karabiner-Elements hyper key transformations.\n")
    print("Target hotkey: Cmd+Option+Control+D")
    print("(If you've mapped Right Command to hyper key in Karabiner,")
    print(" just press Right Command + D)\n")
    
    # Create the NSApplication
    app = NSApplication.sharedApplication()
    
    # Create and set the delegate
    delegate = HotkeyMonitor.alloc().init()
    app.setDelegate_(delegate)
    
    # Run the event loop
    AppHelper.runEventLoop()


if __name__ == '__main__':
    main()
