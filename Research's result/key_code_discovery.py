#!/usr/bin/env python3
"""
Key Code Discovery Tool

This utility helps you find the correct key codes and modifier combinations
for your hotkeys. Run it and press any key combination to see its details.

Requirements:
    pip install pyobjc-core pyobjc-framework-Cocoa --break-system-packages
"""

from AppKit import NSApplication, NSApp
from Foundation import NSObject
from Cocoa import (
    NSEvent,
    NSKeyDownMask,
    NSKeyUpMask,
    NSFlagsChangedMask,
    NSEventModifierFlagCommand,
    NSEventModifierFlagOption,
    NSEventModifierFlagControl,
    NSEventModifierFlagShift,
    NSEventModifierFlagCapsLock,
    NSEventModifierFlagFunction,
)
from PyObjCTools import AppHelper


class KeyCodeDiscovery(NSObject):
    """Monitor for discovering key codes and modifiers."""
    
    def init(self):
        self = super(KeyCodeDiscovery, self).init()
        self.last_key = None
        return self
    
    def applicationDidFinishLaunching_(self, notification):
        """Set up event monitoring."""
        # Monitor all key events
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask | NSFlagsChangedMask,
            self.handleKeyEvent_
        )
    
    def handleKeyEvent_(self, event):
        """Handle all key events and display details."""
        key_code = event.keyCode()
        
        # Skip duplicate events
        if key_code == self.last_key:
            return
        self.last_key = key_code
        
        # Get character
        chars = event.characters()
        chars_ignore_mods = event.charactersIgnoringModifiers()
        
        # Get modifiers
        modifiers = event.modifierFlags()
        
        cmd = bool(modifiers & NSEventModifierFlagCommand)
        option = bool(modifiers & NSEventModifierFlagOption)
        control = bool(modifiers & NSEventModifierFlagControl)
        shift = bool(modifiers & NSEventModifierFlagShift)
        caps = bool(modifiers & NSEventModifierFlagCapsLock)
        fn = bool(modifiers & NSEventModifierFlagFunction)
        
        # Build modifier string
        modifier_parts = []
        if fn:
            modifier_parts.append("Fn")
        if control:
            modifier_parts.append("Control")
        if option:
            modifier_parts.append("Option")
        if shift:
            modifier_parts.append("Shift")
        if cmd:
            modifier_parts.append("Command")
        if caps:
            modifier_parts.append("CapsLock")
        
        modifier_str = "+".join(modifier_parts) if modifier_parts else "None"
        
        # Display information
        print("┌" + "─" * 68 + "┐")
        print(f"│ Key Code: {key_code:<20} Character: {chars_ignore_mods or '(none)':<27}│")
        print(f"│ Modifiers: {modifier_str:<57}│")
        
        if chars != chars_ignore_mods:
            print(f"│ With mods: {chars or '(none)':<57}│")
        
        # Show Python code snippet
        if modifier_parts:
            print("├" + "─" * 68 + "┤")
            print("│ Python Code Snippet:" + " " * 47 + "│")
            
            # For NSEvent
            conditions = []
            if cmd:
                conditions.append("cmd")
            if option:
                conditions.append("option")
            if control:
                conditions.append("control")
            if shift:
                conditions.append("shift")
            
            if chars_ignore_mods:
                print(f"│   if key_char == '{chars_ignore_mods}' and {' and '.join(conditions)}:" + " " * (68 - 25 - len(' and '.join(conditions)) - len(chars_ignore_mods)) + "│")
            else:
                print(f"│   if key_code == {key_code} and {' and '.join(conditions)}:" + " " * (68 - 19 - len(' and '.join(conditions)) - len(str(key_code))) + "│")
            
            print("│       # Your action here" + " " * 43 + "│")
        
        print("└" + "─" * 68 + "┘")
        print()


# Key code reference (for common keys)
KEY_CODE_REFERENCE = {
    # Letters
    0: 'A', 1: 'S', 2: 'D', 3: 'F', 4: 'H', 5: 'G', 6: 'Z', 7: 'X', 8: 'C', 9: 'V',
    11: 'B', 12: 'Q', 13: 'W', 14: 'E', 15: 'R', 16: 'Y', 17: 'T',
    31: 'O', 32: 'U', 34: 'I', 35: 'P',
    37: 'L', 38: 'J', 40: 'K',
    45: 'N', 46: 'M',
    
    # Numbers
    18: '1', 19: '2', 20: '3', 21: '4', 23: '5', 22: '6', 26: '7', 28: '8', 25: '9', 29: '0',
    
    # Special keys
    36: 'Return', 48: 'Tab', 49: 'Space', 51: 'Delete', 53: 'Escape',
    
    # Function keys
    122: 'F1', 120: 'F2', 99: 'F3', 118: 'F4', 96: 'F5', 97: 'F6',
    98: 'F7', 100: 'F8', 101: 'F9', 109: 'F10', 103: 'F11', 111: 'F12',
    
    # Arrow keys
    123: 'Left', 124: 'Right', 125: 'Down', 126: 'Up',
    
    # Modifiers (you'll see these with FlagsChanged events)
    54: 'Right Cmd', 55: 'Left Cmd',
    58: 'Right Option', 61: 'Left Option',
    59: 'Right Control', 62: 'Left Control',
    56: 'Left Shift', 60: 'Right Shift',
}


def print_reference():
    """Print a reference of common key codes."""
    print("\n" + "=" * 70)
    print("COMMON KEY CODES REFERENCE")
    print("=" * 70)
    
    print("\nLetters:")
    for code, name in sorted(KEY_CODE_REFERENCE.items()):
        if name.isalpha() and len(name) == 1:
            print(f"  {code:3d}: {name}", end="  ")
            if code % 5 == 0:
                print()
    
    print("\n\nNumbers:")
    for code, name in sorted(KEY_CODE_REFERENCE.items()):
        if name.isdigit():
            print(f"  {code:3d}: {name}", end="  ")
    
    print("\n\nSpecial Keys:")
    for code, name in sorted(KEY_CODE_REFERENCE.items()):
        if not name.isalnum() and len(name) > 1 and not name.startswith('F') and 'Cmd' not in name and 'Option' not in name and 'Control' not in name and 'Shift' not in name:
            print(f"  {code:3d}: {name}")
    
    print("\nFunction Keys:")
    for code, name in sorted(KEY_CODE_REFERENCE.items()):
        if name.startswith('F') and name[1:].isdigit():
            print(f"  {code:3d}: {name}", end="  ")
            if int(name[1:]) % 4 == 0:
                print()
    
    print("\n\nModifier Keys:")
    for code, name in sorted(KEY_CODE_REFERENCE.items()):
        if 'Cmd' in name or 'Option' in name or 'Control' in name or 'Shift' in name:
            print(f"  {code:3d}: {name}")
    
    print("\n" + "=" * 70 + "\n")


def main():
    """Main entry point."""
    print("=" * 70)
    print("KEY CODE DISCOVERY TOOL")
    print("=" * 70)
    print()
    print("This tool helps you discover key codes and modifier combinations")
    print("for setting up custom hotkeys.")
    print()
    print("Press any key (with or without modifiers) to see its details.")
    print("The tool will show you the Python code snippet to detect that key.")
    print()
    print("Press Ctrl+C to exit")
    print("=" * 70)
    
    # Print reference first
    print_reference()
    
    print("Monitoring started. Press keys now...\n")
    
    # Create and run the app
    app = NSApplication.sharedApplication()
    delegate = KeyCodeDiscovery.alloc().init()
    app.setDelegate_(delegate)
    
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        print("\n\nShutting down...")


if __name__ == '__main__':
    main()
