import base64
import sys
import uuid
from collections import OrderedDict
from pathlib import Path

import pytest

import flameo.parser as parser
import flameo.flameo as utils
from flameo.parser import RequestParser
from flameo.flameo import File


@pytest.fixture
def parser_tmp(monkeypatch, tmp_path):
    old = parser.TEMP_DIR
    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)
    yield tmp_path
    monkeypatch.setattr(parser, "TEMP_DIR", old)


@pytest.fixture
def clean_tracked():
    yield


def _chunks(data, sizes):
    async def gen():
        i = 0
        k = 0
        while i < len(data):
            n = sizes[k % len(sizes)]
            chunk = data[i:i + n]
            i += n
            k += 1
            yield chunk
    return gen()


def _aiter(items):
    async def gen():
        for it in items:
            yield it
    return gen()


# ---------------- utils.py ----------------

def test_file_discard_clears_ram_and_disk(parser_tmp):
    p = parser_tmp / "upload_orphan.part"
    p.write_bytes(b"data")
    tracker = []
    f = File(file_content=None, temp_path=p, filename="o.txt")
    f._tracker = tracker
    tracker.append(f)
    f._discard()
    assert f.temp_path is None
    assert f.content is None
    assert tracker == []
    assert not p.exists()


def test_file_discard_tolerates_drop_oserror(parser_tmp, monkeypatch):
    p = parser_tmp / "upload_bad.part"
    p.write_bytes(b"data")
    f = File(file_content=None, temp_path=p, filename="o.txt")
    monkeypatch.setattr(File, "_drop_temp", lambda self: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        f._discard()


def test_file_discard_without_temp_clears_content(tmp_path):
    f = File(file_content=b"x", filename="m.txt")
    f._discard()
    assert f.content is None
    assert f.temp_path is None


@pytest.mark.asyncio
async def test_upload_sink_finalize_spill_does_not_duplicate_tail(parser_tmp, monkeypatch):
    monkeypatch.setattr(parser, "UPLOAD_MEMORY_LIMIT", 4)
    sink = parser._UploadSink("x.bin", "application/octet-stream", "file")
    await sink.write(b"abcde\n")
    temp_path, content, size = await sink.finalize()
    assert content is None
    assert size == 5
    assert temp_path.read_bytes() == b"abcde"
    await sink.abort()


def test_unregister_removes_from_tracker(clean_tracked):
    tracker = []
    f = File(file_content=b"x", filename="a")
    f._tracker = tracker
    tracker.append(f)
    f._unregister()
    assert tracker == []
    assert f._tracker is None
    f._unregister()


@pytest.mark.asyncio
async def test_file_base64_to_file_caches_module(clean_tracked, monkeypatch):
    monkeypatch.setattr(utils, "_base64", None)
    f = File(file_content=base64.b64encode(b"hello"), filename="a.txt")
    await f.base64_to_file()
    assert f.content == b"hello"
    f2 = File(file_content=base64.b64encode(b"world"), filename="b.txt")
    await f2.base64_to_file()  # cache-hit branch 79->82
    assert f2.content == b"world"


@pytest.mark.asyncio
async def test_module_base64_to_file_both_branches(monkeypatch):
    import flameo.utils as utils_module

    monkeypatch.setattr(utils_module, "_base64", None)
    out1 = await utils_module.base64_to_file(base64.b64encode(b"one"))
    assert out1 == b"one"
    out2 = await utils_module.base64_to_file(base64.b64encode(b"two"))  # cached
    assert out2 == b"two"


def test_drop_temp_none_and_missing(clean_tracked, tmp_path):
    File(file_content=b"x", filename="a")._drop_temp()  # temp_path None -> return
    f = File(file_content=None, temp_path=tmp_path / "nope.part", filename="a")
    f._drop_temp()  # missing file, exists() False
    f._unregister()


def test_drop_temp_oserror_swallowed(clean_tracked, tmp_path, monkeypatch):
    p = tmp_path / "x.part"
    p.write_bytes(b"data")
    f = File(file_content=None, temp_path=p, filename="a")
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("locked")))
    f._drop_temp()  # must not raise
    f._unregister()


def test_unregister_missing_is_ignored(clean_tracked):
    f = File(file_content=b"x", filename="a")
    f._unregister()
    f._unregister()


@pytest.mark.asyncio
async def test_upload_to_rejects_bad_directory(clean_tracked, tmp_path):
    f = File(file_content=b"x", filename="a.txt")
    with pytest.raises(ValueError, match="Upload directory not specified"):
        await f.upload_to("")
    with pytest.raises(ValueError, match="not exists"):
        await f.upload_to(str(tmp_path / "missing"))


@pytest.mark.asyncio
async def test_upload_to_requires_name(clean_tracked, tmp_path):
    f = File(file_content=b"x", filename=None)
    with pytest.raises(NameError, match="No name assigned"):
        await f.upload_to(str(tmp_path))


@pytest.mark.asyncio
async def test_upload_to_requires_aiofiles(clean_tracked, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiofiles", None)
    f = File(file_content=b"x", filename="a.txt")
    with pytest.raises(ImportError, match="aiofiles"):
        await f.upload_to(str(tmp_path))


@pytest.mark.asyncio
async def test_upload_to_exdev_fallback_copies(clean_tracked, tmp_path, monkeypatch):
    src = tmp_path / "upload_src.part"
    src.write_bytes(b"exdev-payload")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f = File(file_content=None, temp_path=src, filename="orig.bin")
    f.rename_as("moved.bin")

    real_replace = __import__("os").replace

    def fake_replace(a, b):
        exc = OSError("cross-device")
        exc.errno = 18
        raise exc

    monkeypatch.setattr(__import__("os"), "replace", fake_replace)
    out = await f.upload_to(str(dest_dir), chunk_size=2)
    assert out == dest_dir / "moved.bin"
    assert out.read_bytes() == b"exdev-payload"
    assert f.temp_path is None
    assert not src.exists()


@pytest.mark.asyncio
async def test_upload_to_reraises_non_exdev(clean_tracked, tmp_path, monkeypatch):
    import os as _os
    src = tmp_path / "upload_src2.part"
    src.write_bytes(b"data")
    dest_dir = tmp_path / "dest2"
    dest_dir.mkdir()
    f = File(file_content=None, temp_path=src, filename="a.bin")

    def fake_replace(a, b):
        exc = OSError("perm")
        exc.errno = 13
        raise exc

    monkeypatch.setattr(_os, "replace", fake_replace)
    with pytest.raises(OSError):
        await f.upload_to(str(dest_dir))
    assert src.exists()
    f._drop_temp()
    f._unregister()


@pytest.mark.asyncio
async def test_upload_to_inmemory_exists_and_replace(clean_tracked, tmp_path):
    dest = tmp_path / "a.txt"
    dest.write_bytes(b"old")
    f = File(file_content=b"new-content", filename="a.txt")
    with pytest.raises(ValueError, match="already exists"):
        await f.upload_to(str(tmp_path))
    out = await f.upload_to(str(tmp_path), replace=True)
    assert out.read_bytes() == b"new-content"


@pytest.mark.asyncio
async def test_upload_to_inmemory_none_content_creates_empty(clean_tracked, tmp_path):
    f = File(file_content=None, filename="empty.txt")
    out = await f.upload_to(str(tmp_path))
    assert out == tmp_path / "empty.txt"
    assert out.exists()


@pytest.mark.asyncio
async def test_upload_to_chunked_write_loop(clean_tracked, tmp_path):
    f = File(file_content=b"0123456789", filename="c.txt")
    out = await f.upload_to(str(tmp_path), chunk_size=3)
    assert out.read_bytes() == b"0123456789"


def test_repr_size_branches(clean_tracked):
    f = File(file_content=b"abc", filename="a.txt")
    f.size = None
    assert "size=3" in repr(f)
    g = File(file_content=None, filename="b.txt")
    g.size = None
    assert "size=None" in repr(g)


def test_pydantic_validate():
    assert File._pydantic_validate(None) is None
    f = File(file_content=b"x", filename="a")
    assert File._pydantic_validate(f) is f
    with pytest.raises(ValueError, match="Expected flameo.flameo.File"):
        File._pydantic_validate("nope")


def test_pydantic_core_schema_shape():
    schema = File.__get_pydantic_core_schema__(None, None)
    assert schema["type"] == "function-plain"
    inner = schema["function"]["function"]
    assert getattr(inner, "__name__", None) == "_pydantic_validate"
    assert callable(inner)


def test_template_to_string_requires_directory(monkeypatch):
    from collections import OrderedDict
    from flameo import templates, template_to_string

    monkeypatch.setattr(templates, "_last_template_directory", None)
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE", OrderedDict())
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE", OrderedDict())
    with pytest.raises(RuntimeError, match="no template folder registered yet"):
        template_to_string("mail.html")


def test_template_to_string_async_no_running_loop(app_factory, tmp_path, monkeypatch):
    from collections import OrderedDict
    from flameo import templates, template_to_string

    async def async_shout(value):
        return str(value).upper()

    monkeypatch.setattr(templates, "_last_template_directory", None)
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE", OrderedDict())
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE", OrderedDict())
    d = tmp_path / "tpl_async_sync"
    d.mkdir()
    (d / "m.txt").write_text("Hello {{ name | async_shout }}", encoding="utf-8")
    app = app_factory()
    app.use_template(str(d), filters=[async_shout])
    # no running loop here (sync test) -> asyncio.run branch
    assert template_to_string("m.txt", {"name": "Ada"}, filters=[async_shout]) == "Hello ADA"


@pytest.mark.asyncio
async def test_chunked_copy_direct(tmp_path):
    src = tmp_path / "s.bin"
    dst = tmp_path / "d.bin"
    src.write_bytes(b"chunked-data-123")
    await utils._chunked_copy(src, dst, 4)
    assert dst.read_bytes() == b"chunked-data-123"


def test_template_to_string_sync(app_factory, tmp_path, monkeypatch):
    from collections import OrderedDict
    from flameo import templates, template_to_string

    monkeypatch.setattr(templates, "_last_template_directory", None)
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE", OrderedDict())
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE", OrderedDict())
    d = tmp_path / "tpl"
    d.mkdir()
    (d / "hi.html").write_text("Hello {{ name }}", encoding="utf-8")
    app = app_factory()
    app.use_template(str(d))
    assert template_to_string("hi.html", {"name": "Ada"}) == "Hello Ada"


@pytest.mark.asyncio
async def test_template_to_string_threadpool_path(app_factory, tmp_path, monkeypatch):
    from collections import OrderedDict
    from flameo import templates, template_to_string

    async def async_shout(value):
        return str(value).upper()

    monkeypatch.setattr(templates, "_last_template_directory", None)
    monkeypatch.setattr(templates, "_CALL_ENV_CACHE", OrderedDict())
    monkeypatch.setattr(templates, "_STRING_ENV_CACHE", OrderedDict())
    d = tmp_path / "tpl_async"
    d.mkdir()
    (d / "m.txt").write_text("Hello {{ name | async_shout }}", encoding="utf-8")
    app = app_factory()
    app.use_template(str(d), filters=[async_shout])
    # inside a running loop with async env -> ThreadPoolExecutor branch
    assert template_to_string("m.txt", {"name": "Ada"}, filters=[async_shout]) == "Hello ADA"


# ---------------- parser.py ----------------

def test_set_get_ensure_temp_dir(tmp_path, monkeypatch):
    old = parser.get_temp_dir()
    try:
        out = parser.set_temp_dir(tmp_path / "spills")
        assert out == Path(tmp_path / "spills")
        assert parser.get_temp_dir() == Path(tmp_path / "spills")
        made = parser._ensure_temp_dir()
        assert made.is_dir()
    finally:
        parser.set_temp_dir(old)


def test_parse_part_headers_skips_bad_lines():
    hdrs = parser._parse_part_headers(b"BadLine\r\nContent-Type: text/plain\r\nX:  1  ")
    assert hdrs == {"content-type": "text/plain", "x": "1"}


def test_rstrip_variants():
    assert parser._rstrip_crlf_len(b"abc\r\n") == 2
    assert parser._rstrip_crlf_len(b"abc\n") == 1
    assert parser._rstrip_crlf_len(b"abc\r") == 1
    assert parser._rstrip_crlf_len(b"abc") == 0
    assert parser._rstrip_crlf_len(b"") == 0


def test_streamed_file_slots():
    sf = parser._StreamedFile("tp", "fn", "ct", "fl", 5)
    assert (sf.temp_path, sf.filename, sf.content_type, sf.field_name, sf.size) == ("tp", "fn", "ct", "fl", 5)


@pytest.mark.asyncio
async def test_streaming_no_boundary_raises():
    with pytest.raises(ValueError, match="No boundary"):
        await parser.parse_multipart_body_streaming("multipart/form-data", _aiter([b"x"]))


@pytest.mark.asyncio
async def test_streaming_simple_split_boundaries(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
            b'hi\r\n--B\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nxyz\r\n--B--\r\n')
    for cs in (1, 2, 3, 7):
        fields, files, paths = await parser.parse_multipart_body_streaming(
            "multipart/form-data; boundary=B", _chunks(data, [cs]))
        assert fields == {"note": "hi"}
        assert files["f"].size == 3
        assert files["f"].content == b"xyz"
        await parser.cleanup_request_temp_files(paths)


@pytest.mark.asyncio
async def test_streaming_ignores_inline_boundary(parser_tmp):
    # boundary bytes inside payload but NOT at line start must be ignored
    data = (b'--S\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
            b'a--S b\r\n--S\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nx--S y\r\n--S--\r\n')
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=S", _chunks(data, [1]))
    assert fields == {"note": "a--S b"}
    assert files["f"].content == b"x--S y"
    await parser.cleanup_request_temp_files(paths)


@pytest.mark.asyncio
async def test_streaming_lf_only_headers(parser_tmp):
    data = (b'--B\nContent-Disposition: form-data; name="a"\n\nhi\n--B--\n')
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(data, [1]))
    assert fields == {"a": "hi"}
    assert files == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_empty_chunks_tolerated(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data; name="a"\r\n\r\nhi\r\n--B--\r\n')
    fields, _, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([b"", data[:10], b"", data[10:], b""]))
    assert fields == {"a": "hi"}


@pytest.mark.asyncio
async def test_streaming_header_too_large_breaks(parser_tmp):
    big = b"--B\r\n" + b"X: " + b"A" * 70000
    fields, files, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([big]))
    assert fields == {} and files == {}


@pytest.mark.asyncio
async def test_streaming_unexpected_end_in_headers(parser_tmp):
    data = b'--B\r\nContent-Disposition: form-data; name="x"\r\n'
    fields, files, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert fields == {} and files == {}


@pytest.mark.asyncio
async def test_streaming_boundary_line_split_across_chunks(parser_tmp):
    part = (b'--B\r\nContent-Disposition: form-data; name="a"\r\n\r\nhi\r\n--B--\r\n')
    fields, _, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(part, [1]))
    assert fields == {"a": "hi"}


@pytest.mark.asyncio
async def test_streaming_eol_eof_breaks(parser_tmp):
    fields, files, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([b"--B\r"]))
    assert fields == {} and files == {}


@pytest.mark.asyncio
async def test_streaming_malformed_part_without_name(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data\r\n\r\nbody\r\n--B--\r\n'
            b'')
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert fields == {} and files == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_malformed_part_eof_no_boundary(parser_tmp):
    data = b'--B\r\nContent-Disposition: form-data\r\n\r\nbody'
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert fields == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_file_eof_malformed_cleans_temp(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\ndata')
    before = set(parser_tmp.iterdir()) if parser_tmp.exists() else set()
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(data, [2]))
    assert files == {} and paths == []
    after = set(parser_tmp.iterdir()) if parser_tmp.exists() else set()
    assert after == before


@pytest.mark.asyncio
async def test_streaming_file_eof_malformed_unlink_oserror(parser_tmp, monkeypatch):
    data = (b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\ndata')
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("busy")))
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert files == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_empty_file_bodies(parser_tmp):
    # immediate boundary (cut2 == 0)
    data1 = (b'--B\r\nContent-Disposition: form-data; name="f"; filename="e.bin"\r\n'
             b'Content-Type: application/octet-stream\r\n\r\n--B--\r\n')
    _, files1, paths1 = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data1]))
    assert files1["f"].size == 0
    assert files1["f"].content == b""
    await parser.cleanup_request_temp_files(paths1)
    # CRLF-only body stripped to empty
    data2 = (b'--B\r\nContent-Disposition: form-data; name="g"; filename="e2.bin"\r\n'
             b'Content-Type: application/octet-stream\r\n\r\n\r\n--B--\r\n')
    _, files2, paths2 = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data2]))
    assert files2["g"].size == 0
    await parser.cleanup_request_temp_files(paths2)


@pytest.mark.asyncio
async def test_streaming_field_eof_without_boundary(parser_tmp):
    data = b'--B\r\nContent-Disposition: form-data; name="note"\r\n\r\npartial'
    fields, _, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert fields == {"note": "partial"}


@pytest.mark.asyncio
async def test_streaming_repeated_fields_and_files(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data; name="tag"\r\n\r\none\r\n'
            b'--B\r\nContent-Disposition: form-data; name="tag"\r\n\r\ntwo\r\n'
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nA\r\n'
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="b.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nB\r\n--B--\r\n')
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(data, [3]))
    assert fields == {"tag": ["one", "two"]}
    assert isinstance(files["f"], list) and len(files["f"]) == 2
    await parser.cleanup_request_temp_files(paths)


@pytest.mark.asyncio
async def test_streaming_delim_suffix_at_eof(parser_tmp):
    # pending ends exactly at the delim / delim+"-" with EOF: unresolved
    # branches (177 / 183) fall through with no candidate -> clean EOF.
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([b"--B"]))
    assert fields == {} and files == {} and paths == []
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([b"--B-"]))
    assert fields == {} and files == {} and paths == []
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([b"--B--"]))
    assert fields == {} and files == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_lookalike_delimiters_ignored(parser_tmp):
    # `--Bz` (tail char not CR/LF/-) and `--B--X` (bad trailer) at a line
    # boundary must not be treated as delimiters.
    data = (b'--B\r\nContent-Disposition: form-data; name="note"\r\n\r\n'
            b'one\r\n--BZZZ\r\ntwo\r\n--B-X\r\nthree\r\n--B--X\r\nfour\r\n--B--\r\n')
    fields, _, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _chunks(data, [2]))
    assert fields == {"note": "one\r\n--BZZZ\r\ntwo\r\n--B-X\r\nthree\r\n--B--X\r\nfour"}


@pytest.mark.asyncio
async def test_streaming_empty_chunk_while_waiting_for_eol(parser_tmp):
    items = [b"--B\r", b"",
             b"\nContent-Disposition: form-data; name=\"a\"\r\n\r\nhi\r\n--B--\r\n"]
    fields, _, _ = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter(items))
    assert fields == {"a": "hi"}


@pytest.mark.asyncio
async def test_streaming_file_eof_empty_body(parser_tmp):
    data = (b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n')
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert files == {} and paths == []


@pytest.mark.asyncio
async def test_streaming_file_eof_malformed_missing_spill(parser_tmp, monkeypatch):
    data = (b'--B\r\nContent-Disposition: form-data; name="f"; filename="x.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\ndata')
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False)
    fields, files, paths = await parser.parse_multipart_body_streaming(
        "multipart/form-data; boundary=B", _aiter([data]))
    assert files == {} and paths == []
    monkeypatch.setattr(Path, "exists", real_exists)
    data = b'--Q\r\nContent-Disposition: form-data; name="a"\r\n\r\nhi\r\n--Q--\r\n'
    fields, _, _ = await parser.parse_multipart_body_streaming(
        'multipart/form-data; boundary="Q"', _aiter([data]))
    assert fields == {"a": "hi"}


@pytest.mark.asyncio
async def test_cleanup_request_temp_files_missing_and_oserror(tmp_path, monkeypatch):
    await parser.cleanup_request_temp_files([tmp_path / "nope.part"])
    p = tmp_path / "x.part"
    p.write_bytes(b"data")
    monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(OSError("busy")))
    await parser.cleanup_request_temp_files([p])  # swallowed
    monkeypatch.undo()
    await parser.cleanup_request_temp_files([p])
    assert not p.exists()


def test_buffered_no_boundary_raises():
    with pytest.raises(ValueError, match="No boundary"):
        RequestParser.parse_multipart_body("text/plain", b"")


def test_buffered_trailing_garbage_and_bare_close(parser_tmp):
    body = (b'--B\r\nContent-Disposition: form-data; name="a"\r\n\r\nx\r\n--B--garbage')
    fields, _ = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert fields == {"a": "x"}


def test_buffered_lf_separators(parser_tmp):
    body = b'--B\nContent-Disposition: form-data; name="a"\n\nhi\n--B--\n'
    fields, _ = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert fields == {"a": "hi"}


def test_buffered_part_without_separator_skipped(parser_tmp):
    body = b'--B\r\nContent-Disposition: form-data; name="a"\r\nno-blank\r\n--B--\r\n'
    fields, files = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert fields == {} and files == {}


def test_buffered_part_without_name_skipped(parser_tmp):
    body = b'--B\r\nContent-Type: text/plain\r\n\r\nzzz\r\n--B--\r\n'
    fields, files = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert fields == {} and files == {}


def test_buffered_empty_filename_skipped(parser_tmp):
    body = (b'--B\r\nContent-Disposition: form-data; name="f"; filename=""\r\n\r\nzzz\r\n--B--\r\n')
    _, files = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert files == {}


def test_buffered_repeated_names_produce_lists(parser_tmp):
    body = (b'--B\r\nContent-Disposition: form-data; name="tag"\r\n\r\none\r\n'
            b'--B\r\nContent-Disposition: form-data; name="tag"\r\n\r\ntwo\r\n'
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="a.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nA\r\n'
            b'--B\r\nContent-Disposition: form-data; name="f"; filename="b.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\nB\r\n--B--\r\n')
    fields, files = RequestParser.parse_multipart_body("multipart/form-data; boundary=B", body)
    assert fields == {"tag": ["one", "two"]}
    assert isinstance(files["f"], list) and len(files["f"]) == 2
    for f in files["f"]:
        f._drop_temp()
        f._unregister()


def test_decode_field_replaces_bad_bytes():
    assert parser._decode_field(b"\xff\xfe") == "\ufffd\ufffd"
