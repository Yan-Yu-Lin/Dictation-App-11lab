#!/usr/bin/env python3
"""Floating dictation status indicator for Wayland (GTK4 + layer-shell).

A small dark capsule anchored top-center. Idle sessions show just a steady
red dot (recording) or orange dot (finalizing); as partial transcripts stream
in, the capsule widens to preview what is being transcribed, then wraps onto
more lines. When the text outgrows MAX_LINES, the oldest words are dropped so
the newest stay visible. Matches the macOS overlay.

Runs as a child of the dictation daemon and reads commands on stdin:
    recording | finalizing | hide | quit | partial <text> | notice <text>
    | preview on | preview off
Uses the SYSTEM python3 (needs python-gobject + gtk4-layer-shell packages) and
must be launched with LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so.
"""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

PANEL_HEIGHT = 30
DOT_SIZE = 10
TOP_MARGIN = 18
MAX_TEXT_WIDTH = 460  # px; longer text wraps onto the next line
MAX_LINES = 4  # beyond this, older words scroll away (dropped at the head)
NOTICE_MS = 1400  # how long a "預覽 開/關" notice stays up

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
}
#dot.recording {
    background-color: #ff453a;
}
#dot.finalizing {
    background-color: #ff9f0a;
}
#dot.notice {
    background-color: #0a84ff;
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
        # The dot stays on the first line when the preview wraps.
        self.dot.set_valign(Gtk.Align.START)
        self.dot.set_margin_top((PANEL_HEIGHT - DOT_SIZE) // 2)
        self.dot.set_margin_start((PANEL_HEIGHT - DOT_SIZE) // 2)
        self.panel.append(self.dot)

        self.preview = Gtk.Label()
        self.preview.set_name("preview")
        self.preview.set_valign(Gtk.Align.CENTER)
        self.preview.set_xalign(0.0)
        self.preview.set_wrap(True)
        self.preview.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.preview.set_size_request(0, -1)
        self.preview.set_visible(False)
        self.panel.append(self.preview)

        self.window.set_child(self.panel)
        self.window.connect("realize", self._make_click_through)

        self.text_width = 0  # current width of the preview label
        self.text_height = 0
        self.mode = "hidden"  # hidden / recording / finalizing
        self.preview_text = ""  # latest partial, kept even while preview is off
        self.preview_enabled = True
        self.notice_token = 0
        self.notice_active = False

    def _make_click_through(self, _widget):
        surface = self.window.get_surface()
        if surface is not None:
            try:
                import cairo

                surface.set_input_region(cairo.Region())
            except Exception:
                pass

    # --- preview size (no animation: instant resize reads cleaner) ---------

    def _layout_size(self, text: str, width: int = -1) -> tuple[int, int]:
        layout = self.preview.create_pango_layout(text)
        if width > 0:
            layout.set_width(width * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        return layout.get_pixel_size()

    def _line_height(self) -> int:
        return self._layout_size("Ag測")[1]

    def _fit_text(self, text: str) -> str:
        """Size the label for text; drop the oldest words past MAX_LINES."""
        width, height = self._layout_size(text)
        if width + 6 <= MAX_TEXT_WIDTH:
            self.text_width = width + 6
            self.text_height = height
            return text

        self.text_width = MAX_TEXT_WIDTH
        max_height = self._line_height() * MAX_LINES
        inner = MAX_TEXT_WIDTH - 6
        if self._layout_size(text, inner)[1] <= max_height:
            self.text_height = self._layout_size(text, inner)[1]
            return text

        # Binary-search the smallest cut that fits, keeping the newest words.
        low, high = 1, len(text) - 1
        while low < high:
            mid = (low + high) // 2
            if self._layout_size("…" + text[mid:], inner)[1] <= max_height:
                high = mid
            else:
                low = mid + 1
        text = "…" + text[low:]
        self.text_height = self._layout_size(text, inner)[1]
        return text

    def _set_text(self, text: str):
        """Show text in the capsule, or collapse to just the dot when empty."""
        if text:
            self.preview.set_text(self._fit_text(text))
        else:
            self.preview.set_text("")
            self.text_width = 0
            self.text_height = 0
        self._apply_size()

    def _apply_size(self):
        width = max(0, int(self.text_width))
        if width <= 0:
            self.preview.set_visible(False)
            self.preview.set_size_request(0, -1)
            # margin_start on dot centers it again inside the bare circle
            self.panel.set_size_request(PANEL_HEIGHT, PANEL_HEIGHT)
        else:
            self.preview.set_visible(True)
            self.preview.set_size_request(width, -1)
            line = self._line_height()
            height = PANEL_HEIGHT + max(0, self.text_height - line)
            self.preview.set_margin_top((PANEL_HEIGHT - line) // 2)
            self.preview.set_margin_bottom((PANEL_HEIGHT - line) // 2)
            self.panel.set_size_request(PANEL_HEIGHT + width + 12, height)
        # Shrink the layer surface back down after a wide/tall preview.
        self.window.set_default_size(1, 1)

    def _set_dot(self, css_class):
        for name in ("recording", "finalizing", "notice"):
            self.dot.remove_css_class(name)
        self.dot.add_css_class(css_class)

    def _render_session(self):
        """Redraw the recording/finalizing view from the current state."""
        self._set_dot("finalizing" if self.mode == "finalizing" else "recording")
        self._set_text(self.preview_text if self.preview_enabled else "")
        self.window.present()

    def _cancel_notice(self):
        self.notice_token += 1
        self.notice_active = False

    # --- commands ---------------------------------------------------------

    def show_recording(self):
        self._cancel_notice()
        self.mode = "recording"
        self.preview_text = ""
        self._render_session()

    def show_finalizing(self):
        self.mode = "finalizing"
        if not self.notice_active:
            self._render_session()

    def show_partial(self, text: str):
        text = text.strip()
        if not text:
            return
        self.preview_text = text
        if self.preview_enabled and not self.notice_active and self.mode != "hidden":
            self._set_text(text)

    def set_preview(self, enabled: bool):
        self.preview_enabled = enabled
        if self.mode != "hidden" and not self.notice_active:
            self._render_session()

    def show_notice(self, text: str):
        """Briefly show a message (e.g. preview toggled), then go back."""
        self.notice_token += 1
        token = self.notice_token
        self.notice_active = True
        self._set_dot("notice")
        self._set_text(text)
        self.window.present()
        GLib.timeout_add(NOTICE_MS, self._end_notice, token)

    def _end_notice(self, token: int):
        if token != self.notice_token:
            return False  # a newer notice or a new recording took over
        self.notice_active = False
        if self.mode == "hidden":
            self.hide()
        else:
            self._render_session()
        return False

    def hide(self):
        self.mode = "hidden"
        self.preview_text = ""
        if self.notice_active:
            return  # let the notice finish; _end_notice hides afterwards
        self.window.set_visible(False)
        self.preview.set_text("")
        self.text_width = 0
        self.text_height = 0
        self._apply_size()


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

        fd = sys.stdin.fileno()
        os.set_blocking(fd, False)
        pending = bytearray()

        def run_command(command: str) -> bool:
            """Apply one command; False means quit."""
            dot = holder["dot"]
            try:
                if command == "recording":
                    dot.show_recording()
                elif command == "finalizing":
                    dot.show_finalizing()
                elif command == "hide":
                    dot.hide()
                elif command == "quit":
                    return False
                elif command.startswith("partial "):
                    dot.show_partial(command[len("partial "):])
                elif command.startswith("notice "):
                    dot.show_notice(command[len("notice "):])
                elif command == "preview on":
                    dot.set_preview(True)
                elif command == "preview off":
                    dot.set_preview(False)
            except Exception as e:  # never let one bad command kill the dot
                print(f"dictation-dot: {command!r} failed: {e}", file=sys.stderr)
            return True

        def on_stdin(_fd, condition):
            # Read the raw fd until it is empty and run every complete line.
            # (A buffered sys.stdin.readline() here would leave extra lines in
            # Python's buffer, where this fd watch never sees them.)
            eof = False
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    eof = True
                    break
                if not chunk:
                    eof = True
                    break
                pending.extend(chunk)
            while b"\n" in pending:
                line, _, rest = bytes(pending).partition(b"\n")
                pending[:] = rest
                if not run_command(line.decode("utf-8", "replace")):
                    gtk_app.quit()
                    return False
            if eof or condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
                gtk_app.quit()  # the daemon is gone: don't leave a dot behind
                return False
            return True

        GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            fd,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            on_stdin,
        )

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
