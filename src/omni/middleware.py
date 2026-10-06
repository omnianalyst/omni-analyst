"""A request-body ceiling at the ASGI layer.

Constrained Pydantic fields refuse an oversized credential before any
handler runs, but they only protect the endpoints that declare them. This
middleware is the blanket for every route: a body over the ceiling is
refused with 413 before a handler, a parser, or the hashing executor sees
a byte of it.

The first version of this middleware watched the stream go by and answered
an overflow with ``http.disconnect`` -- no status, and the handler had
already consumed the earlier chunks (audit A06). This is a JSON API: the
correct contract is to buffer at most the configured ceiling BEFORE the
application is invoked, answer a definite 413/400/408 at the ASGI layer,
and replay one request event into the app when the body is within bounds.
That also closes the malformed-``Content-Length`` branch, which previously
bypassed the counter entirely: a bad length is a 400 here, and a declared
length that disagrees with the received body is a 400 too.

A slowly dripping body holds a request for at most ``read_timeout`` seconds
before the middleware answers 408 and releases it. This is deliberately a
bounded buffer, not transparent streaming: legitimate large uploads need
their own explicit route-level bound, not a wider global ceiling.
"""

from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse

DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_READ_TIMEOUT = 15.0


class MaxBodySizeMiddleware:
    def __init__(
        self,
        app,
        max_bytes: int = DEFAULT_MAX_BODY_BYTES,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        if max_bytes < 1 or read_timeout <= 0:
            raise ValueError("invalid body-limit configuration")
        self.app = app
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int, detail: str) -> None:
            await JSONResponse({"detail": detail}, status_code=status)(
                scope, receive, send
            )

        lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(lengths) > 1:
            return await reject(400, "invalid content length")
        declared: int | None = None
        if lengths:
            raw = lengths[0]
            if not raw or len(raw) > 20 or any(c < 48 or c > 57 for c in raw):
                return await reject(400, "invalid content length")
            declared = int(raw)
            if declared > self.max_bytes:
                return await reject(413, "request body too large")

        body = bytearray()
        try:
            async with asyncio.timeout(self.read_timeout):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        # The client is gone; there is nobody to answer.
                        return
                    if event["type"] != "http.request":
                        return await reject(400, "invalid request body")
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > self.max_bytes:
                        return await reject(413, "request body too large")
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "request body read timed out")

        if declared is not None and declared != len(body):
            return await reject(400, "content length mismatch")

        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)


__all__ = ["DEFAULT_MAX_BODY_BYTES", "MaxBodySizeMiddleware"]
