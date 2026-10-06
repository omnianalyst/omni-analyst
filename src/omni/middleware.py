"""A request-body ceiling at the ASGI layer.

Constrained Pydantic fields refuse an oversized credential before any
handler runs, but they only protect the endpoints that declare them. This
middleware is the blanket: any request whose body exceeds the ceiling is
refused with 413 before a handler, a parser, or the hashing executor sees a
byte of it. Content-Length is checked up front; a streaming body without one
is cut off at the same bound while it is read.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

DEFAULT_MAX_BODY_BYTES = 1024 * 1024


class MaxBodySizeMiddleware:
    def __init__(self, app, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = Request(scope).headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    response = JSONResponse(
                        {"detail": "request body too large"}, status_code=413
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                # A non-numeric Content-Length never reaches a handler anyway;
                # let the server's own malformed-request handling answer it.
                await self.app(scope, receive, send)
                return

        received = 0

        async def _bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, _bounded_receive, send)


__all__ = ["DEFAULT_MAX_BODY_BYTES", "MaxBodySizeMiddleware"]
