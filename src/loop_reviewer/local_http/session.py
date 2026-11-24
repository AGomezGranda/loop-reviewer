"""Protect the private local origin and bound requests before JSON parsing."""

from collections.abc import Callable, Coroutine
from hmac import compare_digest
from typing import Any, override

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from loop_reviewer.local_http.models import ApiError
from loop_reviewer.review.model import MAX_EDIT_BYTES

_MAX_BODY = 2 * 6 * MAX_EDIT_BYTES + 8192
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        error.body.model_dump(by_alias=True, exclude_none=True),
        status_code=error.status,
    )


async def require_empty_body(request: Request) -> None:
    if await request.body():
        raise ApiError(
            422, "invalid_input", "This action does not accept a request body."
        )


class JsonBodyRoute(APIRoute):
    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if self.body_field is None:
            return handler

        async def require_json(request: Request) -> Response:
            content_type = (
                request.headers.get("content-type", "")
                .partition(";")[0]
                .strip()
                .lower()
            )
            if content_type != "application/json" and await request.body():
                raise ApiError(
                    415, "unsupported_media_type", "Send a JSON request body."
                )
            return await handler(request)

        return require_json


class LocalSession:
    def __init__(self, app: ASGIApp, *, origin: str, capability: str) -> None:
        self._app = app
        self._origin = origin
        self._host = origin.removeprefix("http://")
        self._bearer = f"Bearer {capability}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        private = path == "/api" or path.startswith("/api/")
        started = False

        async def send_private(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Referrer-Policy"] = "no-referrer"
                headers["X-Content-Type-Options"] = "nosniff"
                # The root HTML must also come from the current server launch.
                headers["Cache-Control"] = "no-store"
                started = True
            await send(message)

        try:
            headers = Headers(scope=scope)
            if headers.getlist("host") != [self._host]:
                raise ApiError(403, "invalid_host", "Use the local launch address.")
            if private:
                auth = headers.getlist("authorization")
                if len(auth) != 1 or not compare_digest(
                    auth[0].encode("latin-1"), self._bearer
                ):
                    raise ApiError(
                        401,
                        "unauthorized",
                        "Open a fresh browser session with loop-reviewer ui.",
                    )
                if scope["method"] not in _SAFE_METHODS:
                    if headers.getlist("origin") != [self._origin]:
                        raise ApiError(
                            403,
                            "invalid_origin",
                            "The request must come from the local workspace.",
                        )
                    receive = await self._mutation_body(receive)
            await self._app(scope, receive, send_private)
        except ApiError as error:
            if not started:
                await error_response(error)(scope, receive, send_private)
        except Exception:  # noqa: BLE001
            if not started:
                error = ApiError(
                    500,
                    "internal_error",
                    "The local workspace could not complete the request.",
                )
                await error_response(error)(scope, receive, send_private)

    async def _mutation_body(self, receive: Receive) -> Receive:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":

                async def disconnected() -> Message:
                    return {"type": "http.disconnect"}

                return disconnected
            body.extend(message.get("body", b""))
            if len(body) > _MAX_BODY:
                raise ApiError(413, "body_too_large", "The request is too large.")
            if not message.get("more_body", False):
                break

        async def replay() -> Message:
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        pending = True
        return replay
