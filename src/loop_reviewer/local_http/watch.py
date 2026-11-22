"""Share one scan membership across live browser streams."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractContextManager
from typing import Literal

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from loop_reviewer.inbox.polling import ScanCycle
from loop_reviewer.local_http.models import ResponseModel

Event = Literal["ready", "scan-finished", "heartbeat"]


class WatchEvent(ResponseModel):
    event: Event


def _line(event: Event) -> str:
    return WatchEvent(event=event).model_dump_json() + "\n"


class BrowserWatch:
    def __init__(
        self,
        join: Callable[[], AbstractContextManager[None]],
        tick: Callable[[], None],
        cycle: Callable[[], ScanCycle | None],
    ) -> None:
        self._join = join
        self._tick = tick
        self._cycle = cycle
        self._membership: AbstractContextManager[None] | None = None
        self._listeners: set[asyncio.Queue[bool]] = set()
        self._closed = False
        self._polling: asyncio.Task[None] | None = None

    async def events(self) -> AsyncGenerator[str]:
        if self._closed:
            return
        listener: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
        if not self._listeners:
            membership = self._join()
            membership.__enter__()
            self._membership = membership
            if self._polling is None or self._polling.done():
                self._polling = asyncio.create_task(self._poll())
        self._listeners.add(listener)
        try:
            yield _line("ready")
            while True:
                try:
                    changed = await asyncio.wait_for(listener.get(), 2)
                except TimeoutError:
                    yield _line("heartbeat")
                else:
                    if not changed:
                        return
                    yield _line("scan-finished")
        finally:
            self._listeners.remove(listener)
            if not self._listeners and self._membership is not None:
                self._membership.__exit__(None, None, None)
                self._membership = None

    async def _poll(self) -> None:
        previous: ScanCycle | None = None
        try:
            while self._listeners:
                await run_in_threadpool(self._tick)
                current = self._cycle()
                if current != previous and current is not None and current.number:
                    self._notify(True)
                previous = current
                await asyncio.sleep(1)
        except Exception:  # noqa: BLE001
            self._notify(False)

    def _notify(self, changed: bool) -> None:
        for listener in self._listeners:
            if not changed and listener.full():
                listener.get_nowait()
            if not listener.full():
                listener.put_nowait(changed)

    def stop(self) -> None:
        self._closed = True
        self._notify(False)

    async def drain(self) -> None:
        if self._polling is not None:
            await self._polling


class WatchResponse(StreamingResponse):
    def __init__(self, events: AsyncGenerator[str]) -> None:
        super().__init__(events, media_type="application/x-ndjson")
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def bounded(message: Message) -> None:
            await asyncio.wait_for(send(message), 5)

        try:
            await super().__call__(scope, receive, bounded)
        finally:
            await self._events.aclose()
