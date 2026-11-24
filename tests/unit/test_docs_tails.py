"""Second-wave tails for flameo/docs.py (tests only)."""

import datetime as dt
import importlib
import inspect
import sys
import typing
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import flameo.docs as docs
from flameo.docs import (
    DocsRegistry,
    RouteDoc,
    _build_responses,
    _docstrings,
    _hoist_model_schema,
    _model_ref,
    _type_to_schema,
    build_openapi,
    normalize_docs_path,
)
from flameo.flameo import File


class Plain:
    pass


class Sub(BaseModel):
    photo: File


class Top(BaseModel):
    sub: Sub
    name: str = "x"


def _handler():
    """Summ."""


def test_add_tag_updates_blank_description():
    reg = DocsRegistry()
    reg.add_tag("b")
    assert reg.tag_descriptions() == {"b": None}
    reg.add_tag("b", "D")
    assert reg.tag_descriptions() == {"b": "D"}


def test_type_to_schema_plain_class_and_bare_dict():
    from typing import Tuple

    assert _type_to_schema(Plain) == {"type": "string"}
    assert _type_to_schema(Tuple[int, str]) == {"type": "string"}
    assert _type_to_schema(typing.Dict) == {
        "type": "object",
        "additionalProperties": True,
    }


def test_type_to_schema_without_uuid_module(monkeypatch):
    import uuid

    monkeypatch.setitem(sys.modules, "uuid", None)
    assert _type_to_schema(uuid.UUID) == {"type": "string"}


def test_type_to_schema_issubclass_typeerror(monkeypatch):
    import builtins

    real_issubclass = builtins.issubclass

    def boom(a, b):
        if a is Plain:
            raise TypeError("boom")
        return real_issubclass(a, b)

    monkeypatch.setattr(builtins, "issubclass", boom)
    assert _type_to_schema(Plain) == {"type": "string"}


def test_function_plain_schema_branches(monkeypatch):
    from pydantic.json_schema import GenerateJsonSchema

    gen = docs._DocsJsonSchema()
    assert gen.function_plain_schema({"function": File._pydantic_validate}) == {
        "type": "string",
        "format": "binary",
    }
    assert gen.function_plain_schema(
        {"function": {"function": File._pydantic_validate}}
    ) == {"type": "string", "format": "binary"}

    def fail(self, schema):
        from pydantic.errors import PydanticInvalidForJsonSchema

        raise PydanticInvalidForJsonSchema("nope")

    monkeypatch.setattr(GenerateJsonSchema, "function_plain_schema", fail)
    assert gen.function_plain_schema({"function": lambda v: v}) == {}


def test_hoist_model_schema_fallbacks(monkeypatch):
    schemas = {}
    name = _hoist_model_schema(Top, schemas)
    assert name == "Top"
    assert "Sub" in schemas

    class Flaky(BaseModel):
        x: int = 0

    def boom(*a, **k):
        raise RuntimeError("schema boom")

    monkeypatch.setattr(Flaky, "model_json_schema", boom)
    schemas2 = {}
    assert _hoist_model_schema(Flaky, schemas2) == "Flaky"
    assert schemas2["Flaky"] == {"type": "object"}

    monkeypatch.setattr(Flaky, "model_json_schema", lambda *a, **k: ["not", "a", "dict"])
    schemas3 = {}
    assert _hoist_model_schema(Flaky, schemas3) == "Flaky"
    assert schemas3["Flaky"] == {"type": "object"}

    monkeypatch.setattr(docs, "_PYDANTIC_JSON_SCHEMA", False)
    schemas4 = {}
    assert _hoist_model_schema(Flaky, schemas4) == "Flaky"


def test_docs_without_pydantic_json_schema():
    blocked = sys.modules.get("pydantic.json_schema", "__absent__")
    sys.modules["pydantic.json_schema"] = None
    try:
        reloaded = importlib.reload(docs)
        assert reloaded._DocsJsonSchema is None
        assert reloaded._PYDANTIC_JSON_SCHEMA is False
        assert reloaded._hoist_model_schema(Top, {}) == "Top"
    finally:
        if blocked == "__absent__":
            sys.modules.pop("pydantic.json_schema", None)
        else:
            sys.modules["pydantic.json_schema"] = blocked
        importlib.reload(docs)


def test_docstrings_partial_and_getdoc_failure(monkeypatch):
    def h():
        """Only summary."""

    assert _docstrings(h, "S", None) == ("S", None)

    def boom(handler):
        raise RuntimeError("getdoc boom")

    monkeypatch.setattr(inspect, "getdoc", boom)
    assert _docstrings(_handler, None, None) == (None, None)


def test_build_responses_ignores_unknown_shape():
    out = _build_responses(RouteDoc(method="GET", path="/x", handler=_handler,
                                    responses={200: 5}), {}, False)
    assert out["200"] == {"description": "OK"}


def test_build_openapi_owner_tag_and_shared_tags():
    reg = DocsRegistry()
    reg.add(RouteDoc(method="GET", path="/a", handler=_handler, tags=["t"]))
    reg.add(RouteDoc(method="GET", path="/b", handler=_handler, tags=["t"]))
    reg.add(RouteDoc(method="GET", path="/c", handler=_handler,
                     tags=[], owner_tag="o"))
    spec = build_openapi(SimpleNamespace(_docs_registry=reg))
    assert spec["paths"]["/c"]["get"]["tags"] == ["o"]
    assert spec["tags"][0] == {"name": "t"}


def test_asset_handler_open_failure(tmp_path):
    import asyncio

    from flameo.docs import _make_asset_handler

    class _Res:
        def __init__(self):
            self.status_code = 200
            self.body = None

        def setHeader(self, k, v):
            pass

        def status(self, code):
            self.status_code = code
            return self

        async def send(self, data):
            self.body = data

    handler = _make_asset_handler(str(tmp_path))
    res = _Res()
    asyncio.run(handler(SimpleNamespace(headers={}), res))
    assert res.status_code == 404


def test_asset_handler_etag_variants(tmp_path):
    import asyncio

    from flameo.docs import _make_asset_handler

    f = tmp_path / "a.js"
    f.write_bytes(b"var x=1;")

    class _Req:
        def __init__(self, inm=""):
            self.headers = {"if-none-match": inm}

    class _Res:
        def __init__(self):
            self.status_code = 200
            self.headers = {}
            self.body = None

        def setHeader(self, k, v):
            self.headers[k] = v

        def status(self, code):
            self.status_code = code
            return self

        async def send(self, data):
            self.body = data

    handler = _make_asset_handler(str(f))
    res = _Res()
    asyncio.run(handler(_Req(), res))
    etag = res.headers["ETag"]
    weak = "W/" + etag[2:] if etag.startswith("W/") else etag

    res2 = _Res()
    asyncio.run(handler(_Req("*"), res2))
    assert res2.status_code == 304

    res3 = _Res()
    asyncio.run(handler(_Req(f'"nope", {etag}'), res3))
    assert res3.status_code == 304

    res4 = _Res()
    asyncio.run(handler(_Req('"nope", "alsono"'), res4))
    assert res4.status_code == 200

    res5 = _Res()
    asyncio.run(handler(_Req(weak), res5))
    assert res5.status_code == 304


def test_register_docs_route_variants(tmp_path, monkeypatch):
    import asyncio

    from flameo.docs import _make_asset_handler  # noqa: F401

    def routes_of(app):
        return [p for _, p, _ in app.router.routes]

    def make_app(**kw):
        base = dict(_docs_ui_path=None, _docs_redoc_path=None,
                    _docs_openapi_path=None, title="T")
        base.update(kw)
        holder = {}
        app = SimpleNamespace(
            **base,
            router=SimpleNamespace(
                routes=[],
                add_route=lambda m, p, h: holder.setdefault("app", app).router.routes.append((m, p, h)),
            ),
        )
        return app

    (tmp_path / "swagger-ui").mkdir()
    (tmp_path / "swagger-ui" / "sub").mkdir()
    (tmp_path / "swagger-ui" / "a.js").write_text("x")
    (tmp_path / "redoc").mkdir()
    (tmp_path / "redoc" / "sub").mkdir()
    (tmp_path / "redoc" / "b.js").write_text("y")
    monkeypatch.setattr(docs, "_STATIC_DIR", str(tmp_path))

    ui_only = make_app(_docs_ui_path="/docs")
    docs.register_docs_routes(ui_only)
    assert "/docs" in routes_of(ui_only)
    assert "/openapi.json" not in routes_of(ui_only)
    assert any("swagger-ui/a.js" in p for _, p, _ in ui_only.router.routes)

    ui_redoc = make_app(_docs_ui_path="/docs", _docs_redoc_path="/redoc")
    docs.register_docs_routes(ui_redoc)
    assert "/redoc" in routes_of(ui_redoc)
    assert any("redoc/b.js" in p for _, p, _ in ui_redoc.router.routes)

    bare = make_app(_docs_ui_path="/docs", _docs_redoc_path="/redoc")
    monkeypatch.setattr(docs, "_STATIC_DIR", str(tmp_path / "nonexistent"))
    docs.register_docs_routes(bare)
    assert "/docs" in routes_of(bare) and "/redoc" in routes_of(bare)

    class _Res:
        def __init__(self):
            self.status_code = 200
            self.body = None

        def setHeader(self, k, v):
            pass

        def status(self, code):
            self.status_code = code
            return self

        async def send(self, data):
            self.body = data

        async def json(self, data):
            self.body = data

    ui_handlers = {p: h for _, p, h in ui_only.router.routes}
    asyncio.run(ui_handlers["/docs"](SimpleNamespace(), _Res()))
    redoc_handlers = {p: h for _, p, h in ui_redoc.router.routes}
    asyncio.run(redoc_handlers["/redoc"](SimpleNamespace(), _Res()))


def test_normalize_docs_path_edge():
    assert normalize_docs_path("x", "/") == "/"
    assert normalize_docs_path("x", "///docs///") == "///docs"
