import os
import re
import uuid
import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Tuple

# Where the parser spills part bodies while the request is in flight.
# Created on demand so projects that never receive an upload don't pay
# the cost of an empty directory at startup.
TEMP_DIR = Path("temp")
UPLOAD_MEMORY_LIMIT = 1024 * 1024
_UPLOAD_TAIL = 2


def set_temp_dir(directory) -> Path:
    """Point the spill directory at ``directory``.

    Called by :class:`~flameo.Flameo` when the app is built with a
    ``temp_dir``. Returns the new :class:`~pathlib.Path`.
    """
    global TEMP_DIR
    TEMP_DIR = Path(directory)
    return TEMP_DIR


def get_temp_dir() -> Path:
    """Return the directory the parser currently spills uploads into."""
    return TEMP_DIR


def _ensure_temp_dir() -> Path:
    """Make sure the spill directory exists, return it as a ``Path``.

    Multipart uploads are spilled to disk so ``File.upload_to`` can
    ``os.replace`` them into place instead of buffering bytes. The
    directory is created lazily -- non-upload code paths never touch it.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR


def _parse_part_headers(header_block: bytes) -> Dict[str, str]:
    """Decode the raw bytes between ``--boundary`` and ``\\r\\n\\r\\n``
    into a ``{lowercase-name: value}`` dict. Header names are case-
    insensitive per RFC 7230, so we always lower-case them here.
    """
    out: Dict[str, str] = {}
    for line in header_block.split(b"\r\n"):
        if not line or b":" not in line:
            continue
        name_b, _, value_b = line.partition(b":")
        out[name_b.decode("latin-1").strip().lower()] = value_b.decode("latin-1").strip()
    return out


def _decode_field(content: bytes) -> str:
    """Best-effort text decode for a non-file form field.

    Multipart form fields are 7-bit ASCII per RFC 7578, but browsers do
    sometimes ship UTF-8. Use ``utf-8`` with ``errors='replace'`` so the
    parser never raises on weird bytes; callers can re-encode if they
    need stricter handling.
    """
    return content.decode("utf-8", errors="replace")


def _safe_upload_filename(name: str) -> str:
    """Keep only the final path component of a client-supplied filename."""
    cleaned = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    return cleaned if cleaned not in ("", ".", "..") else "upload"


def _rstrip_crlf_len(buf: bytes) -> int:
    """Return the count of trailing ``\r\n`` / ``\n`` / ``\r`` bytes.

    Used to peel the line break the multipart client emitted just
    before the boundary delimiter so the spill file's size matches
    ``File.size``. Accepts either CRLF, bare LF, or bare CR --
    tolerant of non-conformant clients that mix styles.
    """
    n = 0
    L = len(buf)
    if L >= 2 and buf[L - 2:L] == b"\r\n":
        return 2
    if L >= 1 and buf[L - 1] in (0x0A, 0x0D):
        return 1
    return 0


def _at_line_boundary(buf, pos, start_ok=True):
    if pos == 0:
        return start_ok
    return buf[pos - 1] in (0x0A, 0x0D)


def _scan_boundary(buf, delim, eof, start_ok=True):
    best_cut = -1
    best_kind = ""
    unresolved_at = None
    pos = 0
    del_len = len(delim)

    while pos < len(buf):
        idx = buf.find(delim, pos)
        if idx == -1:
            break
        if not _at_line_boundary(buf, idx, start_ok):
            pos = idx + 1
            continue

        tail = idx + del_len
        candidate_kind = ""
        if tail == len(buf):
            if not eof:
                unresolved_at = idx if unresolved_at is None else min(unresolved_at, idx)
        elif buf[tail] in (0x0D, 0x0A):
            candidate_kind = "delim"
        elif buf[tail] == 0x2D:
            if tail + 1 == len(buf):
                if not eof:
                    unresolved_at = idx if unresolved_at is None else min(unresolved_at, idx)
            elif buf[tail + 1] == 0x2D:
                trailer = tail + 2
                if trailer == len(buf):
                    if eof:
                        candidate_kind = "delim_end"
                    else:
                        unresolved_at = idx if unresolved_at is None else min(unresolved_at, idx)
                elif buf[trailer] in (0x0D, 0x0A):
                    candidate_kind = "delim_end"

        if candidate_kind and (best_cut < 0 or idx < best_cut):
            best_cut = idx
            best_kind = candidate_kind
        pos = idx + 1

    return best_cut, best_kind, unresolved_at


class _UploadSink:
    """Buffer an uploaded part in memory, spilling to disk past the limit."""

    __slots__ = (
        "filename", "content_type", "field_name", "size", "temp_path",
        "_buf", "_tail", "_fp",
    )

    def __init__(self, filename, content_type, field_name):
        self.filename = filename
        self.content_type = content_type
        self.field_name = field_name
        self.size = 0
        self.temp_path = None
        self._buf = bytearray()
        self._tail = b""
        self._fp = None

    async def _spill(self):
        import aiofiles
        _ensure_temp_dir()
        path = get_temp_dir() / f"upload_{uuid.uuid4().hex}.part"
        fp = await aiofiles.open(path, "wb")
        if self._buf:
            await fp.write(bytes(self._buf))
            self._buf.clear()
        self.temp_path = path
        self._fp = fp

    async def write(self, data):
        if not data:
            return
        data = self._tail + data
        if len(data) <= _UPLOAD_TAIL:
            self._tail = data
            return
        emit = data[:-_UPLOAD_TAIL]
        self._tail = data[-_UPLOAD_TAIL:]
        self.size += len(emit)
        if self._fp is not None:
            await self._fp.write(emit)
            return
        self._buf.extend(emit)
        if len(self._buf) > UPLOAD_MEMORY_LIMIT:
            await self._spill()

    async def finalize(self):
        trim = _rstrip_crlf_len(self._tail)
        tail = self._tail[: len(self._tail) - trim] if trim else self._tail
        self._tail = b""
        if self._fp is None:
            if tail:
                self._buf.extend(tail)
                self.size += len(tail)
            if len(self._buf) > UPLOAD_MEMORY_LIMIT:
                await self._spill()
            if self._fp is None:
                content = bytes(self._buf)
                self._buf.clear()
                self.size = len(content)
                return None, content, self.size
        elif tail:
            await self._fp.write(tail)
            self.size += len(tail)
        await self._fp.close()
        self._fp = None
        return self.temp_path, None, self.size

    async def abort(self):
        self._buf.clear()
        self._tail = b""
        if self._fp is not None:
            try:
                await self._fp.close()
            except OSError:
                pass
            self._fp = None
        if self.temp_path is not None:
            try:
                if self.temp_path.exists():
                    self.temp_path.unlink()
            except OSError:
                pass
            self.temp_path = None


class _StreamedFile:
    """Internal: metadata for a multipart part whose body is on disk.

    ``File`` (the public object in ``utils.py``) wraps one of these.
    Keeping the path / filename / MIME / field name / size here lets
    the parser hand back a portable payload without instantiating the
    public ``File`` class itself.
    """

    __slots__ = ("temp_path", "filename", "content_type", "field_name", "size")

    def __init__(self, temp_path, filename, content_type, field_name, size):
        self.temp_path = temp_path
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.field_name = field_name


async def parse_multipart_body_streaming(content_type, body_iter):
    """Stream-parse a ``multipart/form-data`` body part-by-part.

    File parts stay in memory until they exceed
    :data:`UPLOAD_MEMORY_LIMIT`; only then are they spilled to a temp
    file. Text fields are always decoded in memory.

    Returns ``(fields, files, temp_paths)``. ``temp_paths`` lists every
    spill-file the parser created.
    """

    # Lazy import: avoids a circular import at module load time.
    from .flameo import File

    boundary_match = RequestParser._BOUNDARY_RE.search(content_type or "")
    if not boundary_match:
        raise ValueError("No boundary in Content-Type")
    boundary_txt = (boundary_match.group(1) or boundary_match.group(2)).encode("latin-1")
    delim = b"--" + boundary_txt
    delim_end = delim + b"--"
    HEADER_MAX = 64 * 1024

    fields_lists = {}
    files_lists = {}
    temp_paths = []
    pending = bytearray()

    async def _find_delim():
        """Scan ``pending`` (refilling from ``body_iter`` as needed) and
        return ``(offset, kind)`` for the next ``delim`` or ``delim_end``.
        Returns ``(-1, "")`` at end-of-stream.
        """
        nonlocal pending

        eof = False
        while True:
            best_cut, best_kind, unresolved_at = _scan_boundary(
                pending, delim, eof, True
            )
            if best_cut >= 0 and (unresolved_at is None or best_cut < unresolved_at):
                return (best_cut, best_kind)

            if eof:
                return (-1, "")

            try:
                chunk = await body_iter.__anext__()
            except StopAsyncIteration:
                eof = True
                continue
            if chunk:
                pending.extend(chunk)

    async def _stream_file_body(sink):
        nonlocal pending

        eof = False
        start_ok = True
        keep_tail = len(delim) + 5

        while True:
            best_cut, best_kind, unresolved_at = _scan_boundary(
                pending, delim, eof, start_ok
            )
            if best_cut >= 0 and (unresolved_at is None or best_cut < unresolved_at):
                if best_cut > 0:
                    await sink.write(bytes(pending[:best_cut]))
                del pending[:best_cut]
                return best_kind

            if eof:
                if pending:
                    await sink.write(bytes(pending))
                    pending.clear()
                return ""

            if unresolved_at is not None:
                cut = unresolved_at
            else:
                raw = pending.find(delim)
                if raw == 0:
                    cut = 0
                elif raw > 0:
                    cut = min(max(0, len(pending) - keep_tail), raw - 1)
                else:
                    cut = max(0, len(pending) - keep_tail)

            if cut > 0:
                start_ok = pending[cut - 1] in (0x0A, 0x0D)
                await sink.write(bytes(pending[:cut]))
                del pending[:cut]

            try:
                chunk = await body_iter.__anext__()
            except StopAsyncIteration:
                eof = True
                continue
            if chunk:
                pending.extend(chunk)

    async def _read_headers_after_boundary():
        nonlocal pending
        while True:
            cut = pending.find(b"\r\n\r\n")
            if cut != -1:
                hb = bytes(pending[:cut])
                del pending[: cut + 4]
                return hb
            cut = pending.find(b"\n\n")
            if cut != -1:
                hb = bytes(pending[:cut])
                del pending[: cut + 2]
                return hb
            if len(pending) > HEADER_MAX:
                raise ValueError("[flameo] Multipart part header block too large")
            try:
                chunk = await body_iter.__anext__()
            except StopAsyncIteration:
                raise ValueError("[flameo] Unexpected end of stream inside part headers")
            if chunk:
                pending.extend(chunk)

    while True:
        cut, kind = await _find_delim()
        if cut < 0:
            break
        if kind == "delim_end":
            pending.clear()
            try:
                while True:
                    await body_iter.__anext__()
            except StopAsyncIteration:
                pass
            break

        # Consume the boundary line itself: everything up to and
        # including the LF past ``delim``.
        eol = pending.find(b"\n", cut)
        if eol == -1:
            try:
                chunk = await body_iter.__anext__()
            except StopAsyncIteration:
                break
            if chunk:
                pending.extend(chunk)
            continue
        del pending[: eol + 1]

        try:
            header_block = await _read_headers_after_boundary()
        except ValueError:
            break
        headers = _parse_part_headers(header_block)
        disp = headers.get("content-disposition", "")
        part_ct = headers.get("content-type", "application/octet-stream")

        name_m = RequestParser._DISP_NAME_RE.search(disp)
        if not name_m:
            # Malformed part -- skip its body. Drop everything up to
            # the next boundary; the outer loop will see it next.
            cut2, kind2 = await _find_delim()
            if cut2 >= 0:
                del pending[:cut2]
            else:
                pending.clear()
            continue
        field_name = name_m.group(1)

        filename_m = RequestParser._DISP_FILENAME_RE.search(disp)
        is_file_part = filename_m is not None and filename_m.group(1) != ""

        if is_file_part:
            fname = _safe_upload_filename(filename_m.group(1))
            sink = _UploadSink(fname, part_ct, field_name)
            kind2 = await _stream_file_body(sink)
            if not kind2:
                await sink.abort()
                break
            temp_path, content, size = await sink.finalize()
            if temp_path is not None:
                temp_paths.append(temp_path)
            files_lists.setdefault(field_name, []).append(
                File(
                    file_content=content,
                    temp_path=temp_path,
                    filename=fname,
                    content_type=part_ct,
                    field_name=field_name,
                    size=size,
                )
            )
        else:
            body = bytearray()
            while True:
                cut2, kind2 = await _find_delim()
                if cut2 < 0:
                    body.extend(pending)
                    pending.clear()
                    break
                body.extend(pending[:cut2])
                # Consume bytes BEFORE the boundary but NOT the boundary
                # line itself -- the outer loop expects to see the
                # boundary intact on its next iteration.
                del pending[:cut2]
                break
            fields_lists.setdefault(field_name, []).append(
                _decode_field(bytes(body).rstrip(b"\r\n"))
            )

    fields = {k: (v[0] if len(v) == 1 else v) for k, v in fields_lists.items()}
    files = {k: (v[0] if len(v) == 1 else v) for k, v in files_lists.items()}
    return fields, files, temp_paths


async def cleanup_request_temp_files(paths) -> None:
    """Best-effort delete of every spill-file a request created.

    Called by the request lifecycle after the response is sent, and on
    early exceptions. Failures are swallowed because there is nothing
    useful the caller can do with an unlink error mid-response.
    """
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


class RequestParser:
    """Stateless multipart/form-data parser (buffered-body flavor).

    Multipart bodies encode one or more *parts*, separated by a boundary
    line declared in the request's ``Content-Type`` header. Each part
    has its own short header block (carrying at least
    ``Content-Disposition`` and optionally ``Content-Type``) followed
    by the field's bytes.

    * ``fields`` -- non-file form fields. Values are bare strings when
      the field appears once, or a list of strings when it appears
      multiple times (e.g. ``<input name="tag">`` repeated).
    * ``files``  -- uploaded files. Each value is a
      :class:`flameo.flameo.File` instance (single upload) OR a list
      of such instances (multiple uploads for the same field name).

      Handler code reads the stored metadata with
      ``files.photo.filename``, ``files.photo.size``, and
      ``files.photo.content_type``, and persists the file with
      ``await files.photo.upload_to("uploads")``. See
      :class:`flameo.flameo.File` for the full surface.

    .. note::
       New code paths should prefer :func:`parse_multipart_body_streaming`,
       which avoids materializing the body in RAM. This buffered
       version remains for backwards compatibility and synthetic tests
       where the body is already in memory.
    """

    _BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;,\s]+))', re.I)
    _DISP_NAME_RE = re.compile(r'name="([^"]*)"', re.I)
    _DISP_FILENAME_RE = re.compile(r'filename="([^"]*)"', re.I)

    @staticmethod
    def parse_multipart_body(
        content_type: str, body: bytes
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        m = RequestParser._BOUNDARY_RE.search(content_type or "")
        if not m:
            raise ValueError("No boundary in Content-Type")
        boundary_txt = (m.group(1) or m.group(2)).encode("latin-1")

        delim = b"--" + boundary_txt
        chunks = body.split(delim)

        fields_lists: Dict[str, List[str]] = defaultdict(list)
        files_lists: Dict[str, List[Any]] = defaultdict(list)

        for raw in chunks:
            raw = raw.lstrip(b"\r\n")
            if raw in (b"", b"--", b"--\r\n", b"--\n"):
                continue
            if raw.startswith(b"--"):
                continue

            sep = b"\r\n\r\n"
            if sep not in raw:
                if b"\n\n" not in raw:
                    continue
                sep = b"\n\n"
            header_block, content = raw.split(sep, 1)
            # Strip exactly the CRLF/LF that precedes the boundary
            # delimiter (one line break, not arbitrary trailing
            # whitespace). ``size`` and the bytes on disk are now
            # consistent. See the matching note in
            # ``parse_multipart_body_streaming``.
            crlf = _rstrip_crlf_len(content)
            content = content[: len(content) - crlf]
            size = len(content)

            headers = _parse_part_headers(header_block)
            disp = headers.get("content-disposition", "")
            part_ct = headers.get("content-type", "application/octet-stream")

            name_m = RequestParser._DISP_NAME_RE.search(disp)
            if not name_m:
                continue
            field_name = name_m.group(1)

            filename_m = RequestParser._DISP_FILENAME_RE.search(disp)
            if filename_m is not None:
                if filename_m.group(1) == "":
                    continue
                fname = _safe_upload_filename(filename_m.group(1))
                temp_path = None
                payload = None
                if size > UPLOAD_MEMORY_LIMIT:
                    _ensure_temp_dir()
                    temp_path = get_temp_dir() / f"upload_{uuid.uuid4().hex}.part"
                    with open(temp_path, "wb") as fh:
                        fh.write(content)
                else:
                    payload = content
                from .flameo import File
                files_lists[field_name].append(
                    File(
                        file_content=payload,
                        temp_path=temp_path,
                        filename=fname,
                        content_type=part_ct,
                        field_name=field_name,
                        size=size,
                    )
                )
            else:
                fields_lists[field_name].append(_decode_field(content))

        fields: Dict[str, Any] = {
            k: (v[0] if len(v) == 1 else v) for k, v in fields_lists.items()
        }
        files: Dict[str, Any] = {
            k: (v[0] if len(v) == 1 else v) for k, v in files_lists.items()
        }
        return fields, files
