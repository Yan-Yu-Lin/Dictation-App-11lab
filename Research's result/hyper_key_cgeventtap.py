#!/usr/bin/env python3
"""
Alternative: Global Hotkey Detection using CGEventTap at Session Level
This approach uses CGEventTap with kCGSessionEventTap to catch events
after they've been transformed by Karabiner-Elements.

Requirements:
    pip install pyobjc-core pyobjc-framework-Quartz --break-system-packages

This is similar to what pynput does, but taps at the session level
which should see transformed events.
"""

import Quartz
from Cocoa import NSEvent
from PyObjCTools import AppHelper


# Modifier flag constants
kCGEventFlagMaskCommand = 1 << 20
kCGEventFlagMaskAlternate = 1 << 19  # Option key
kCGEventFlagMaskControl = 1 << 18
kCGEventFlagMaskShift = 1 << 17


def create_event_tap(handler_function):
    """
    Create a CGEventTap at the session level.
    This should receive events AFTER Karabiner-Elements transforms them.
    """
    # Create event mask for key down events
    event_mask = (
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) |
        Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
    )
    
    # Create the event tap at SESSION level (not HID level)
    # kCGSessionEventTap is higher level and should see transformed events
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,          # Tap at session level (higher)
        Quartz.kCGHeadInsertEventTap,       # Insert at head of event queue
        Quartz.kCGEventTapOptionListenOnly, # Listen only (don't modify)
        event_mask,
        handler_function,
        None  # refcon (user data)
    )
    
    return tap


def event_handler(proxy, event_type, event, refcon):
    """
    Handle CGEvents.
    This callback receives events after Karabiner-Elements transformation.
    """
    try:
        if event_type == Quartz.kCGEventKeyDown:
            # Get key code and modifiers
            key_code = Quartz.CGEventGetIntegerValueField(
                event,
                Quartz.kCGKeyboardEventKeycode
            )
            
            flags = Quartz.CGEventGetFlags(event)
            
            # Check for modifier keys
            cmd = bool(flags & kCGEventFlagMaskCommand)
            option = bool(flags & kCGEventFlagMaskAlternate)
            control = bool(flags & kCGEventFlagMaskControl)
            shift = bool(flags & kCGEventFlagMaskShift)
            
            # Get the character (requires converting CGEvent to NSEvent)
            ns_event = NSEvent.eventWithCGEvent_(event)
            if ns_event:
                char = ns_event.charactersIgnoringModifiers()
            else:
                char = "?"
            
            # Check for Cmd+Option+Control+D (hyper key + D)
            # D key has keycode 2
            if key_code == 2 and cmd and option and control and not shift:
                print("🎯 HOTKEY TRIGGERED: Cmd+Option+Control+D")
                print("✅ Hotkey action executed!")
                print("=" * 50)
                # Add your custom action here
                return event
            
            # Debug output
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
            print(f"Key: {char} (code: {key_code}) | Modifiers: {mod_display}")
    
    except Exception as e:
        print(f"Error in event handler: {e}")
    
    # Always return the event to pass it along
    return event


def main():
    """Main entry point."""
    print("=" * 60)
    print("CGEventTap Hyper Key Monitor (Session Level)")
    print("=" * 60)
    print("\nThis uses CGEventTap at kCGSessionEventTap level.")
    print("It should detect events AFTER Karabiner-Elements transforms them.\n")
    print("Target hotkey: Cmd+Option+Control+D")
    print("(Right Command + D if you've mapped Right Cmd to hyper)\n")
    print("Press Ctrl+C to quit.\n")
    
    # Create the event tap
    tap = create_event_tap(event_handler)
    
    if not tap:
        print("ERROR: Failed to create event tap!")
        print("Make sure:")
        print("1. Your app has Accessibility permissions")
        print("2. Go to System Settings > Privacy & Security > Accessibility")
        print("3. Add Terminal (or your Python app) to the list")
        return 1
    
    # Create a run loop source from the tap
    run_loop_source = Quartz.CFMachPortCreateRunLoopSource(
        None,  # allocator
        tap,   # tap
        0      # order
    )
    
    # Add to current run loop
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(),
        run_loop_source,
        Quartz.kCFRunLoopCommonModes
    )
    
    # Enable the tap
    Quartz.CGEventTapEnable(tap, True)
    
    print("Event tap installed and running...")
    
    # Run the event loop
    try:
        AppHelper.runConsoleEventLoop()
    except KeyboardInterrupt:
        print("\nShutting down...")
    
    return 0


if __name__ == '__main__':
    exit(main())
