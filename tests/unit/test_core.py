import asyncio

import pytest

from flameo.cycles import RouterMap
from flameo.endpoint_cache import MemoryCacheStore
from flameo.models import bind_to_instance, bind_to_list
from flameo.parser import RequestParser
from flameo.sessions import Session, _decode_session, _encode_session
from flameo.validation import _expand_nested_keys, validate_model


def test_router_map_static_dynamic_typed_and_precedence():
    routes = RouterMap()
    static_handler = object()
    dynamic_handler = object()
    typed_handler = object()
    routes.add_route("GET", "/users/:user_id", dynamic_handler)
    routes.add_route("GET", "/users/me", static_handler)
    routes.add_route("GET", "/orders/{order_id:int}", typed_handler)

    assert routes.match("GET", "/users/me")[0] is static_handler
    assert routes.match("GET", "/users/42")[0] is dynamic_handler
    assert routes.match("GET", "/orders/17")[0] is typed_handler
    assert routes.match("GET", "/orders/nope")[0] is None
    assert routes.match("GET", "/users/42")[1]["user_id"] == "42"


def test_duplicate_static_route_replaces_previous_handler():
    routes = RouterMap()
    first, second = object(), object()
    routes.add_route("GET", "/same", first)
    routes.add_route("GET", "/same", second)
    assert routes.match("GET", "/same")[0] is second


@pytest.mark.asyncio
async def test_memory_cache_expiry_json_increment_and_nx(monkeypatch):
    import flameo.endpoint_cache as cache_module

    now = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    cache = MemoryCacheStore()
    assert await cache.set("key", "1", ex=2, nx=True) is True
    assert await cache.set("key", "2", nx=True) is None
    assert await cache.incr("key") == 2
    await cache.set_json("json", {"ok": True})
    assert await cache.get_json("json") == {"ok": True}
    now[0] += 3
    assert await cache.get("key") is None
    with pytest.raises(KeyError):
        await cache.incr("missing")


def test_session_mutation_tracking_signatures_and_expiration(monkeypatch):
    session = Session({"user": "sam"})
    assert session._dirty is False
    session["user"] = "lee"
    assert session._dirty is True
    session.mark_clean()
    session.destroy()
    assert session == {}
    assert session._dirty is True

    token = _encode_session("long-enough-secret", {"n": 3})
    assert _decode_session("long-enough-secret", token, 60) == {"n": 3}
    assert _decode_session("wrong-secret", token, 60) is None
    assert _decode_session("long-enough-secret", "bad-cookie", 60) is None

    import flameo.sessions as sessions
    monkeypatch.setattr(sessions.time, "time", lambda: 3_000_000_000)
    assert _decode_session("long-enough-secret", token, 1) is None


def test_nested_validation_and_case_insensitive_model_fields():
    from pydantic import BaseModel

    class Address(BaseModel):
        postal_code: int

    class Person(BaseModel):
        name: str
        address: Address
        tags: list[str] = []

    normalized = _expand_nested_keys({"address[postal_code]": "100", "tags[]": "a"})
    assert normalized == {"address": {"postal_code": "100"}, "tags": ["a"]}
    valid, errors = validate_model(
        Person, {"NAME": "Ada", "address": {"postal_code": "100"}},
        validate_case_sensitive=False,
    )
    assert errors == []
    assert valid.name == "Ada" and valid.address.postal_code == 100
    invalid, errors = validate_model(Person, {"name": "Ada", "address": {"postal_code": "x"}})
    assert invalid is None
    assert errors


def test_model_binder_converts_and_ignores_unknown_fields():
    class Record:
        identifier: int
        label: str = "default"

    record = bind_to_instance({"IDENTIFIER": "7", "unused": True}, Record)
    assert record.identifier == 7
    assert record.label == "default"
    records = bind_to_list([{"identifier": 2}], Record)
    assert [record.identifier for record in records] == [2]


def test_buffered_multipart_fields_and_file_are_kept_in_memory(monkeypatch, tmp_path):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    boundary = "abc123"
    body = (
        b"--abc123\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"hello\r\n--abc123\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"a.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\nfile bytes\r\n--abc123--\r\n"
    )
    fields, files = RequestParser.parse_multipart_body(f"multipart/form-data; boundary={boundary}", body)
    assert fields == {"title": "hello"}
    uploaded = files["upload"]
    assert uploaded.filename == "a.txt"
    assert uploaded.size == 10
    assert uploaded.temp_path is None
    assert uploaded.content == b"file bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
async def test_streaming_multipart_parser_keeps_small_file_in_memory(monkeypatch, tmp_path, chunk_size):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    data = (
        b"--split\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\n"
        b"hi\r\n--split-X is field content\ninside\r\n"
        b"--split\r\nContent-Disposition: form-data; name=\"f\"; filename=\"x.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n"
        b"xyz\r\n--split-X is file content\ninside\r\n--split--"
    )

    async def chunks():
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]

    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=split", chunks()
    )
    assert fields == {"note": "hi\r\n--split-X is field content\ninside"}
    expected_file = b"xyz\r\n--split-X is file content\ninside"
    assert files["f"].size == len(expected_file)
    assert files["f"].temp_path is None
    assert files["f"].content == expected_file
    assert paths == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
async def test_streaming_multipart_parser_spills_large_file(monkeypatch, tmp_path, chunk_size):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 8)
    payload = b"x" * 32
    data = (
        b"--split\r\nContent-Disposition: form-data; name=\"f\"; filename=\"x.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n--split--\r\n"
    )

    async def chunks():
        for start in range(0, len(data), chunk_size):
            yield data[start:start + chunk_size]

    _, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=split", chunks()
    )
    assert files["f"].content is None
    assert files["f"].size == len(payload)
    assert len(paths) == 1
    assert paths[0].read_bytes() == payload
    await parser.cleanup_request_temp_files(paths)
    assert not paths[0].exists()


def test_public_configuration_validation(app_factory):
    from flameo import CacheType

    with pytest.raises(ValueError, match="max_age"):
        app_factory().use_cors(max_age=-1)
    with pytest.raises(ValueError, match="secret_key"):
        app_factory().use_session(secret_key="short")
    with pytest.raises(ValueError, match="cache_config"):
        app_factory(cache_config={"cache_type": CacheType.Redis})


@pytest.mark.asyncio
async def test_sqlite_query_manager_reads_writes_and_transactions(monkeypatch):
    import aiosqlite
    import flameo.flameo as framework
    from flameo import DBType, TransactionError
    from flameo.flameo import QueryManager

    monkeypatch.setattr(framework, "_db_type", DBType.SQLite)
    async with aiosqlite.connect(":memory:", isolation_level=None) as connection:
        manager = QueryManager(connection)
        await manager.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, name TEXT)")
        assert await manager.execute("INSERT INTO records (name) VALUES (?)", ("one",)) == 1
        assert await manager.executemany(
            "INSERT INTO records (name) VALUES (?)", [("two",), ("three",)]
        ) == 2
        assert await manager.fetchrow("SELECT id, name FROM records WHERE id = ?", (1,)) == {
            "id": 1, "name": "one"
        }
        assert len(await manager.fetch("SELECT id, name FROM records", batch_size=2)) == 3
        with pytest.raises(ValueError, match="positive"):
            await manager.fetch("SELECT id FROM records", batch_size=0)

        async with manager.transaction():
            await manager.execute("INSERT INTO records (name) VALUES (?)", ("committed",))
        assert await manager.fetchrow("SELECT id FROM records WHERE name = ?", ("committed",))

        async with manager.transaction():
            async with manager.transaction():
                await manager.execute("INSERT INTO records (name) VALUES (?)", ("nested",))
        assert await manager.fetchrow("SELECT id FROM records WHERE name = ?", ("nested",))

        with pytest.raises(TransactionError) as info:
            async with manager.transaction():
                await manager.execute("INSERT INTO records (name) VALUES (?)", ("rolled back",))
                raise RuntimeError("rollback")
        assert isinstance(info.value.cause, RuntimeError)
        assert info.value.rolled_back is True
        assert await manager.fetchrow("SELECT id FROM records WHERE name = ?", ("rolled back",)) is None
        assert await manager.execute_returning(
            "INSERT INTO records (name) VALUES (?) RETURNING id, name", ("returned",)
        ) == {"id": 6, "name": "returned"}
        await manager.close()
        assert manager._closed


def test_temp_sweeper_removes_only_stale_spill_files(monkeypatch, tmp_path):
    import os
    import time
    import flameo.cleanup as cleanup

    monkeypatch.setattr(cleanup, "get_temp_dir", lambda: tmp_path)
    stale = tmp_path / "upload_old.part"
    fresh = tmp_path / "body_fresh.part"
    unrelated = tmp_path / "keep.txt"
    for path in (stale, fresh, unrelated):
        path.write_text("x", encoding="utf-8")
    os.utime(stale, (time.time() - 100, time.time() - 100))
    assert cleanup._sweep_once(10) == 1
    assert not stale.exists()
    assert fresh.exists() and unrelated.exists()


@pytest.mark.asyncio
async def test_file_base64_conversion_and_rename(tmp_path):
    import base64
    from flameo import File

    file = File(file_content=base64.b64encode(b"sample"), filename="old.txt")
    assert file.rename_as("new.txt") is file
    assert file.new_name == "new.txt"
    await file.base64_to_file()
    assert file.content == b"sample"
    assert await file.upload_to(str(tmp_path)) == tmp_path / "new.txt"
    assert (tmp_path / "new.txt").read_bytes() == b"sample"


@pytest.mark.asyncio
async def test_shutdown_db_pool_handles_sync_and_async_close(app_factory):
    events = []

    class SyncClosePool:
        def close(self):
            events.append("close")

        async def wait_closed(self):
            events.append("wait_closed")

    class AsyncClosePool:
        async def close(self):
            events.append("async-close")

    sync_app = app_factory()
    sync_app.DB_POOL = SyncClosePool()
    await sync_app.shutdown_db_pool()
    assert events == ["close", "wait_closed"]
    assert sync_app.DB_POOL is None

    async_app = app_factory()
    async_app.DB_POOL = AsyncClosePool()
    await async_app.shutdown_db_pool()
    assert events == ["close", "wait_closed", "async-close"]
    assert async_app.DB_POOL is None
