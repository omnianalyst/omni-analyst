"""The ASGI body ceiling (audit finding A06).

The first version watched the stream go by and answered an overflow with
``http.disconnect``: no status, the handler had already consumed earlier
chunks, and a malformed Content-Length bypassed the counter entirely. These
tests pin the replacement contract at the ASGI boundary: a definite 413 for
oversized bodies (declared or streamed), 400 for a length that lies, 408 for
a body that never finishes, and byte-exact passthrough at the limit.
"""

from __future__ import annotations

import asyncio

import pytest

from omni.middleware import MaxBodySizeMiddleware

LIMIT = 64


class _Recorder:
    """A minimal ASGI app that captures what the middleware hands it."""

    def __init__(self):
        self.events: list[dict] = []
        self.response: dict | None = None
        self.started = False

    async def __call__(self, scope, receive, send):
        self.started = True
        while True:
            event = await receive()
            self.events.append(event)
            if event["type"] != "http.request" or not event.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _drive(app, events, headers=None, *, method=b"POST"):
    sent: list[dict] = []

    async def receive():
        if events:
            return events.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "headers": headers or [],
        "path": "/",
        "query_string": b"",
    }
    await app(scope, receive, send)
    return sent


def _status(sent) -> int | None:
    for message in sent:
        if message["type"] == "http.response.start":
            return message["status"]
    return None


@pytest.fixture
def recorder():
    return _Recorder()


@pytest.fixture
def wrapped(recorder):
    return MaxBodySizeMiddleware(recorder, max_bytes=LIMIT, read_timeout=5.0)


class TestInBoundBodies:
    async def test_exact_limit_passes_and_is_replayed_once(self, wrapped, recorder):
        body = b"x" * LIMIT
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": body, "more_body": False}],
            headers=[(b"content-length", str(LIMIT).encode())],
        )
        assert _status(sent) == 200
        assert recorder.events == [
            {"type": "http.request", "body": body, "more_body": False}
        ]

    async def test_chunked_body_within_limit_is_reassembled(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [
                {"type": "http.request", "body": b"ab", "more_body": True},
                {"type": "http.request", "body": b"cd", "more_body": True},
                {"type": "http.request", "body": b"ef", "more_body": False},
            ],
        )
        assert _status(sent) == 200
        assert recorder.events == [
            {"type": "http.request", "body": b"abcdef", "more_body": False}
        ]


class TestOversizedBodies:
    async def test_declared_over_limit_is_413_before_any_body(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}],
            headers=[(b"content-length", str(LIMIT + 1).encode())],
        )
        assert _status(sent) == 413
        assert not recorder.started, "the handler ran for an oversized body"

    async def test_streamed_over_limit_is_413_not_a_disconnect(self, wrapped, recorder):
        # The A06 core: the old middleware answered http.disconnect here and
        # the handler saw five bytes then silence.
        sent = await _drive(
            wrapped,
            [
                {"type": "http.request", "body": b"x" * 32, "more_body": True},
                {"type": "http.request", "body": b"x" * 32, "more_body": True},
                {"type": "http.request", "body": b"x", "more_body": False},
            ],
        )
        assert _status(sent) == 413
        assert not recorder.started

    async def test_limit_plus_one_byte_is_413(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"x" * (LIMIT + 1), "more_body": False}],
        )
        assert _status(sent) == 413


class TestMalformedLengths:
    async def test_non_numeric_length_is_400_not_a_bypass(self, wrapped, recorder):
        # The old code fell through to the streaming branch and delivered
        # every byte past the limit for a non-numeric Content-Length.
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"x" * (LIMIT + 5), "more_body": False}],
            headers=[(b"content-length", b"not-a-number")],
        )
        assert _status(sent) == 400
        assert not recorder.started

    async def test_negative_length_is_400(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"tiny", "more_body": False}],
            headers=[(b"content-length", b"-5")],
        )
        assert _status(sent) == 400

    async def test_duplicate_length_headers_are_400(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"tiny", "more_body": False}],
            headers=[
                (b"content-length", b"4"),
                (b"content-length", b"4"),
            ],
        )
        assert _status(sent) == 400

    async def test_a_length_below_the_received_body_is_400(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"0123456789", "more_body": False}],
            headers=[(b"content-length", b"8")],
        )
        assert _status(sent) == 400
        assert not recorder.started

    async def test_a_length_above_the_received_body_is_400(self, wrapped, recorder):
        sent = await _drive(
            wrapped,
            [{"type": "http.request", "body": b"01", "more_body": False}],
            headers=[(b"content-length", b"8")],
        )
        assert _status(sent) == 400


class TestClientGoneAndStalled:
    async def test_client_disconnect_answers_nothing(self, wrapped, recorder):
        sent = await _drive(wrapped, [{"type": "http.disconnect"}])
        assert sent == []
        assert not recorder.started

    async def test_a_stalled_body_times_out_with_408(self, recorder):
        wrapped = MaxBodySizeMiddleware(recorder, max_bytes=LIMIT, read_timeout=0.05)
        gate = asyncio.Event()

        async def receive():
            await gate.wait()
            return {"type": "http.request", "body": b"x", "more_body": True}

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "headers": [], "method": b"POST"}
        await asyncio.wait_for(
            wrapped(scope, receive, send), timeout=2
        )
        assert _status(sent) == 408
        assert not recorder.started

    async def test_non_http_scopes_pass_through_untouched(self, recorder):
        seen: dict = {}

        async def app(scope, receive, send):
            seen["scope"] = scope

        wrapped = MaxBodySizeMiddleware(app, max_bytes=LIMIT)
        await wrapped({"type": "websocket"}, None, None)
        assert seen["scope"]["type"] == "websocket"


class TestConfiguration:
    def test_invalid_limits_are_refused_at_construction(self):
        with pytest.raises(ValueError):
            MaxBodySizeMiddleware(_Recorder(), max_bytes=0)
        with pytest.raises(ValueError):
            MaxBodySizeMiddleware(_Recorder(), read_timeout=0)
