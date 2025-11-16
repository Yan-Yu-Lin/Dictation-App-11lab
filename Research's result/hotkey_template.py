#!/usr/bin/env python3
"""
CUSTOMIZABLE HOTKEY TEMPLATE

Easy-to-customize template for detecting global hotkeys on macOS
that works with Karabiner-Elements hyper key transformations.

SETUP:
1. pip3 install pyobjc-core pyobjc-framework-Cocoa --break-system-packages
2. Grant Accessibility permissions to Terminal
3. Customize the CONFIGURATION section below
4. Run: python3 hotkey_template.py

Author: Generated for macOS hotkey detection
"""

from AppKit import NSApplication, NSApp
from Foundation import NSObject
from Cocoa import NSEvent, NSKeyDownMask, NSEventModifierFlagCommand, \
                  NSEventModifierFlagOption, NSEventModifierFlagControl, \
                  NSEventModifierFlagShift
from PyObjCTools import AppHelper
import subprocess


# ============================================================================
# CONFIGURATION - CUSTOMIZE THIS SECTION
# ============================================================================

# Define your hotkeys here
# Format: {
#     'name': 'Description',
#     'key': 'a',  # The key character (lowercase)
#     'modifiers': {
#         'cmd': True/False,
#         'option': True/False, 
#         'control': True/False,
#         'shift': True/False
#     },
#     'action': function_name  # Function to call (defined below)
# }

def open_safari():
    """Open Safari browser."""
    subprocess.run(['open', '-a', 'Safari'])

def open_terminal():
    """Open Terminal."""
    subprocess.run(['open', '-a', 'Terminal'])

def paste_text():
    """Paste text using system keystroke."""
    subprocess.run(['osascript', '-e', 
        'tell app "System Events" to keystroke "v" using command down'])

def custom_action():
    """Your custom Python action."""
    print("🎯 Custom action triggered!")
    # Add your Python code here
    # Examples:
    # - Run a script
    # - Open a file
    # - Make an API call
    # - Send a notification
    # - Anything Python can do!

# Define your hotkeys
HOTKEYS = [
    {
        'name': 'Hyper+D - Open Safari',
        'key': 'd',
        'modifiers': {
            'cmd': True,
            'option': True,
            'control': True,
            'shift': False
        },
        'action': open_safari
    },
    {
        'name': 'Hyper+T - Open Terminal',
        'key': 't',
        'modifiers': {
            'cmd': True,
            'option': True,
            'control': True,
            'shift': False
        },
        'action': open_terminal
    },
    {
        'name': 'Hyper+Shift+V - Paste',
        'key': 'v',
        'modifiers': {
            'cmd': True,
            'option': True,
            'control': True,
            'shift': True
        },
        'action': paste_text
    },
    {
        'name': 'Hyper+C - Custom Action',
        'key': 'c',
        'modifiers': {
            'cmd': True,
            'option': True,
            'control': True,
            'shift': False
        },
        'action': custom_action
    },
]

# Enable debug mode to see all key presses
DEBUG_MODE = False  # Set to True to see what keys are being pressed

# ============================================================================
# END OF CONFIGURATION
# ============================================================================


class HotkeyManager(NSObject):
    """Manages global hotkey detection."""
    
    def init(self):
        self = super(HotkeyManager, self).init()
        if self is None:
            return None
        return self
    
    def applicationDidFinishLaunching_(self, notification):
        """Set up event monitoring."""
        print("=" * 70)
        print("HOTKEY MANAGER")
        print("=" * 70)
        print("\nConfigured Hotkeys:")
        for hotkey in HOTKEYS:
            mods = []
            if hotkey['modifiers'].get('cmd'):
                mods.append('Cmd')
            if hotkey['modifiers'].get('option'):
                mods.append('Option')
            if hotkey['modifiers'].get('control'):
                mods.append('Control')
            if hotkey['modifiers'].get('shift'):
                mods.append('Shift')
            
            mod_str = '+'.join(mods) if mods else 'No modifiers'
            print(f"  • {hotkey['name']}")
            print(f"    Keys: {mod_str}+{hotkey['key'].upper()}")
        
        print("\n" + "=" * 70)
        print("Monitoring started. Press your hotkeys!")
        print("Press Cmd+Q to quit.\n")
        
        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask,
            self.handleKeyEvent_
        )
    
    def handleKeyEvent_(self, event):
        """Handle key events and check against configured hotkeys."""
        key_char = event.charactersIgnoringModifiers()
        if not key_char:
            return
        
        key_char = key_char.lower()
        modifiers = event.modifierFlags()
        
        # Extract current modifier state
        current_mods = {
            'cmd': bool(modifiers & NSEventModifierFlagCommand),
            'option': bool(modifiers & NSEventModifierFlagOption),
            'control': bool(modifiers & NSEventModifierFlagControl),
            'shift': bool(modifiers & NSEventModifierFlagShift)
        }
        
        # Debug mode: print all keys
        if DEBUG_MODE:
            active_mods = [k.title() for k, v in current_mods.items() if v]
            mod_str = '+'.join(active_mods) if active_mods else 'None'
            print(f"[DEBUG] Key: {key_char.upper()} | Modifiers: {mod_str}")
        
        # Check each configured hotkey
        for hotkey in HOTKEYS:
            if key_char == hotkey['key']:
                # Check if all modifiers match
                if all(current_mods[k] == v for k, v in hotkey['modifiers'].items()):
                    print(f"✅ Triggered: {hotkey['name']}")
                    try:
                        hotkey['action']()
                    except Exception as e:
                        print(f"❌ Error executing action: {e}")
                    return


def main():
    """Main entry point."""
    if not HOTKEYS:
        print("ERROR: No hotkeys configured!")
        print("Please edit this file and add hotkeys to the HOTKEYS list.")
        return 1
    
    # Validate hotkey configuration
    for i, hotkey in enumerate(HOTKEYS):
        if 'key' not in hotkey or 'modifiers' not in hotkey or 'action' not in hotkey:
            print(f"ERROR: Hotkey {i} is missing required fields!")
            print("Required: 'key', 'modifiers', 'action'")
            return 1
    
    # Create and run the app
    app = NSApplication.sharedApplication()
    delegate = HotkeyManager.alloc().init()
    app.setDelegate_(delegate)
    
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    return 0


if __name__ == '__main__':
    exit(main())
