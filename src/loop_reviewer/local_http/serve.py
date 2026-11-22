"""Launch the installed local workspace on a private loopback origin."""

import socket
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from secrets import token_urlsafe
from typing import override

import uvicorn
from fastapi import FastAPI

_ASSETS = Path(__file__).with_name("dist")


class BrowserServer(uvicorn.Server):
    def __init__(
        self,
        app: FastAPI,
        address: str,
        capability: str,
        open_browser: Callable[[str], bool],
    ) -> None:
        super().__init__(
            uvicorn.Config(
                app,
                proxy_headers=False,
                access_log=False,
                server_header=False,
                ws="none",
                log_level="critical",
            )
        )
        self._url = f"{address}/#capability={capability}"
        self._open_browser = open_browser
        self._stop_watch: Callable[[], None] = app.state.stop_watch

    @override
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            try:
                if self._open_browser(self._url):
                    return
            except OSError, webbrowser.Error:
                pass
            print(self._url, flush=True)

    @override
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._stop_watch()
        await super().shutdown(sockets)


def serve(
    create: Callable[[str, str, Path], FastAPI],
    *,
    open_browser: Callable[[str], bool] = webbrowser.open,
) -> int:
    if not (_ASSETS / "index.html").is_file() or not (_ASSETS / "assets").is_dir():
        print(
            "Browser bundle is missing. Run mise exec -- pnpm --dir web build from the checkout.",
            file=sys.stderr,
        )
        return 1
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            capability = token_urlsafe(32)
            server = BrowserServer(
                create(origin, capability, _ASSETS), origin, capability, open_browser
            )
            server.run(sockets=[listener])
            if not server.started:
                print("Could not start the local browser workspace.", file=sys.stderr)
                return 1
    except OSError:
        print("Could not bind the local browser workspace.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
