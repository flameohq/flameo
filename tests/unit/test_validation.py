import importlib
import json
import sys
import typing
from typing import List, Optional, Union

import pytest
from pydantic import BaseModel, RootModel

import flameo.validation as validation
from flameo import File

_LIVE_HANDLERS = []


class _DuplicateKeyMap:
    def __init__(self, pairs):
        self._pairs = list(pairs)

    def __bool__(self):
        return bool(self._pairs)

    def items(self):
        return list(self._pairs)


class _NoCacheMeta(dict):
    def get(self, key, default=None):
        return default


def _disable_handler_meta_cache(monkeypatch, app):
    module_globals = type(app).__init__.__globals__
    monkeypatch.setitem(module_globals, "_HANDLER_META", _NoCacheMeta())


def test_module_falls_back_when_pydantic_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "pydantic", None)
    reloaded = importlib.reload(validation)
    try:
        assert reloaded._PYDANTIC_AVAILABLE is False
        assert reloaded.BaseModel is None
        assert reloaded._PydanticValidationError is None
        assert reloaded.is_pydantic_model(object) is False
        with pytest.raises(RuntimeError, match="pydantic"):
            reloaded.validate_model(object, {})
    finally:
        monkeypatch.undo()
        importlib.reload(validation)
    assert validation._PYDANTIC_AVAILABLE is True


def test_get_file_class_returns_none_when_flameo_import_fails(monkeypatch):
    import flameo.flameo as flameo_module

    monkeypatch.setitem(sys.modules, "flameo.flameo", None)
    monkeypatch.setattr(validation, "_FILE_CLASS", None)
    assert validation._get_file_class() is None
    assert validation._is_file_annotation(File) is False
    monkeypatch.undo()
    assert validation._get_file_class() is flameo_module.File


def test_validation_error_formats_summary_from_error_dicts():
    class Payload(BaseModel):
        name: str

    errors = [{"loc": ("body", "name"), "msg": "Field required"}]
    error = validation.ValidationError(Payload, errors)
    assert error.model is Payload
    assert error.errors == errors
    assert "Validation failed for Payload" in str(error)
    assert "body.name: Field required" in str(error)

    single = validation.ValidationError(Payload, {"loc": ["name"], "msg": "bad"})
    assert "name: bad" in str(single)

    without_loc = validation.ValidationError(Payload, [{"msg": "oops"}])
    assert "oops" in str(without_loc)

    anonymous = validation.ValidationError(object(), [])
    assert "Validation failed for" in str(anonymous)


def test_is_pydantic_model_rejects_non_models(monkeypatch):
    class Payload(BaseModel):
        name: str

    assert validation.is_pydantic_model(Payload) is True
    assert validation.is_pydantic_model(None) is False
    assert validation.is_pydantic_model(type(None)) is False
    assert validation.is_pydantic_model(int) is False
    assert validation.is_pydantic_model(typing.Optional[Payload]) is False
    assert validation.is_pydantic_model(Payload(name="x")) is False

    monkeypatch.setattr(validation, "BaseModel", object())
    assert validation.is_pydantic_model(int) is False


def test_file_annotation_detection_variants(monkeypatch):
    class DerivedFile(File):
        pass

    assert validation._is_file_annotation(File) is True
    assert validation._is_file_annotation(DerivedFile) is True
    assert validation._is_file_annotation(List[File]) is False
    assert validation._is_file_annotation(typing.Optional[File]) is False
    assert validation._is_file_annotation("File") is False

    monkeypatch.setattr(validation, "_FILE_CLASS", object())
    assert validation._is_file_annotation(File) is False


def test_unwrap_optional_and_list_helpers():
    assert validation._unwrap_optional(None) == (None, False)
    assert validation._unwrap_optional(type(None)) == (type(None), False)
    assert validation._unwrap_optional(Optional[int]) == (int, True)
    assert validation._unwrap_optional(Union[int, str]) == (Union[int, str], False)
    assert validation._unwrap_optional(Union[int, None, str]) == (
        Union[int, None, str],
        False,
    )
    assert validation._unwrap_optional(int) == (int, False)

    assert validation._unwrap_list(List[int]) == (int, True)
    assert validation._unwrap_list(list[int]) == (int, True)
    assert validation._unwrap_list(typing.List) == (typing.List, False)
    assert validation._unwrap_list(int) == (int, False)
    assert validation._unwrap_list(Optional[List[int]]) == (Optional[List[int]], False)


def test_unwrap_optional_handles_pep604_unions(monkeypatch):
    import types

    monkeypatch.setattr(typing, "UnionType", types.UnionType, raising=False)
    assert validation._unwrap_optional(int | None) == (int, True)
    assert validation._unwrap_optional(int | str) == (int | str, False)


def test_expand_nested_keys_handles_bracket_notation():
    assert validation._expand_nested_keys({}) == {}
    assert validation._expand_nested_keys({"a": 1}) == {"a": 1}
    assert validation._expand_nested_keys({1: "one"}) == {1: "one"}
    assert validation._expand_nested_keys({"[odd]": 1}) == {"[odd]": 1}
    assert validation._expand_nested_keys({"a[b]": 2}) == {"a": {"b": 2}}
    assert validation._expand_nested_keys({"a[b][c]": 3}) == {"a": {"b": {"c": 3}}}
    assert validation._expand_nested_keys({"a[b]": 1, "a[c]": 2}) == {
        "a": {"b": 1, "c": 2}
    }
    assert validation._expand_nested_keys({"a[]": 1}) == {"a": [1]}
    assert validation._expand_nested_keys({"a[]": [1, 2]}) == {"a": [1, 2]}
    assert validation._expand_nested_keys({"a": [1], "a[]": 2}) == {"a": [1, 2]}
    assert validation._expand_nested_keys({"a": [1], "a[]": [2, 3]}) == {
        "a": [1, 2, 3]
    }
    assert validation._expand_nested_keys({"a[b][]": 1}) == {"a": {"b": {"": [1]}}}


def test_expand_nested_keys_appends_repeated_list_keys():
    duplicated = _DuplicateKeyMap([("a[]", 1), ("a[]", 2)])
    assert validation._expand_nested_keys(duplicated) == {"a": [1, 2]}

    duplicated_with_list = _DuplicateKeyMap([("a[]", 1), ("a[]", [2, 3])])
    assert validation._expand_nested_keys(duplicated_with_list) == {"a": [1, 2, 3]}

    duplicated_leaf = _DuplicateKeyMap([("a[b][]", 1), ("a[b][]", 2)])
    assert validation._expand_nested_keys(duplicated_leaf) == {
        "a": {"b": {"": [1, 2]}}
    }


def test_remap_case_insensitive_merges_and_keeps_unknown_keys():
    assert validation._remap_case_insensitive({}, {}) == {}
    assert validation._remap_case_insensitive({"NAME": "x"}, {"name": "name"}) == {
        "name": "x"
    }
    assert validation._remap_case_insensitive(
        {"name": "exact", "NAME": "other"}, {"name": "name"}
    ) == {"name": "exact"}
    assert validation._remap_case_insensitive(
        {"name": "x", "other": 1}, {"name": "name"}
    ) == {"name": "x", "other": 1}
    assert validation._remap_case_insensitive({1: "one"}, {"name": "name"}) == {1: "one"}
    assert validation._remap_case_insensitive(
        {"FOO": 1, "BAZ": 2}, {"foo": "bar", "baz": "bar"}
    ) == {"bar": 1}


def test_model_plan_classifies_file_and_nested_fields():
    class Profile(BaseModel):
        avatar: File
        nickname: str = "n"

    class Payload(BaseModel):
        document: File
        optional_document: Optional[File] = None
        documents: List[File] = []
        profile: Profile = None
        title: str = "t"

    plan = validation._get_plan(Payload)
    assert plan.fields == [
        "document",
        "optional_document",
        "documents",
        "profile",
        "title",
    ]
    assert plan.list_file_fields == ["documents"]
    assert plan.single_file_fields == ["document", "optional_document"]
    assert plan.nested == {"profile": Profile}
    assert plan.nested_file_names["profile"] == frozenset({"avatar"})
    assert plan.nested_file_names_exact["profile"] == frozenset({"avatar"})
    assert plan.any_file_field is True
    assert plan.lower_to_name["title"] == "title"
    assert plan.exact_names == frozenset(plan.fields)


def test_model_plan_survives_broken_model_metadata():
    class Broken(BaseModel):
        value: int

    Broken.model_fields = 42

    plan = validation._ModelPlan(Broken)
    assert plan.fields == []
    assert plan.any_file_field is False
    assert plan.lower_to_name == {}
    assert validation._nested_file_field_names(Broken) == frozenset()
    assert validation._collect_field_names(Broken) == []


def test_get_plan_caches_by_model_class():
    class Payload(BaseModel):
        name: str

    first = validation._get_plan(Payload)
    second = validation._get_plan(Payload)
    assert first is second
    assert first.fields == ["name"]


def test_get_plan_handles_unhashable_and_unstorable_models(monkeypatch):
    assert validation._get_plan([]).fields == []

    class Payload(BaseModel):
        name: str

    class _FailingCache:
        def get(self, key):
            return None

        def __setitem__(self, key, value):
            raise TypeError("not storable")

    monkeypatch.setattr(validation, "_PLAN_CACHE", _FailingCache())
    plan = validation._get_plan(Payload)
    assert plan.fields == ["name"]


def test_collect_field_names_returns_declared_fields():
    class Payload(BaseModel):
        first: str
        second: int = 2

    assert validation._collect_field_names(Payload) == ["first", "second"]
    assert validation._collect_field_names("not a model") == []
    assert validation._collect_field_names(object()) == []


def test_bind_files_into_data_ignores_non_models():
    data = {"a": 1}
    assert validation._bind_files_into_data(object, data, None) == (data, [])
    assert validation._bind_files_into_data(object, data, {"a": 2}) == (data, [])


def test_bind_files_into_data_without_file_fields_returns_data():
    class Payload(BaseModel):
        name: str

    assert validation._bind_files_into_data(Payload, {"name": "x"}, None) == (
        {"name": "x"},
        [],
    )
    assert validation._bind_files_into_data(Payload, {}, None) == ({}, [])
    assert validation._bind_files_into_data(Payload, None, None) == ({}, [])


def test_bind_files_into_data_handles_list_file_fields():
    class Payload(BaseModel):
        documents: List[File]

    class OptionalPayload(BaseModel):
        documents: Optional[List[File]] = None

    class DefaultedPayload(BaseModel):
        documents: List[File] = []

    document = File(file_content=b"a", filename="a.txt")

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"documents": document}
    )
    assert errors == []
    assert merged["documents"] == [document]

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"documents": [document]}
    )
    assert errors == []
    assert merged["documents"] == [document]

    merged, errors = validation._bind_files_into_data(Payload, {}, {"documents": []})
    assert errors == [
        {"type": "missing", "loc": ["documents"], "msg": "Field required"}
    ]
    assert merged == {}

    merged, errors = validation._bind_files_into_data(Payload, {}, {})
    assert errors == [
        {"type": "missing", "loc": ["documents"], "msg": "Field required"}
    ]

    merged, errors = validation._bind_files_into_data(
        OptionalPayload, {}, {"documents": []}
    )
    assert errors == []

    merged, errors = validation._bind_files_into_data(
        OptionalPayload, {}, {"documents": document}
    )
    assert errors == []
    assert merged["documents"] == [document]

    merged, errors = validation._bind_files_into_data(OptionalPayload, {}, {})
    assert errors == []

    merged, errors = validation._bind_files_into_data(DefaultedPayload, {}, {})
    assert errors == []

    merged, errors = validation._bind_files_into_data(
        DefaultedPayload, {}, {"documents": []}
    )
    assert errors == []

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"DOCUMENTS": document}, validate_case_sensitive=False
    )
    assert errors == []
    assert merged["documents"] == [document]


def test_bind_files_into_data_handles_single_file_fields():
    class Payload(BaseModel):
        document: File
        optional_document: Optional[File] = None

    document = File(file_content=b"a", filename="a.txt")

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"document": document}
    )
    assert errors == []
    assert merged["document"] is document
    assert "optional_document" not in merged

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"document": [document, document]}
    )
    assert errors == []
    assert merged["document"] is document

    merged, errors = validation._bind_files_into_data(Payload, {}, {"document": []})
    assert errors == []
    assert "document" not in merged

    merged, errors = validation._bind_files_into_data(
        Payload, {}, {"DOCUMENT": document}, validate_case_sensitive=False
    )
    assert errors == []
    assert merged["document"] is document


def test_bind_files_into_data_scopes_nested_files():
    class Profile(BaseModel):
        avatar: File
        nickname: str = "n"

    class Payload(BaseModel):
        profile: Profile

    avatar = File(file_content=b"a", filename="a.png")

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": {"nickname": "x"}}, {"profile[avatar]": avatar}
    )
    assert errors == []
    assert merged["profile"]["avatar"] is avatar

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": {}}, {"avatar": avatar}
    )
    assert errors == []
    assert merged["profile"]["avatar"] is avatar

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": {}}, {"profile": avatar}
    )
    assert errors == []
    assert merged["profile"] == {}

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": "not-a-dict"}, {"profile[avatar]": avatar}
    )
    assert errors == []
    assert merged["profile"] == "not-a-dict"

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": {}}, {1: avatar}
    )
    assert errors == []
    assert merged["profile"] == {}

    merged, errors = validation._bind_files_into_data(
        Payload, {"profile": {}}, {"unrelated": avatar}
    )
    assert errors == []
    assert merged["profile"] == {}


def test_bind_files_into_data_scopes_nested_files_case_insensitively():
    class Profile(BaseModel):
        avatar: File

    class Payload(BaseModel):
        profile: Profile

    avatar = File(file_content=b"a", filename="a.png")

    merged, errors = validation._bind_files_into_data(
        Payload,
        {"profile": {}},
        {"PROFILE[AVATAR]": avatar},
        validate_case_sensitive=False,
    )
    assert errors == []
    assert merged["profile"]["avatar"] is avatar

    merged, errors = validation._bind_files_into_data(
        Payload,
        {"profile": {}},
        {"AVATAR": avatar},
        validate_case_sensitive=False,
    )
    assert errors == []
    assert merged["profile"]["avatar"] is avatar

    merged, errors = validation._bind_files_into_data(
        Payload,
        {"profile": {}},
        {"PROFILE": avatar},
        validate_case_sensitive=False,
    )
    assert errors == []
    assert merged["profile"] == {}

    merged, errors = validation._bind_files_into_data(
        Payload,
        {"profile": {}},
        {"UNRELATED": avatar},
        validate_case_sensitive=False,
    )
    assert errors == []
    assert merged["profile"] == {}


def test_bind_files_into_data_relocates_nested_file_errors():
    class Profile(BaseModel):
        documents: List[File]

    class Payload(BaseModel):
        profile: Profile

    merged, errors = validation._bind_files_into_data(Payload, {"profile": {}}, {})
    assert errors == [
        {"type": "missing", "loc": ["profile", "documents"], "msg": "Field required"}
    ]
    assert merged == {"profile": {}}


def test_validate_model_handles_none_data():
    class Payload(BaseModel):
        name: str

    instance, errors = validation.validate_model(Payload, None)
    assert instance is None
    assert errors
    assert list(errors[0]["loc"]) == ["name"]


def test_validate_model_validates_non_dict_payloads():
    class Numbers(RootModel[List[int]]):
        pass

    instance, errors = validation.validate_model(Numbers, [1, 2])
    assert errors == []
    assert instance.root == [1, 2]

    instance, errors = validation.validate_model(Numbers, ["x"])
    assert instance is None
    assert errors


def test_validate_model_expands_bracket_keys():
    class Address(BaseModel):
        postal_code: int

    class Person(BaseModel):
        name: str
        address: Address

    instance, errors = validation.validate_model(
        Person, {"name": "Ada", "address[postal_code]": "100"}
    )
    assert errors == []
    assert instance.address.postal_code == 100


def test_validate_model_binds_files_and_reports_missing_uploads():
    class Payload(BaseModel):
        document: File

    class ListPayload(BaseModel):
        documents: List[File]

    document = File(file_content=b"data", filename="a.txt")

    instance, errors = validation.validate_model(Payload, {}, {"document": document})
    assert errors == []
    assert instance.document is document

    instance, errors = validation.validate_model(ListPayload, {}, {"documents": []})
    assert instance is None
    assert errors == [
        {"type": "missing", "loc": ["documents"], "msg": "Field required"}
    ]

    instance, errors = validation.validate_model(ListPayload, {}, {})
    assert instance is None
    assert errors
    assert list(errors[0]["loc"]) == ["documents"]


def test_validate_model_remaps_case_insensitive_keys():
    class Payload(BaseModel):
        name: str

    class Empty(BaseModel):
        pass

    instance, errors = validation.validate_model(
        Payload, {"NAME": "Ada"}, validate_case_sensitive=False
    )
    assert errors == []
    assert instance.name == "Ada"

    instance, errors = validation.validate_model(
        Payload, {"name": "Ada"}, validate_case_sensitive=False
    )
    assert errors == []
    assert instance.name == "Ada"

    instance, errors = validation.validate_model(
        Empty, {"anything": 1}, validate_case_sensitive=False
    )
    assert errors == []
    assert instance is not None


@pytest.mark.asyncio
async def test_asgi_json_body_validation(app_factory, asgi_client, monkeypatch):
    class Payload(BaseModel):
        name: str
        quantity: int

    app = app_factory()
    _disable_handler_meta_cache(monkeypatch, app)

    @app.post("/items")
    async def create_item(data: Payload, res):
        await res.json({"name": data.name, "quantity": data.quantity}, status=201)

    _LIVE_HANDLERS.append(create_item)

    valid = await asgi_client(
        app,
        "POST",
        "/items",
        headers={"Content-Type": "application/json"},
        body=b'{"name":"tea","quantity":3}',
    )
    assert valid["status"] == 201
    assert json.loads(valid["body"]) == {"name": "tea", "quantity": 3}

    invalid = await asgi_client(
        app,
        "POST",
        "/items",
        headers={"Content-Type": "application/json"},
        body=b'{"name":"tea","quantity":"many"}',
    )
    assert invalid["status"] == 422
    payload = json.loads(invalid["body"])
    assert payload["error"] == "ValidationError"
    assert payload["details"]

    empty = await asgi_client(
        app,
        "POST",
        "/items",
        headers={"Content-Type": "application/json"},
        body=b"",
    )
    assert empty["status"] == 422
    assert json.loads(empty["body"])["error"] == "ValidationError"


@pytest.mark.asyncio
async def test_asgi_case_insensitive_body_validation(app_factory, asgi_client, monkeypatch):
    class Payload(BaseModel):
        name: str

    app = app_factory(validate_case_sensitive=False)
    _disable_handler_meta_cache(monkeypatch, app)

    @app.post("/items")
    async def create_item(data: Payload, res):
        await res.json({"name": data.name})

    _LIVE_HANDLERS.append(create_item)

    response = await asgi_client(
        app,
        "POST",
        "/items",
        headers={"Content-Type": "application/json"},
        body=b'{"NAME":"tea"}',
    )
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"name": "tea"}


@pytest.mark.asyncio
async def test_asgi_multipart_upload_binds_file_field(
    app_factory, asgi_client, tmp_path, monkeypatch
):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)

    class Upload(BaseModel):
        title: str
        document: File

    app = app_factory()
    _disable_handler_meta_cache(monkeypatch, app)

    @app.post("/upload")
    async def upload(data: Upload, res):
        await res.json(
            {"title": data.title, "filename": data.document.filename}
        )

    _LIVE_HANDLERS.append(upload)

    boundary = "upload-boundary"
    body = (
        b"--upload-boundary\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"report\r\n--upload-boundary\r\n"
        b"Content-Disposition: form-data; name=\"document\"; filename=\"report.txt\"\r\n"
        b"Content-Type: text/plain\r\n\r\ncontents\r\n--upload-boundary--\r\n"
    )
    response = await asgi_client(
        app,
        "POST",
        "/upload",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    assert response["status"] == 200
    assert json.loads(response["body"]) == {"title": "report", "filename": "report.txt"}


@pytest.mark.asyncio
async def test_asgi_multipart_upload_reports_missing_file(
    app_factory, asgi_client, tmp_path, monkeypatch
):
    import flameo.parser as parser

    monkeypatch.setattr(parser, "TEMP_DIR", tmp_path)

    class Upload(BaseModel):
        document: File

    class ListUpload(BaseModel):
        documents: List[File]

    app = app_factory()
    _disable_handler_meta_cache(monkeypatch, app)

    @app.post("/upload")
    async def upload(data: Upload, res):
        await res.json({"filename": data.document.filename})

    @app.post("/uploads")
    async def uploads(data: ListUpload, res):
        await res.json({"count": len(data.documents)})

    _LIVE_HANDLERS.append(upload)
    _LIVE_HANDLERS.append(uploads)

    boundary = "upload-boundary"
    body = (
        b"--upload-boundary\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
        b"report\r\n--upload-boundary--\r\n"
    )
    response = await asgi_client(
        app,
        "POST",
        "/upload",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    assert response["status"] == 422
    payload = json.loads(response["body"])
    assert payload["error"] == "ValidationError"
    assert payload["details"][0]["loc"] == ["document"]

    response = await asgi_client(
        app,
        "POST",
        "/uploads",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body,
    )
    assert response["status"] == 422
    payload = json.loads(response["body"])
    assert payload["error"] == "ValidationError"
    assert payload["details"][0]["loc"] == ["documents"]
