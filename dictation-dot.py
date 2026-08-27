#!/usr/bin/env python3
"""Floating dictation status indicator for Wayland (GTK4 + layer-shell).

A small dark capsule anchored top-center. Idle sessions show just a pulsing
red dot (recording) or orange dot (finalizing); as partial transcripts stream
in, the capsule fluidly widens to preview what is being transcribed.

Runs as a child of the dictation daemon and reads commands on stdin:
    recording | finalizing | hide | quit | partial <text>
Uses the SYSTEM python3 (needs python-gobject + gtk4-layer-shell packages) and
must be launched with LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

PANEL_HEIGHT = 30
DOT_SIZE = 10
TOP_MARGIN = 18
MAX_TEXT_WIDTH = 460  # px; older words scroll away (ellipsized at the start)

CSS = b"""
window {
    background: transparent;
}
#panel {
    background-color: rgba(5, 5, 8, 0.96);
    border-radius: 15px;
}
#dot {
    border-radius: 5px;
    transition: background-color 200ms ease;
}
#dot.recording {
    background-color: #ff453a;
    animation: pulse 1.6s ease-in-out infinite;
}
#dot.finalizing {
    background-color: #ff9f0a;
    animation: none;
}
@keyframes pulse {
    0%   { opacity: 1.0; }
    50%  { opacity: 0.35; }
    100% { opacity: 1.0; }
}
#preview {
    color: rgba(235, 235, 240, 0.92);
    font-size: 13px;
    padding-right: 2px;
}
"""


class StatusDot:
    def __init__(self, app):
        self.window = Gtk.Window(application=app)
        self.window.set_resizable(False)

        LayerShell.init_for_window(self.window)
        LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
        LayerShell.set_anchor(self.window, LayerShell.Edge.TOP, True)
        LayerShell.set_margin(self.window, LayerShell.Edge.TOP, TOP_MARGIN)
        LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
        LayerShell.set_namespace(self.window, "dictation-dot")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.window.get_display(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.panel.set_name("panel")
        self.panel.set_size_request(PANEL_HEIGHT, PANEL_HEIGHT)

        self.dot = Gtk.Box()
        self.dot.set_name("dot")
        self.dot.set_size_request(DOT_SIZE, DOT_SIZE)
        self.dot.set_halign(Gtk.Align.CENTER)
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.set_margin_start((PANEL_HEIGHT - DOT_SIZE) // 2)
        self.panel.append(self.dot)

        self.preview = Gtk.Label()
        self.preview.set_name("preview")
        self.preview.set_valign(Gtk.Align.CENTER)
        self.preview.set_xalign(1.0)  # keep the newest words visible
        self.preview.set_single_line_mode(True)
        self.preview.set_ellipsize(Pango.EllipsizeMode.START)
        self.preview.set_size_request(0, -1)
        self.preview.set_visible(False)
        self.panel.append(self.preview)

        self.window.set_child(self.panel)
        self.window.connect("realize", self._make_click_through)

        self.text_width = 0  # current width of the preview label

    def _make_click_through(self, _widget):
        surface = self.window.get_surface()
        if surface is not None:
            try:
                import cairo

                surface.set_input_region(cairo.Region())
            except Exception:
                pass

    # --- preview width (no animation: instant resize reads cleaner) --------

    def _measure_text_width(self) -> int:
        layout = self.preview.create_pango_layout(self.preview.get_text())
        width, _height = layout.get_pixel_size()
        return min(width + 6, MAX_TEXT_WIDTH)

    def _apply_width(self):
        width = max(0, int(self.text_width))
        if width <= 0:
            self.preview.set_visible(False)
            self.preview.set_size_request(0, -1)
            # margin_start on dot centers it again inside the bare circle
            self.panel.set_size_request(PANEL_HEIGHT, PANEL_HEIGHT)
        else:
            self.preview.set_visible(True)
            self.preview.set_size_request(width, -1)
            self.panel.set_size_request(
                PANEL_HEIGHT + width + 12, PANEL_HEIGHT
            )

    # --- commands ---------------------------------------------------------

    def _set_state(self, css_class):
        self.dot.remove_css_class("recording")
        self.dot.remove_css_class("finalizing")
        self.dot.add_css_class(css_class)

    def show_recording(self):
        self.preview.set_text("")
        self.text_width = 0
        self._apply_width()
        self._set_state("recording")
        self.window.present()

    def show_finalizing(self):
        self._set_state("finalizing")
        self.window.present()

    def show_partial(self, text: str):
        text = text.strip()
        if not text:
            return
        self.preview.set_text(text)
        self.text_width = self._measure_text_width()
        self._apply_width()

    def hide(self):
        self.window.set_visible(False)
        self.preview.set_text("")
        self.text_width = 0
        self._apply_width()


def main():
    # NON_UNIQUE: each daemon owns its own dot; without this a second launch
    # silently defers to the first instance and exits 0
    app = Gtk.Application(
        application_id="dev.arthurlin.dictation-dot",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    holder = {}

    def on_activate(gtk_app):
        holder["dot"] = StatusDot(gtk_app)
        # Keep the app alive while the window is hidden
        gtk_app.hold()

        def on_stdin(channel, _condition):
            line = sys.stdin.readline()
            if not line:  # daemon died, EOF
                gtk_app.quit()
                return False
            command = line.rstrip("\n")
            dot = holder["dot"]
            if command == "recording":
                dot.show_recording()
            elif command == "finalizing":
                dot.show_finalizing()
            elif command == "hide":
                dot.hide()
            elif command == "quit":
                gtk_app.quit()
                return False
            elif command.startswith("partial "):
                dot.show_partial(command[len("partial "):])
            return True

        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, on_stdin)

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
