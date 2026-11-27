#!/usr/bin/env python3
"""Control client for the dictation daemon. Stdlib-only, fast to launch.

Usage: dictation-ctl.py {toggle|start|stop|status|preview|mode|info}
Meant to be bound in Hyprland: Hyper+D -> toggle, Hyper+Shift+D -> preview,
Hyper+Shift+M -> mode (live/batch).
"""

import os
import socket
import sys

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dictation-app.sock"
)
COMMANDS = ("toggle", "start", "stop", "status", "preview", "mode", "info")


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if command not in COMMANDS:
        print(f"usage: {os.path.basename(sys.argv[0])} {{{'|'.join(COMMANDS)}}}")
        return 2

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(SOCKET_PATH)
    except OSError as e:
        print(f"dictation daemon not running? ({e})")
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            os.system(
                "notify-send -a Dictation -u critical "
                "'Dictation daemon not running' 'Start it with: uv run dictation.py'"
            )
        client.close()
        return 1

    # Connected: the daemon is alive. A stop can take a few seconds on a slow
    # connection (it lets the sender finish, up to 3 s), so wait longer for the
    # reply, and never report a slow reply as "not running".
    try:
        client.settimeout(10)
        client.sendall(command.encode() + b"\n")
        response = client.recv(256).decode("utf-8", "replace").strip()
        print(response)
        return 0
    except OSError as e:
        print(f"dictation daemon is running but did not answer in time ({e})")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
