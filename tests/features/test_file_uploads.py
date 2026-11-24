import asyncio
import json

import pytest


def _multipart(name="file", filename="upload.bin", payload=b"payload", boundary="flameo"):
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()


def _headers(boundary="flameo"):
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}


async def _wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


def _register_endpoint(app, kind, path, handler):
    if kind == "app":
        app.post(path)(handler)
        return path

    from flameo import Router

    router = Router()
    router.post(path)(handler)
    app.use_router("/api", router)
    return "/api" + path


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("endpoint_kind", ["app", "router"])
async def test_small_upload_stays_in_ram_and_is_cleaned_after_response(
    protocol, endpoint_kind, app_factory, asgi_client, rsgi_client, tmp_path, monkeypatch,
):
    import flameo.parser as parser

    parser_dir = tmp_path / "spills"
    monkeypatch.setattr(parser, "TEMP_DIR", parser_dir)
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    captured = {}

    async def upload(req, res):
        uploaded = (await req.files)["file"]
        captured.update(
            file=uploaded,
            content=uploaded.content,
            temp_path=uploaded.temp_path,
            size=uploaded.size,
        )
        await res.json({"size": uploaded.size})

    path = _register_endpoint(app, endpoint_kind, "/small", upload)
    response = await client(
        app, "POST", path, headers=_headers(), body=_multipart(payload=b"in-memory"),
    )

    assert response["status"] == 200
    assert json.loads(response["body"]) == {"size": 9}
    assert captured["content"] == b"in-memory"
    assert captured["temp_path"] is None
    assert captured["file"].size == 9

    await _wait_for(lambda: captured["file"].content is None)
    assert captured["file"].temp_path is None
    assert list(parser_dir.glob("upload_*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("endpoint_kind", ["app", "router"])
async def test_large_upload_spills_to_disk_and_is_cleaned_after_response(
    protocol, endpoint_kind, app_factory, asgi_client, rsgi_client, tmp_path, monkeypatch,
):
    import flameo.parser as parser

    parser_dir = tmp_path / "spills"
    monkeypatch.setattr(parser, "TEMP_DIR", parser_dir)
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 8)
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    captured = {}
    payload = b"x" * 32

    async def upload(req, res):
        uploaded = (await req.files)["file"]
        captured.update(file=uploaded, temp_path=uploaded.temp_path)
        await res.json({"size": uploaded.size})

    path = _register_endpoint(app, endpoint_kind, "/large", upload)
    response = await client(
        app, "POST", path, headers=_headers(), body=_multipart(payload=payload),
    )

    assert response["status"] == 200
    assert json.loads(response["body"]) == {"size": 32}
    assert captured["file"].content is None
    assert captured["temp_path"] is not None
    assert captured["temp_path"].read_bytes() == payload

    await _wait_for(lambda: captured["file"].temp_path is None)
    assert not captured["temp_path"].exists()
    assert list(parser_dir.glob("upload_*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("endpoint_kind", ["app", "router"])
async def test_upload_to_persists_large_upload_and_skips_cleanup(
    protocol, endpoint_kind, app_factory, asgi_client, rsgi_client, tmp_path, monkeypatch,
):
    import flameo.parser as parser

    parser_dir = tmp_path / "spills"
    destination = tmp_path / "saved"
    parser_dir.mkdir()
    destination.mkdir()
    monkeypatch.setattr(parser, "TEMP_DIR", parser_dir)
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 8)
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    captured = {}
    payload = b"persist-me" * 4

    async def upload(req, res):
        uploaded = (await req.files)["file"]
        saved = await uploaded.upload_to(str(destination), name="saved.bin")
        captured.update(file=uploaded, path=saved)
        await res.json({"name": saved.name, "size": saved.stat().st_size})

    path = _register_endpoint(app, endpoint_kind, "/persist", upload)
    response = await client(
        app, "POST", path, headers=_headers(), body=_multipart(payload=payload),
    )

    assert response["status"] == 200
    assert json.loads(response["body"]) == {"name": "saved.bin", "size": len(payload)}
    assert captured["path"].read_bytes() == payload
    assert captured["file"].temp_path is None

    await asyncio.sleep(0)
    assert captured["path"].exists()
    assert captured["path"].read_bytes() == payload
    assert list(parser_dir.glob("upload_*.part")) == []


@pytest.mark.asyncio
async def test_concurrent_request_upload_cleanup_is_isolated(
    app_factory, asgi_client, tmp_path, monkeypatch,
):
    import flameo.parser as parser

    parser_dir = tmp_path / "spills"
    monkeypatch.setattr(parser, "TEMP_DIR", parser_dir)
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 8)
    app = app_factory()
    parsed = asyncio.Event()
    release = asyncio.Event()
    captured = {}
    payload = b"y" * 32

    @app.post("/hold")
    async def hold(req, res):
        uploaded = (await req.files)["file"]
        captured["hold"] = uploaded
        parsed.set()
        await release.wait()
        await res.send("held")

    @app.post("/quick")
    async def quick(req, res):
        captured["quick"] = (await req.files)["file"]
        await res.send("quick")

    slow = asyncio.create_task(
        asgi_client(app, "POST", "/hold", headers=_headers(), body=_multipart(payload=payload))
    )
    await asyncio.wait_for(parsed.wait(), timeout=2)

    await asgi_client(
        app, "POST", "/quick", headers=_headers(), body=_multipart(payload=payload)
    )
    await _wait_for(lambda: captured["quick"].temp_path is None)

    assert captured["hold"].temp_path is not None
    assert captured["hold"].temp_path.exists()
    assert captured["hold"].content is None

    release.set()
    await asyncio.wait_for(slow, timeout=2)
    await _wait_for(lambda: captured["hold"].temp_path is None)
    assert list(parser_dir.glob("upload_*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,should_spill", [(b"x" * 8, False), (b"x" * 9, True)])
async def test_upload_threshold_keeps_at_limit_and_spills_above(
    payload, should_spill, tmp_path, monkeypatch,
):
    import flameo.parser as parser
    from flameo.parser import RequestParser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 8)
    body = _multipart(payload=payload)

    async def chunks():
        yield body

    _, streamed_files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=flameo", chunks()
    )
    streamed = streamed_files["file"]
    assert streamed.size == len(payload)
    assert (streamed.temp_path is not None) is should_spill
    if should_spill:
        assert streamed.content is None
        assert paths[0].read_bytes() == payload
        await parser.cleanup_request_temp_files(paths)
    else:
        assert streamed.content == payload
        assert paths == []

    _, buffered_files = RequestParser.parse_multipart_body(
        "multipart/form-data; boundary=flameo", body
    )
    buffered = buffered_files["file"]
    assert buffered.size == len(payload)
    assert (buffered.temp_path is not None) is should_spill
    if should_spill:
        assert buffered.content is None
        assert buffered.temp_path.read_bytes() == payload
        buffered._drop_temp()
    else:
        assert buffered.content == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
@pytest.mark.parametrize("endpoint_kind", ["app", "router"])
async def test_request_model_file_binding_across_endpoints(
    protocol, endpoint_kind, app_factory, asgi_client, rsgi_client, tmp_path, monkeypatch,
):
    from pydantic import BaseModel

    from flameo import File
    import flameo.parser as parser

    parser_dir = tmp_path / "spills"
    monkeypatch.setattr(parser, "TEMP_DIR", parser_dir)
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    captured = {}

    class Upload(BaseModel):
        title: str
        document: File

    async def upload(data: Upload, res):
        captured["file"] = data.document
        await res.json({
            "title": data.title,
            "filename": data.document.filename,
            "content": data.document.content.decode(),
        })

    path = _register_endpoint(app, endpoint_kind, "/model", upload)
    body = (
        b"--flameo\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"report\r\n--flameo\r\n"
        b"Content-Disposition: form-data; name=\"document\"; filename=\"report.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\ncontents\r\n--flameo--\r\n"
    )
    response = await client(app, "POST", path, headers=_headers(), body=body)

    assert response["status"] == 200
    assert json.loads(response["body"]) == {
        "title": "report", "filename": "report.txt", "content": "contents",
    }
    await _wait_for(lambda: captured["file"].content is None)
    assert captured["file"].temp_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["asgi", "rsgi"])
async def test_response_after_runs_background_task(protocol, app_factory, asgi_client, rsgi_client):
    client = asgi_client if protocol == "asgi" else rsgi_client
    app = app_factory()
    finished = asyncio.Event()

    @app.get("/after")
    async def after(req, res):
        res.after(finished.set)
        await res.send("done")

    response = await client(app, path="/after")
    assert response["body"] == b"done"
    await asyncio.wait_for(finished.wait(), timeout=2)
