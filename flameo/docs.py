from __future__ import annotations

import datetime
import html
import inspect
import json
import mimetypes
import os
import re
import types
import typing
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, get_args, get_origin

from .validation import (
    _get_file_class,
    _is_file_annotation,
    _unwrap_list,
    _unwrap_optional,
    is_pydantic_model,
)

try:
    from pydantic.json_schema import GenerateJsonSchema
    from pydantic.errors import PydanticInvalidForJsonSchema
    _PYDANTIC_JSON_SCHEMA = True
except Exception:
    GenerateJsonSchema = None  # type: ignore[assignment]
    PydanticInvalidForJsonSchema = Exception  # type: ignore[assignment]
    _PYDANTIC_JSON_SCHEMA = False

ASSET_PREFIX = "/_flameo/docs"
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_STANDARD_METHODS = frozenset(
    ("get", "put", "post", "delete", "options", "head", "patch", "trace")
)

_PARAM_SEGMENT_RE = re.compile(
    r"^(?::|\{)([A-Za-z_][A-Za-z0-9_]*)(?::([A-Za-z_][A-Za-z0-9_]*))?(?:\})?$"
)

_CONVERTER_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "bool": {"type": "boolean"},
    "slug": {"type": "string"},
    "alphanum": {"type": "string"},
    "path": {"type": "string"},
}

_STATUS_PHRASES: Dict[str, str] = {
    "200": "OK",
    "201": "Created",
    "202": "Accepted",
    "204": "No Content",
    "301": "Moved Permanently",
    "302": "Found",
    "304": "Not Modified",
    "400": "Bad Request",
    "401": "Unauthorized",
    "403": "Forbidden",
    "404": "Not Found",
    "405": "Method Not Allowed",
    "409": "Conflict",
    "415": "Unsupported Media Type",
    "422": "Validation Error",
    "429": "Too Many Requests",
    "500": "Internal Server Error",
    "502": "Bad Gateway",
    "503": "Service Unavailable",
}

_VALIDATION_ERROR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "error": {"type": "string"},
        "details": {},
    },
}


# ---------------------------------------------------------------------
# Route metadata
# ---------------------------------------------------------------------


@dataclass
class RouteDoc:
    """Documentation metadata captured for a single registered route."""

    method: str
    path: str
    handler: Any
    body_model: Any = None
    summary: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    responses: Optional[Dict[Any, Any]] = None
    query: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, Any]] = None
    deprecated: bool = False
    operation_id: Optional[str] = None
    owner_tag: Optional[str] = None

    def rebased(self, path: str, owner_tag: Optional[str]) -> "RouteDoc":
        """Copy of this record for a router mounted at ``path``'s prefix."""
        return RouteDoc(
            method=self.method,
            path=path,
            handler=self.handler,
            body_model=self.body_model,
            summary=self.summary,
            description=self.description,
            tags=self.tags,
            responses=self.responses,
            query=self.query,
            headers=self.headers,
            deprecated=self.deprecated,
            operation_id=self.operation_id,
            owner_tag=owner_tag,
        )


class DocsRegistry:
    """Ordered collection of route records + tag descriptions.

    Every ``Flameo.route`` / ``Router.route`` call appends here when the
    app has docs enabled. The built spec is cached and invalidated on
    every mutation.
    """

    __slots__ = ("_records", "_tag_descriptions", "_cache")

    def __init__(self) -> None:
        self._records: List[RouteDoc] = []
        self._tag_descriptions: Dict[str, Optional[str]] = {}
        self._cache: Optional[dict] = None

    def add(self, record: RouteDoc) -> None:
        self._records.append(record)
        self._cache = None

    def add_tag(self, name: str, description: Optional[str] = None) -> None:
        if not name:
            return
        if name not in self._tag_descriptions:
            self._tag_descriptions[name] = description
            self._cache = None
        elif description and not self._tag_descriptions[name]:
            self._tag_descriptions[name] = description
            self._cache = None

    def records(self) -> List[RouteDoc]:
        return list(self._records)

    def tag_descriptions(self) -> Dict[str, Optional[str]]:
        return dict(self._tag_descriptions)

    @property
    def cache(self) -> Optional[dict]:
        return self._cache

    def set_cache(self, spec: dict) -> None:
        self._cache = spec

    def invalidate(self) -> None:
        self._cache = None


# ---------------------------------------------------------------------
# Path / type helpers
# ---------------------------------------------------------------------


def to_openapi_path(path: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Convert a Flameo route path to an OpenAPI path + param list.

    ``/users/:id`` -> ``("/users/{id}", [("id", "str")])``.
    """
    if not path:
        return "/", []
    out: List[str] = []
    params: List[Tuple[str, str]] = []
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        candidate = segment[1:] if segment.startswith("*") else segment
        m = _PARAM_SEGMENT_RE.match(candidate)
        if m:
            name = m.group(1)
            params.append((name, m.group(2) or "str"))
            out.append("{" + name + "}")
        else:
            out.append(segment)
    return "/" + "/".join(out), params


def _type_to_schema(typ: Any) -> Dict[str, Any]:
    typ, _ = _unwrap_optional(typ)
    if typ is None or typ is type(None):
        return {"type": "null"}
    origin = get_origin(typ)
    if origin is not None:
        if origin is list:
            args = get_args(typ)
            item = _type_to_schema(args[0]) if args else {"type": "string"}
            return {"type": "array", "items": item}
        if origin is dict:
            return {"type": "object", "additionalProperties": True}
        if origin is typing.Union or origin is types.UnionType:
            alts = [_type_to_schema(a) for a in get_args(typ) if a is not type(None)]
            if len(alts) == 1:  # pragma: no cover - single-member unions unwrap above
                return alts[0]
            return {"anyOf": alts}
    if isinstance(typ, type):
        if typ is str:
            return {"type": "string"}
        if typ is int:
            return {"type": "integer"}
        if typ is float:
            return {"type": "number"}
        if typ is bool:
            return {"type": "boolean"}
        if typ is bytes:
            return {"type": "string", "format": "byte"}
        if typ is dict:
            return {"type": "object"}
        if typ is list:
            return {"type": "array", "items": {}}
        if typ is object:
            return {}
        if typ is datetime.datetime:
            return {"type": "string", "format": "date-time"}
        if typ is datetime.date:
            return {"type": "string", "format": "date"}
        if typ is datetime.time:
            return {"type": "string", "format": "time"}
        try:
            import uuid as _uuid_mod

            if typ is _uuid_mod.UUID:
                return {"type": "string", "format": "uuid"}
        except Exception:
            pass
        try:
            if issubclass(typ, Enum):
                values = [m.value for m in typ]
                if all(isinstance(v, str) for v in values):
                    schema: Dict[str, Any] = {"type": "string"}
                elif all(isinstance(v, bool) for v in values):
                    schema = {"type": "boolean"}
                elif all(isinstance(v, int) for v in values):
                    schema = {"type": "integer"}
                elif all(isinstance(v, float) for v in values):
                    schema = {"type": "number"}
                else:
                    schema = {}
                schema["enum"] = values
                return schema
        except TypeError:
            pass
    return {"type": "string"}


def _param_from_decl(name: str, decl: Any, location: str) -> Dict[str, Any]:
    """Build one OpenAPI parameter from a developer-declared entry.

    Accepted shapes::

        "page" -> {"type": "string"}          # optional
        int    -> {"type": "integer"}         # optional
        (int, True)                           # required
        (int, True, "Page number")            # required + description
        {"type": int, "required": True, "description": "..."}
    """
    required = False
    description: Optional[str] = None
    if isinstance(decl, dict):
        typ = decl.get("type", str)
        required = bool(decl.get("required", False))
        description = decl.get("description")
        if not isinstance(description, str):
            description = None
    elif isinstance(decl, tuple):
        typ = decl[0] if decl else str
        if len(decl) > 1 and isinstance(decl[1], bool):
            required = decl[1]
        if len(decl) > 2 and isinstance(decl[2], str):
            description = decl[2]
    else:
        typ = decl
    param: Dict[str, Any] = {
        "name": name,
        "in": location,
        "required": True if location == "path" else required,
        "schema": _type_to_schema(typ),
    }
    if description:
        param["description"] = description
    return param


# ---------------------------------------------------------------------
# Pydantic model -> OpenAPI schema
# ---------------------------------------------------------------------


if _PYDANTIC_JSON_SCHEMA:

    class _DocsJsonSchema(GenerateJsonSchema):  # type: ignore[misc,valid-type]
        """JSON-schema generator that understands ``flameo.flameo.File``.

        ``File`` uses a plain validator function, which stock pydantic
        refuses to serialize (``PydanticInvalidForJsonSchema``). Map it
        to the OpenAPI binary-string form; any other plain validator
        degrades to an empty (any) schema instead of failing the whole
        spec build.
        """

        def function_plain_schema(self, schema):
            fn = schema.get("function")
            if isinstance(fn, dict):
                fn = fn.get("function")
            file_cls = _get_file_class()
            if file_cls is not None and getattr(fn, "__self__", None) is file_cls:
                return {"type": "string", "format": "binary"}
            try:
                return super().function_plain_schema(schema)
            except PydanticInvalidForJsonSchema:
                return {}

else:
    _DocsJsonSchema = None  # type: ignore[assignment,misc]


def _collect_models(root: Any) -> List[Any]:
    """Root model plus every pydantic model reachable through its fields."""
    found: List[Any] = []
    seen: set = set()

    def walk(annotation: Any) -> None:
        if annotation is None:
            return
        if is_pydantic_model(annotation):
            if annotation in seen:
                return
            seen.add(annotation)
            found.append(annotation)
            for fld in getattr(annotation, "model_fields", {}).values():
                walk(fld.annotation)
            return
        origin = get_origin(annotation)
        if origin is not None:
            for arg in get_args(annotation):
                if arg is type(None):
                    continue
                walk(arg)

    walk(root)
    return found


def _model_has_files(root: Any) -> bool:
    for model in _collect_models(root):
        for fld in getattr(model, "model_fields", {}).values():
            inner, _ = _unwrap_list(fld.annotation)
            inner, _ = _unwrap_optional(inner)
            if _is_file_annotation(inner):
                return True
    return False


def _hoist_model_schema(model: Any, schemas: Dict[str, Any]) -> str:
    """Register ``model``'s JSON schema (and its ``$defs``) in ``schemas``.

    Returns the component name the model is stored under.
    """
    name = getattr(model, "__name__", None) or "Model"
    if name in schemas:
        return name
    raw: Dict[str, Any]
    if _PYDANTIC_JSON_SCHEMA:
        try:
            raw = model.model_json_schema(
                ref_template="#/components/schemas/{model}",
                schema_generator=_DocsJsonSchema,
            )
        except Exception:
            raw = {"type": "object"}
    else:
        raw = {"type": "object"}
    if not isinstance(raw, dict):
        raw = {"type": "object"}
    defs = raw.pop("$defs", None)
    if isinstance(defs, dict):
        for def_name, def_schema in defs.items():
            schemas.setdefault(def_name, def_schema)
    schemas.setdefault(name, raw)
    return name


def _model_ref(model: Any, schemas: Dict[str, Any]) -> Dict[str, str]:
    name = _hoist_model_schema(model, schemas)
    return {"$ref": f"#/components/schemas/{name}"}


# ---------------------------------------------------------------------
# Spec assembly
# ---------------------------------------------------------------------


def _info(app: Any) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "title": getattr(app, "title", None) or "Flameo API",
        "version": str(getattr(app, "version", None) or "1.0.0"),
    }
    description = getattr(app, "description", None)
    if description:
        info["description"] = description
    return info


def _docstrings(handler: Any, summary, description):
    if summary is not None and description is not None:
        return summary, description
    try:
        text = inspect.getdoc(handler)
    except Exception:
        text = None
    if not text:
        return summary, description
    lines = text.strip().splitlines()
    if summary is None and lines:
        summary = lines[0].strip() or None
    if description is None and len(lines) > 1:
        rest = "\n".join(lines[1:]).strip()
        description = rest or None
    return summary, description


def _build_responses(
    record: RouteDoc, schemas: Dict[str, Any], has_body: bool
) -> Dict[str, Any]:
    declared = record.responses or {}
    responses: Dict[str, Any] = {}
    if not declared:
        declared = {200: None}
    for code, value in declared.items():
        code_s = str(code)
        phrase = _STATUS_PHRASES.get(code_s, f"Response {code_s}")
        entry: Dict[str, Any] = {"description": phrase}
        if value is None:
            entry["content"] = {"application/json": {"schema": {"type": "object"}}}
        elif isinstance(value, str):
            entry["description"] = value
        elif is_pydantic_model(value):
            entry["content"] = {
                "application/json": {"schema": _model_ref(value, schemas)}
            }
        elif isinstance(value, type):
            entry["content"] = {
                "application/json": {"schema": _type_to_schema(value)}
            }
        elif isinstance(value, dict):
            entry["content"] = {"application/json": {"schema": value}}
        responses[code_s] = entry
    if has_body and "422" not in responses:
        responses["422"] = {
            "description": _STATUS_PHRASES["422"],
            "content": {"application/json": {"schema": _VALIDATION_ERROR_SCHEMA}},
        }
    return responses


def build_openapi(app: Any) -> Dict[str, Any]:
    """Build (and cache) the OpenAPI 3.1 document for ``app``."""
    registry = getattr(app, "_docs_registry", None)
    if registry is None:
        records: List[RouteDoc] = []
        tag_descriptions: Dict[str, Optional[str]] = {}
        cached = None
    else:
        cached = registry.cache
        if cached is not None:
            return cached
        records = registry.records()
        tag_descriptions = registry.tag_descriptions()

    schemas: Dict[str, Any] = {}
    paths: Dict[str, Any] = {}
    tag_meta: Dict[str, Optional[str]] = {}
    for tag_name, tag_desc in tag_descriptions.items():
        tag_meta[tag_name] = tag_desc
    used_tags: List[str] = []
    op_ids: set = set()

    for index, record in enumerate(records):
        method = (record.method or "").upper()
        if method.lower() not in _STANDARD_METHODS:
            continue
        op_path, path_params = to_openapi_path(record.path)
        summary, description = _docstrings(
            record.handler, record.summary, record.description
        )

        parameters: List[Dict[str, Any]] = []
        for pname, conv in path_params:
            parameters.append(
                {
                    "name": pname,
                    "in": "path",
                    "required": True,
                    "schema": dict(_CONVERTER_SCHEMAS.get(conv, {"type": "string"})),
                }
            )
        for pname, decl in (record.query or {}).items():
            parameters.append(_param_from_decl(pname, decl, "query"))
        for pname, decl in (record.headers or {}).items():
            parameters.append(_param_from_decl(pname, decl, "header"))

        tags = list(record.tags) if record.tags else None
        if not tags and record.owner_tag:
            tags = [record.owner_tag]

        base_id = record.operation_id
        if not base_id:
            base_id = getattr(record.handler, "__name__", None)
        if not base_id:
            base_id = f"{method.lower()}_{index}"
        op_id = base_id
        suffix = 2
        while op_id in op_ids:
            op_id = f"{base_id}_{suffix}"
            suffix += 1
        op_ids.add(op_id)

        operation: Dict[str, Any] = {"operationId": op_id}
        if summary:
            operation["summary"] = summary
        if description:
            operation["description"] = description
        if tags:
            operation["tags"] = tags
        if record.deprecated:
            operation["deprecated"] = True
        if parameters:
            operation["parameters"] = parameters

        has_body = record.body_model is not None
        if has_body:
            content_type = (
                "multipart/form-data" if _model_has_files(record.body_model)
                else "application/json"
            )
            operation["requestBody"] = {
                "required": True,
                "content": {
                    content_type: {"schema": _model_ref(record.body_model, schemas)}
                },
            }

        operation["responses"] = _build_responses(record, schemas, has_body)

        item = paths.setdefault(op_path, {})
        item[method.lower()] = operation

        if tags:
            for tag in tags:
                if tag not in tag_meta:
                    tag_meta[tag] = None
                if tag not in used_tags:
                    used_tags.append(tag)

    spec: Dict[str, Any] = {
        "openapi": "3.1.0",
        "info": _info(app),
        "paths": paths,
    }
    ordered_tags = [t for t in tag_meta if t in used_tags] + [
        t for t in used_tags if t not in tag_meta
    ]
    if ordered_tags:
        spec["tags"] = [
            {"name": t, **({"description": tag_meta[t]} if tag_meta.get(t) else {})}
            for t in ordered_tags
        ]

    components: Dict[str, Any] = {}
    if schemas:
        components["schemas"] = schemas
    auth_header = getattr(app, "auth_header", None)
    if auth_header:
        components["securitySchemes"] = {
            "authHeader": {
                "type": "apiKey",
                "in": "header",
                "name": auth_header,
                "description": (
                    "Send the complete header value, prefix included "
                    '(e.g. "Bearer <token>" or "Token <token>").'
                ),
            }
        }
        spec["security"] = [{"authHeader": []}]
    if components:
        spec["components"] = components

    if registry is not None:
        registry.set_cache(spec)
    return spec


# ---------------------------------------------------------------------
# HTML + asset serving
# ---------------------------------------------------------------------


def swagger_ui_html(spec_url: str, title: str) -> str:
    t = html.escape(title or "Flameo API")
    s = json.dumps(spec_url)
    a = ASSET_PREFIX + "/swagger-ui"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{t} - Swagger UI</title>
<link rel="icon" type="image/png" sizes="32x32" href="{a}/favicon-32x32.png"/>
<link rel="icon" type="image/png" sizes="16x16" href="{a}/favicon-16x16.png"/>
<link rel="stylesheet" href="{a}/swagger-ui.css"/>
</head>
<body>
<div id="swagger-ui"></div>
<script src="{a}/swagger-ui-bundle.js" crossorigin></script>
<script src="{a}/swagger-ui-standalone-preset.js" crossorigin></script>
<script>
window.onload = function () {{
  window.ui = SwaggerUIBundle({{
    url: {s},
    dom_id: "#swagger-ui",
    deepLinking: true,
    tryItOutEnabled: true,
    persistAuthorization: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    layout: "StandaloneLayout"
  }});
}};
</script>
</body>
</html>
"""


def redoc_html(spec_url: str, title: str) -> str:
    t = html.escape(title or "Flameo API")
    s = html.escape(spec_url, quote=True)
    a = ASSET_PREFIX + "/redoc"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{t} - ReDoc</title>
<style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url="{s}"></redoc>
<script src="{a}/redoc.standalone.js"></script>
</body>
</html>
"""


def _make_asset_handler(file_path: str):
    holder: Dict[str, Any] = {}

    async def handler(req, res):
        try:
            st = os.stat(file_path)
        except OSError:
            await res.status(404).send("Not found")
            return
        data = holder.get("data")
        if (
            data is None
            or holder.get("mtime_ns") != st.st_mtime_ns
            or holder.get("size") != st.st_size
        ):
            try:
                with open(file_path, "rb") as fh:
                    data = fh.read()
            except OSError:
                await res.status(404).send("Not found")
                return
            holder.update(data=data, mtime_ns=st.st_mtime_ns, size=st.st_size)
        etag = 'w/"{:x}-{:x}"'.format(st.st_size, st.st_mtime_ns)
        inm = (req.headers.get("if-none-match") or "").lower()
        if inm:
            for candidate in inm.split(","):
                candidate = candidate.strip()
                if candidate in ("*", etag):
                    await res.status(304).send("")
                    return
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        res.setHeader("Content-Type", content_type)
        res.setHeader("ETag", etag)
        res.setHeader("Cache-Control", "public, max-age=3600")
        await res.send(data)

    return handler


def register_docs_routes(app: Any) -> None:
    """Register the Swagger UI / ReDoc / OpenAPI JSON routes on ``app``.

    Called once from ``Flameo.__init__`` when a UI path was configured.
    These routes are added straight to the internal RouterMap so they
    never show up in the generated spec.
    """
    ui_path = getattr(app, "_docs_ui_path", None)
    redoc_path = getattr(app, "_docs_redoc_path", None)
    spec_path = getattr(app, "_docs_openapi_path", None)
    if not ui_path:
        return
    title = getattr(app, "title", None) or "Flameo API"

    if spec_path:
        async def openapi_handler(req, res):
            res.setHeader("Cache-Control", "no-cache")
            await res.json(build_openapi(app))

        app.router.add_route("GET", spec_path, openapi_handler)

    swagger_page = swagger_ui_html(spec_path or "", title)

    async def swagger_handler(req, res):
        res.setHeader("Content-Type", "text/html; charset=utf-8")
        res.setHeader("Cache-Control", "no-cache")
        await res.send(swagger_page)

    app.router.add_route("GET", ui_path, swagger_handler)

    swagger_dir = os.path.join(_STATIC_DIR, "swagger-ui")
    if os.path.isdir(swagger_dir):
        base = ASSET_PREFIX + "/swagger-ui"
        for name in sorted(os.listdir(swagger_dir)):
            full = os.path.join(swagger_dir, name)
            if os.path.isfile(full):
                app.router.add_route(
                    "GET", f"{base}/{name}", _make_asset_handler(full)
                )

    if redoc_path:
        redoc_page = redoc_html(spec_path or "", title)

        async def redoc_handler(req, res):
            res.setHeader("Content-Type", "text/html; charset=utf-8")
            res.setHeader("Cache-Control", "no-cache")
            await res.send(redoc_page)

        app.router.add_route("GET", redoc_path, redoc_handler)

        redoc_dir = os.path.join(_STATIC_DIR, "redoc")
        if os.path.isdir(redoc_dir):
            base = ASSET_PREFIX + "/redoc"
            for name in sorted(os.listdir(redoc_dir)):
                full = os.path.join(redoc_dir, name)
                if os.path.isfile(full):
                    app.router.add_route(
                        "GET", f"{base}/{name}", _make_asset_handler(full)
                    )


def normalize_docs_path(name: str, value: Any) -> Optional[str]:
    """Validate + normalize a docs URL path (leading slash, no trailing)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[flameo] {name} must be a non-empty path string")
    path = value.strip()
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    return path or "/"
