import datetime
import uuid
from enum import Enum
from types import SimpleNamespace
from typing import Dict, List, Optional, Union

import pytest
from pydantic import BaseModel

import flameo.docs as docs
from flameo.docs import (
    DocsRegistry,
    RouteDoc,
    _build_responses,
    _collect_models,
    _docstrings,
    _hoist_model_schema,
    _info,
    _make_asset_handler,
    _model_has_files,
    _model_ref,
    _param_from_decl,
    _type_to_schema,
    build_openapi,
    normalize_docs_path,
    redoc_html,
    register_docs_routes,
    swagger_ui_html,
    to_openapi_path,
)
from flameo.flameo import File


class Item(BaseModel):
    name: str
    price: float = 0.0


class WithFile(BaseModel):
    photo: File
    title: str = "t"


def _handler():
    """Sample handler."""


def _rec(**kw):
    base = dict(method="GET", path="/x", handler=_handler)
    base.update(kw)
    return RouteDoc(**base)


def test_routedoc_rebased():
    r = _rec(path="/a", summary="s", tags=["t"])
    out = r.rebased("/b", "owner")
    assert out.path == "/b" and out.owner_tag == "owner" and out.summary == "s"


def test_registry_add_tag_cache():
    reg = DocsRegistry()
    reg.add(_rec())
    assert len(reg.records()) == 1
    reg.add_tag("", "x")
    assert reg.tag_descriptions() == {}
    reg.add_tag("a", "desc")
    assert reg.tag_descriptions() == {"a": "desc"}
    reg.add_tag("a", "other")
    assert reg.tag_descriptions() == {"a": "desc"}
    reg.add_tag("b")
    assert reg.cache is None
    reg.set_cache({"x": 1})
    assert reg.cache == {"x": 1}
    reg.invalidate()
    assert reg.cache is None
    assert reg.tag_descriptions()["b"] is None


def test_to_openapi_path():
    assert to_openapi_path("") == ("/", [])
    assert to_openapi_path("/users/:id") == ("/users/{id}", [("id", "str")])
    assert to_openapi_path("/f/{name:int}") == ("/f/{name}", [("name", "int")])
    assert to_openapi_path("/a//b") == ("/a/b", [])
    assert to_openapi_path("/w/*rest") == ("/w/*rest", [])


def test_type_to_schema_scalars():
    assert _type_to_schema(str) == {"type": "string"}
    assert _type_to_schema(int) == {"type": "integer"}
    assert _type_to_schema(float) == {"type": "number"}
    assert _type_to_schema(bool) == {"type": "boolean"}
    assert _type_to_schema(bytes) == {"type": "string", "format": "byte"}
    assert _type_to_schema(dict) == {"type": "object"}
    assert _type_to_schema(list) == {"type": "array", "items": {}}
    assert _type_to_schema(object) == {}
    assert _type_to_schema(type(None)) == {"type": "null"}
    assert _type_to_schema(None) == {"type": "null"}
    assert _type_to_schema(datetime.datetime)["format"] == "date-time"
    assert _type_to_schema(datetime.date)["format"] == "date"
    assert _type_to_schema(datetime.time)["format"] == "time"
    assert _type_to_schema(uuid.UUID) == {"type": "string", "format": "uuid"}
    assert _type_to_schema(List[int]) == {"type": "array", "items": {"type": "integer"}}
    assert _type_to_schema(Dict[str, int]) == {"type": "object", "additionalProperties": True}
    assert _type_to_schema(Optional[int]) == {"type": "integer"}
    assert _type_to_schema(Union[int, str])["anyOf"] == [{"type": "integer"}, {"type": "string"}]
    assert _type_to_schema(123) == {"type": "string"}


def test_type_to_schema_enums():
    class S(str, Enum):
        A = "a"

    class I(int, Enum):
        A = 1

    class B(Enum):
        A = True
        B = False

    class F(float, Enum):
        A = 1.5

    class M(Enum):
        A = object()

    assert _type_to_schema(S)["enum"] == ["a"]
    assert _type_to_schema(I)["type"] == "integer"
    assert _type_to_schema(B)["type"] == "boolean"
    assert _type_to_schema(F)["type"] == "number"
    assert "enum" in _type_to_schema(M)


def test_param_from_decl_shapes():
    assert _param_from_decl("p", "x", "query")["schema"] == {"type": "string"}
    p = _param_from_decl("p", int, "query")
    assert p["required"] is False
    p = _param_from_decl("p", (int, True), "query")
    assert p["required"] is True
    p = _param_from_decl("p", (int, True, "Page"), "query")
    assert p["description"] == "Page"
    p = _param_from_decl("p", {"type": int, "required": True, "description": "d"}, "query")
    assert p["required"] is True and p["description"] == "d"
    p = _param_from_decl("p", {"type": int, "description": 5}, "path")
    assert p["required"] is True and "description" not in p
    p = _param_from_decl("p", (), "query")
    assert p["schema"] == {"type": "string"}


def test_collect_models_and_files():
    class Sub(BaseModel):
        photo: File

    class Top(BaseModel):
        sub: Sub
        items: List[Sub]
        maybe: Optional[Sub] = None

    assert Sub in _collect_models(Top)
    assert _model_has_files(Top) is True
    assert _model_has_files(Item) is False
    assert _collect_models(None) == []


def test_hoist_and_ref():
    schemas = {}
    ref = _model_ref(Item, schemas)
    assert ref == {"$ref": "#/components/schemas/Item"}
    assert "Item" in schemas
    assert _hoist_model_schema(Item, schemas) == "Item"
    assert _model_ref(Item, schemas) == ref


def test_info_and_docstrings():
    assert _info(SimpleNamespace()) == {"title": "Flameo API", "version": "1.0.0"}
    assert _info(SimpleNamespace(title="T", version="2", description="d"))["description"] == "d"

    def h():
        """Sum.

        Long desc.
        """

    assert _docstrings(h, None, None) == ("Sum.", "Long desc.")
    assert _docstrings(h, "S", "D") == ("S", "D")
    assert _docstrings(lambda: None, None, None) == (None, None)


def test_build_responses_variants():
    schemas = {}
    r = _build_responses(_rec(), schemas, False)
    assert r["200"]["description"] == "OK"
    r = _build_responses(_rec(responses={200: "fine"}), schemas, False)
    assert r["200"]["description"] == "fine"
    r = _build_responses(_rec(responses={200: Item}), schemas, True)
    assert "422" in r and "$ref" in r["200"]["content"]["application/json"]["schema"]
    r = _build_responses(_rec(responses={200: int}), schemas, False)
    assert r["200"]["content"]["application/json"]["schema"] == {"type": "integer"}
    r = _build_responses(_rec(responses={999: None}), schemas, False)
    assert r["999"]["description"] == "Response 999"
    r = _build_responses(_rec(responses={200: {"type": "string"}}), schemas, False)
    assert r["200"]["content"]["application/json"]["schema"] == {"type": "string"}


def test_build_openapi_full():
    reg = DocsRegistry()
    reg.add_tag("t", "Tag T")
    reg.add(_rec(path="/users/:id", query={"page": int}, headers={"x-t": (str, True)},
                 tags=["t"], summary="S", description="D", operation_id="op1"))
    reg.add(_rec(method="POST", path="/items", handler=_handler, body_model=Item,
                 operation_id="op1", responses={201: Item}, deprecated=True))
    reg.add(_rec(method="POST", path="/up", handler=_handler, body_model=WithFile))
    reg.add(_rec(method="BREW", path="/nope", handler=_handler))
    app = SimpleNamespace(_docs_registry=reg, title="T", version="1",
                          description="d", auth_header="Authorization")
    spec = build_openapi(app)
    assert spec["openapi"] == "3.1.0"
    assert "/users/{id}" in spec["paths"]
    assert spec["paths"]["/users/{id}"]["get"]["operationId"] == "op1"
    assert spec["paths"]["/items"]["post"]["operationId"] == "op1_2"
    assert spec["paths"]["/items"]["post"]["deprecated"] is True
    assert spec["paths"]["/up"]["post"]["requestBody"]["content"].keys() == {"multipart/form-data"}
    assert "422" in spec["paths"]["/up"]["post"]["responses"]
    assert spec["security"] == [{"authHeader": []}]
    assert spec["tags"][0]["name"] == "t"
    assert build_openapi(app) is spec
    reg.add(_rec(path="/new", handler=_handler))
    assert build_openapi(app) is not spec


def test_build_openapi_no_registry_and_ids():
    anon = SimpleNamespace(_docs_registry=None)
    assert build_openapi(anon)["paths"] == {}
    reg = DocsRegistry()
    reg.add(RouteDoc(method="GET", path="/a", handler=lambda: None))
    reg.add(RouteDoc(method="get", path="/b", handler=12345))
    spec = build_openapi(SimpleNamespace(_docs_registry=reg))
    ids = [op["operationId"] for p in spec["paths"].values() for op in p.values()]
    assert len(set(ids)) == 2


def test_html_helpers_escape():
    h = swagger_ui_html("/openapi.json", "<API>")
    assert "&lt;API&gt;" in h and "/openapi.json" in h
    h = redoc_html("/spec", '"q"')
    assert "spec-url=" in h


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

    async def json(self, data):
        import json as _json

        self.setHeader("Content-Type", "application/json")
        self.body = _json.dumps(data)
        return self.body


def test_asset_handler(tmp_path):
    import asyncio

    f = tmp_path / "a.js"
    f.write_bytes(b"var x=1;")
    handler = _make_asset_handler(str(f))
    req, res = _Req(), _Res()
    asyncio.run(handler(req, res))
    assert res.body == b"var x=1;"
    etag = res.headers["ETag"]
    req2, res2 = _Req(etag), _Res()
    asyncio.run(handler(req2, res2))
    assert res2.status_code == 304
    bad = _make_asset_handler(str(tmp_path / "missing.js"))
    req3, res3 = _Req(), _Res()
    asyncio.run(bad(req3, res3))
    assert res3.status_code == 404


def test_register_docs_routes_and_paths(tmp_path, monkeypatch):
    import asyncio

    app = SimpleNamespace(_docs_ui_path=None, router=SimpleNamespace(routes=[],
                          add_route=lambda m, p, h: app.router.routes.append((m, p, h))))
    register_docs_routes(app)
    assert app.router.routes == []
    (tmp_path / "swagger-ui").mkdir()
    (tmp_path / "swagger-ui" / "a.js").write_text("x")
    (tmp_path / "redoc").mkdir()
    (tmp_path / "redoc" / "b.js").write_text("y")
    monkeypatch.setattr(docs, "_STATIC_DIR", str(tmp_path))
    app2 = SimpleNamespace(_docs_ui_path="/docs", _docs_redoc_path="/redoc",
                           _docs_openapi_path="/openapi.json", title="T",
                           router=SimpleNamespace(routes=[],
                           add_route=lambda m, p, h: app2.router.routes.append((m, p, h))))
    register_docs_routes(app2)
    paths = [p for _, p, _ in app2.router.routes]
    assert "/openapi.json" in paths and "/docs" in paths and "/redoc" in paths
    assert any("swagger-ui/a.js" in p for p in paths)
    assert any("redoc/b.js" in p for p in paths)
    openapi_handler = [h for m, p, h in app2.router.routes if p == "/openapi.json"][0]
    res = _Res()
    asyncio.run(openapi_handler(SimpleNamespace(), res))
    assert res.body is not None


def test_normalize_docs_path():
    assert normalize_docs_path("x", None) is None
    assert normalize_docs_path("x", "/docs/") == "/docs"
    assert normalize_docs_path("x", "docs") == "/docs"
    with pytest.raises(ValueError):
        normalize_docs_path("x", "   ")
    with pytest.raises(ValueError):
        normalize_docs_path("x", 123)
