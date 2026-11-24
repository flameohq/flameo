from __future__ import annotations

from types import SimpleNamespace

import pytest

from flameo import Flameo


@pytest.fixture
def app_factory():
    apps = []

    def create(**kwargs):
        app = Flameo(**kwargs)
        apps.append(app)
        return app

    return create


async def asgi_request(app, method="GET", path="/", *, headers=None, body=b"", body_chunks=None, query=b""):
    events = []
    chunks = list(body_chunks) if body_chunks is not None else [body]
    if not chunks:
        chunks = [b""]
    chunk_index = 0
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                   for k, v in (headers or {}).items()]

    async def receive():
        nonlocal chunk_index
        if chunk_index >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[chunk_index]
        chunk_index += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": chunk_index < len(chunks),
        }

    async def send(event):
        events.append(event)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": raw_headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    await app(scope, receive, send)
    start = next((event for event in events if event["type"] == "http.response.start"), None)
    response_body = b"".join(
        event.get("body", b"") for event in events
        if event["type"] == "http.response.body"
    )
    response_headers = {}
    if start:
        for key, value in start.get("headers", []):
            response_headers.setdefault(key.decode("latin-1").lower(), []).append(
                value.decode("latin-1")
            )
    return {
        "status": start["status"] if start else None,
        "headers": response_headers,
        "body": response_body,
        "events": events,
    }


class RSGIReceive:
    def __init__(self, body=b"", chunks=None):
        self.chunks = list(chunks) if chunks is not None else [body]
        self.read_count = 0
        self.response = None
        self.stream = None

    def __await__(self):
        async def value():
            self.read_count += 1
            if self.read_count <= len(self.chunks):
                return self.chunks[self.read_count - 1]
            return b""
        return value().__await__()

    def response_bytes(self, *, status, headers, body):
        self.response = (status, headers, body)

    def response_stream(self, status, headers):
        self.stream = RSGIStream(status, headers)
        return self.stream


class RSGIStream:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers
        self.chunks = []

    async def send_bytes(self, chunk):
        self.chunks.append(bytes(chunk))


async def rsgi_request(app, method="GET", path="/", *, headers=None, body=b"", body_chunks=None, query=b""):
    receive = RSGIReceive(body, chunks=body_chunks)
    scope = SimpleNamespace(
        method=method,
        path=path,
        query_string=query,
        headers={k.lower().encode("latin-1"): v.encode("latin-1")
                 for k, v in (headers or {}).items()},
        scheme="http",
        server=("testserver", 80),
        client=("127.0.0.1", 12345),
    )
    result = await app.__rsgi__(scope, receive)
    if receive.response is not None:
        status, headers_out, response_body = receive.response
    elif result is not None:
        status, headers_out, chunks = result
        response_body = b"".join(chunks)
    else:
        status, headers_out, response_body = None, [], b""
    normalized_headers = {}
    for key, value in headers_out:
        normalized_headers.setdefault(key.lower(), []).append(value)
    return {
        "status": status,
        "headers": normalized_headers,
        "body": response_body,
        "stream": receive.stream,
    }


@pytest.fixture
def asgi_client():
    return asgi_request


@pytest.fixture
def rsgi_client():
    return rsgi_request
