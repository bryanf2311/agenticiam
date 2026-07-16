"""Entry point for the GUI executable. With no arguments it starts the same
REST/OAuth2 server used by `agenticiam serve` (which also serves the admin
UI at `/`) and opens it in the default browser — double-clicking the GUI
executable is enough to get a working admin session with no terminal
involved.

With arguments, it behaves exactly like the CLI executable (`agenticiam
mcp`, `agenticiam init`, ...). This matters for the Goose "New Agent"
wizard: users who only downloaded the GUI executable (the "seamless" one)
still need something Goose can spawn as `<cmd> mcp` for the MCP stdio
extension — if the GUI exe only understood being double-clicked, that
extension entry would point at a program that doesn't do what it's told,
which is exactly what happened before this dispatch existed.
"""

import socket
import sys
import threading
import webbrowser


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) != 0


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless environment, no browser available — server still runs


def launch_gui(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    url = f"http://{host}:{port}/"

    if not _port_is_free(host, port):
        # Likely a previous launch of this same tool is already listening
        # here; just open the browser to it instead of failing to bind.
        if open_browser:
            _safe_open(url)
        return

    from .api import create_app

    app = create_app()
    if open_browser:
        threading.Timer(0.6, _safe_open, args=(url,)).start()
    app.run(host=host, port=port)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from .cli import main as cli_main

        cli_main(args=argv)
    else:
        launch_gui()
