from __future__ import annotations

import re
import types
import typing
import weakref
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Type, get_args, get_origin

try:
    from pydantic import BaseModel, ValidationError as _PydanticValidationError
    _PYDANTIC_AVAILABLE = True
except Exception:
    BaseModel = None  # type: ignore[assignment]
    _PydanticValidationError = None  # type: ignore[assignment]
    _PYDANTIC_AVAILABLE = False

_FILE_CLASS: Optional[type] = None


def _get_file_class() -> Optional[type]:
    """Return the ``flameo.flameo.File`` class, or ``None`` if it's
    not importable for any reason.

    The class itself is the marker we use to detect ``File``-typed
    fields in a pydantic model. Keeping the import lazy means a project
    that never uses uploads never pays the module-load cost of
    ``utils.py``.
    """
    global _FILE_CLASS
    if _FILE_CLASS is not None:
        return _FILE_CLASS
    try:
        from .flameo import File
        _FILE_CLASS = File
    except Exception:
        _FILE_CLASS = None
    return _FILE_CLASS


class ValidationError(Exception):

    def __init__(self, model: Type[Any], errors: Any):
        self.model = model
        self.errors = errors
        summary = ", ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in (errors if isinstance(errors, list) else [errors])
        )
        super().__init__(f"Validation failed for {getattr(model, '__name__', model)}: {summary}")


def is_pydantic_model(annotation: Any) -> bool:
    if not _PYDANTIC_AVAILABLE:
        return False
    if annotation is None or annotation is type(None):
        return False
    try:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)
    except TypeError:
        return False


def _is_file_annotation(annotation: Any) -> bool:
    """Return ``True`` iff ``annotation`` is (or wraps) the
    ``flameo.flameo.File`` class.

    Recognises bare ``File`` and the common union/optional wrappers
    (``Optional[File]`` / ``File | None``). The ``List[File]`` shape
    is handled by the recursive walker -- this helper returns
    ``False`` for the outer ``List`` because the inner-element type
    is what matters for the leaf.

    Anything more exotic (e.g. ``Annotated[File, ...]``) falls through
    to the standard pydantic validator. We only short-circuit on the
    common shapes to keep the detection cheap.
    """
    file_cls = _get_file_class()
    if file_cls is None:
        return False
    if annotation is file_cls:
        return True
    if isinstance(annotation, type):
        try:
            return issubclass(annotation, file_cls)
        except TypeError:
            return False
    return False


def _unwrap_optional(annotation: Any) -> Tuple[Any, bool]:
    """Peel ``Optional[X]`` / ``X | None`` / ``Union[X, None]`` off an
    annotation, returning ``(inner, True)`` if it was optional and
    ``(annotation, False)`` otherwise.

    Only ``None`` is treated as the optional marker. A ``Union[File,
    str]`` (rare but legal) is left untouched so pydantic handles it
    normally.
    """
    if annotation is None or annotation is type(None):
        return annotation, False
    origin = get_origin(annotation)
    # ``typing.Optional[X]`` and ``typing.Union[X, None]`` both come
    # through ``get_origin`` as ``typing.Union`` (PEP 604 ``X | None``
    # uses ``types.UnionType`` on 3.10+).
    if origin is typing.Union or origin is types.UnionType:
        args = tuple(a for a in get_args(annotation) if a is not type(None))
        if len(args) == 1 and type(None) in get_args(annotation):
            return args[0], True
    return annotation, False


def _unwrap_list(annotation: Any) -> Tuple[Any, bool]:
    """If ``annotation`` is ``List[X]`` (in any of its common spellings),
    return ``(X, True)``. Otherwise return ``(annotation, False)``.
    """
    origin = get_origin(annotation)
    if origin in (list, List):
        args = get_args(annotation)
        if len(args) == 1:
            return args[0], True
    return annotation, False


# Match the head of a bracket-notation form key so we can split it
# into segments. We accept any non-bracket characters in the head;
# everything after is a sequence of ``[seg]`` (or ``[]``) suffixes.
_NESTED_KEY_RE = re.compile(r"^([^\[\]]+)(.*)$")
# Walk the bracket *suffix* of a key in a single pass. The previous
# implementation re-sliced the tail for every level (``cursor =
# cursor[close + 1:]``), which is O(n^2) over the key length for
# keys with many segments. ``finditer`` walks once.
_NESTED_SUFFIX_RE = re.compile(r"\[([^\[\]]*)\]")


def _expand_nested_keys(flat: Dict[str, Any]) -> Dict[str, Any]:
    """Expand flat form keys with bracket notation into a nested dict.

    Examples::

        {"a": 1}                            -> {"a": 1}
        {"a[b]": 2}                         -> {"a": {"b": 2}}
        {"a[b][c]": 3}                      -> {"a": {"b": {"c": 3}}}
        {"a[]": 1, "a[]": 2}                -> {"a": [1, 2]}
        {"profile[name]": "x",              {"profile": {"name": "x",
         "profile[avatar]": <File>}          "avatar": <File>}}

    Later occurrences of the same key win for the same path; this
    matches the standard "last value wins" semantics most frameworks
    use. Lists (``a[]``) are built by repeated insertion; if the same
    ``a[]`` appears once with a list value, that list is taken
    verbatim. We do NOT descend into already-structured values
    (e.g. dicts) -- the caller is expected to hand us a flat mapping
    for the most predictable behaviour.
    """
    if not flat:
        return flat
    out: Dict[str, Any] = {}
    for raw_key, value in flat.items():
        if not isinstance(raw_key, str) or "[" not in raw_key:
            out[raw_key] = value
            continue
        m = _NESTED_KEY_RE.match(raw_key)
        if not m:
            out[raw_key] = value
            continue
        head = m.group(1)
        rest = m.group(2)
        
        levels: List[str] = []  # segment names, "" denotes ``[]``
        for m2 in _NESTED_SUFFIX_RE.finditer(rest):
            levels.append(m2.group(1))
        
        if len(levels) == 1 and levels[0] == "":
            existing = out.get(head)
            if isinstance(existing, list):
                if isinstance(value, list):
                    existing.extend(value)
                else:
                    existing.append(value)
            else:
                if isinstance(value, list):
                    out[head] = list(value)
                else:
                    out[head] = [value]
            continue
        
        chain: List[str] = [head] + list(levels)
        
        parent: Any = out
        for key in chain[:-1]:
            existing = parent.get(key)
            if not isinstance(existing, dict):
                existing = {}
                parent[key] = existing
            parent = existing
        leaf_key = chain[-1]
        
        leaf_is_list = levels[-1] == ""
        if leaf_is_list:
            existing = parent.get(leaf_key)
            if isinstance(existing, list):
                existing.append(value)
            else:
                parent[leaf_key] = [value]
        else:
            parent[leaf_key] = value
    return out


def _remap_case_insensitive(
    data: Dict[str, Any],
    lower_to_name: Dict[str, str],
) -> Dict[str, Any]:
    if not data:
        return {}
    # Cache the canonical field names as a set the first time we
    # call this per plan; the caller reuses one plan for every
    # request against the same model.
    canonical_set = set(lower_to_name.values())
    out: Dict[str, Any] = {}
    used: set = set()
    # First pass: exact-case matches. These always win over case-only
    # matches so a user who happened to spell the field correctly
    # gets the canonical form.
    for k, v in data.items():
        if isinstance(k, str) and k in canonical_set:
            out[k] = v
            used.add(k.lower())
    # Second pass: case-only matches, mapped onto the canonical
    # spelling. Skip keys we already used in pass 1.
    for k, v in data.items():
        if not isinstance(k, str):
            out[k] = v
            continue
        lk = k.lower()
        if lk in used:
            continue
        canonical = lower_to_name.get(lk)
        if canonical is None:
            out[k] = v
            continue
        if canonical not in out:
            out[canonical] = v
        used.add(lk)
    return out



_PLAN_CACHE: "weakref.WeakKeyDictionary[Any, _ModelPlan]" = (
    weakref.WeakKeyDictionary()
)


class _FieldPlan:
    """How one model field binds, computed once per model class."""

    __slots__ = ("name", "lower", "is_file", "is_file_list",
                 "is_optional_file", "nested_cls")

    def __init__(self, name: str, annotation: Any):
        self.name = name
        self.lower = name.lower()
        tmp, opt1 = _unwrap_optional(annotation)
        tmp, is_list = _unwrap_list(tmp)
        unwrapped, opt2 = _unwrap_optional(tmp)
        self.is_file = _is_file_annotation(unwrapped)
        self.is_file_list = self.is_file and is_list
        self.is_optional_file = self.is_file and (opt1 or opt2)
        self.nested_cls = annotation if is_pydantic_model(annotation) else None


class _ModelPlan:
    """Request-independent metadata derived once per model class.
    """

    __slots__ = ("fields", "lower_to_name", "exact_names", "plans",
                 "list_file_fields", "single_file_fields", "nested",
                 "nested_file_names", "nested_file_names_exact",
                 "any_file_field")

    def __init__(self, model_cls: Type[Any]):
        self.fields: List[str] = []
        self.plans: Dict[str, _FieldPlan] = {}
        self.list_file_fields: List[str] = []
        self.single_file_fields: List[str] = []
        self.nested: Dict[str, Any] = {}
        self.nested_file_names: Dict[str, FrozenSet[str]] = {}
        self.nested_file_names_exact: Dict[str, FrozenSet[str]] = {}
        try:
            for name, field in model_cls.model_fields.items():
                fp = _FieldPlan(name, field.annotation)
                self.fields.append(name)
                self.plans[name] = fp
                if fp.is_file_list:
                    self.list_file_fields.append(name)
                elif fp.is_file:
                    self.single_file_fields.append(name)
                if fp.nested_cls is not None:
                    self.nested[name] = fp.nested_cls
                    self.nested_file_names[name] = _nested_file_field_names(
                        fp.nested_cls
                    )
                    self.nested_file_names_exact[name] = _nested_file_field_names(
                        fp.nested_cls, exact=True
                    )
        except Exception:
            # An exotic model shouldn't take the request path down;
            # the subsequent ``model_validate`` produces the real
            # error message.
            pass
        self.lower_to_name = {n.lower(): n for n in self.fields}
        self.exact_names = frozenset(self.fields)
        self.any_file_field = bool(
            self.list_file_fields or self.single_file_fields
        )


def _nested_file_field_names(model_cls: Any, exact: bool = False) -> FrozenSet[str]:
    """Lower-cased ``File``-typed field names declared on a sub-model."""
    names: set = set()
    try:
        for sub_name, sub_field in model_cls.model_fields.items():
            inner, _ = _unwrap_list(sub_field.annotation)
            inner, _ = _unwrap_optional(inner)
            if _is_file_annotation(inner):
                names.add(sub_name if exact else sub_name.lower())
    except Exception:
        pass
    return frozenset(names)


def _get_plan(model_cls: Type[Any]) -> _ModelPlan:
    """Return the cached :class:`_ModelPlan` for ``model_cls``.
    """
    try:
        plan = _PLAN_CACHE.get(model_cls)
    except TypeError:
        return _ModelPlan(model_cls)
    if plan is None:
        plan = _ModelPlan(model_cls)
        try:
            _PLAN_CACHE[model_cls] = plan
        except TypeError:
            pass
    return plan


def _collect_field_names(model_cls: Type[Any]) -> List[str]:
    """Enumerate the field names declared on a pydantic ``BaseModel``.
    """
    if not is_pydantic_model(model_cls):
        return []
    try:
        return list(model_cls.model_fields.keys())
    except Exception:
        return []


def _bind_files_into_data(
    model_cls: Type[Any],
    data: Dict[str, Any],
    files: Optional[Dict[str, Any]],
    validate_case_sensitive: bool = True,
) -> Tuple[Dict[str, Any], List[dict]]:
    """Walk ``model_cls`` and inject ``File``-typed fields from
    ``files`` into ``data`` so pydantic can validate the whole model
    in one pass.

    Returns ``(merged_data, errors)``. ``errors`` is non-empty when a
    required ``File`` field has no upload, or when a non-optional
    ``File`` field received ``None``. We surface those as pydantic-
    style error dicts so the existing 422 envelope still applies.

    All shape-derived information (which fields are ``File``,
    ``List[File]`` or nested models, the per-nested File-typed
    lower-cased names, etc.) is read from the per-class
    :class:`_ModelPlan`. We never re-derive it inside this function.

    Binding rules:

    * Bare ``File`` -- pull ``files[canonical]``. Missing -> error.
    * ``Optional[File]`` -- pull if present, else leave for pydantic
      to validate as ``None``.
    * ``List[File]`` -- accept either a single ``File`` (wrap in list)
      or a list of ``File`` (use as-is). Missing -> error.
    * Nested ``BaseModel`` -- recurse with the sub-data taken from
      ``data[canonical]``; if absent, leave to pydantic to fail. We
      do NOT recurse into ``List[BaseModel]`` because multipart
      bodies can't really express it and the existing form parser
      flattens lists.
    * Case handling: when ``validate_case_sensitive`` is ``False``,
      matches the input ``files`` keys against the model field names
      case-insensitively; when ``True``, only exact-case matches bind.
    """
    if not is_pydantic_model(model_cls):
        return data, []
    plan = _get_plan(model_cls)

    if not files:
        if not plan.any_file_field and not plan.nested:
            return data if data else {}, []

    if not data:
        data = {}

    _ = _get_file_class()

    files_lookup: Dict[str, Any] = {}
    if files:
        for k, v in files.items():
            if not isinstance(k, str):
                continue
            files_lookup[k if validate_case_sensitive else k.lower()] = v

    errors: List[dict] = []
    merged: Dict[str, Any] = dict(data)

    for name in plan.list_file_fields:
        fp = plan.plans[name]
        file_value = files_lookup.get(fp.name if validate_case_sensitive else fp.lower)
        if file_value is None:
            if model_cls.model_fields[name].is_required():
                errors.append({
                    "type": "missing",
                    "loc": [name],
                    "msg": "Field required",
                })
            continue
        if isinstance(file_value, list):
            if not file_value and model_cls.model_fields[name].is_required():
                errors.append({
                    "type": "missing",
                    "loc": [name],
                    "msg": "Field required",
                })
                continue
            merged[name] = file_value
        else:
            # Single ``File`` -- wrap in a one-element list.
            merged[name] = [file_value]

    # Bare / ``Optional[File]`` fields -> single instance.
    for name in plan.single_file_fields:
        fp = plan.plans[name]
        file_value = files_lookup.get(fp.name if validate_case_sensitive else fp.lower)
        if file_value is None:
            continue
        if isinstance(file_value, list):
            if file_value:
                merged[name] = file_value[0]
        else:
            merged[name] = file_value

    for name, nested_cls in plan.nested.items():
        if not isinstance(merged.get(name), dict):
            continue
        scoped_files: Dict[str, Any] = {}
        nested_file_field_names = plan.nested_file_names.get(name) or frozenset()
        nested_file_names_exact = plan.nested_file_names_exact.get(name) or frozenset()
        name_lower = name.lower()
        prefix = name_lower + "["
        prefix_exact = name + "["
        if files:
            for k, v in files.items():
                if not isinstance(k, str):
                    continue
                if validate_case_sensitive:
                    if k.startswith(prefix_exact) and k.endswith("]"):
                        leaf = k[len(prefix_exact):-1]
                        scoped_files[leaf] = v
                    elif k == name:
                        scoped_files[k] = v
                    elif k in nested_file_names_exact:
                        scoped_files[k] = v
                    continue
                lk = k.lower()
                if lk.startswith(prefix) and lk.endswith("]"):
                    # ``profile[avatar]`` -> ``avatar``
                    leaf = k[len(prefix):-1]
                    scoped_files[leaf] = v
                elif lk == name_lower:
                    scoped_files[k] = v
                elif lk in nested_file_field_names:
                    # Flat ``avatar`` matches a File field
                    # declared on the nested model -- take it.
                    scoped_files[k] = v
        sub_merged, sub_errors = _bind_files_into_data(
            nested_cls,
            merged[name],
            scoped_files or None,
            validate_case_sensitive=validate_case_sensitive,
        )
        merged[name] = sub_merged
        for err in sub_errors:
            # Re-locate the error under the parent field so the
            # ``loc`` chain in the 422 envelope points at the
            # right field path.
            err_loc = list(err.get("loc", []))
            err["loc"] = [name] + err_loc
        errors.extend(sub_errors)
    return merged, errors


def validate_model(
    model_cls: Type[Any],
    data: Any,
    files: Optional[Dict[str, Any]] = None,
    validate_case_sensitive: bool = True,
) -> Tuple[Any, list]:
    if not _PYDANTIC_AVAILABLE:
        raise RuntimeError(
            "Pydantic validation requested but 'pydantic' is not installed.\n"
            "Install it with: pip install pydantic"
        )

    if data is None:
        data = {}

    if isinstance(data, dict):
        has_brackets = False
        for k in data:
            if isinstance(k, str) and ("[" in k or "]" in k):
                has_brackets = True
                break
        if has_brackets:
            data = _expand_nested_keys(data)
    else:
        try:
            instance = model_cls.model_validate(data)
            return instance, []
        except _PydanticValidationError as exc:
            return None, exc.errors()

    if files:
        data, file_errors = _bind_files_into_data(
            model_cls, data, files,
            validate_case_sensitive=validate_case_sensitive,
        )
        if file_errors:
            return None, file_errors

    if not validate_case_sensitive and isinstance(data, dict):
        plan = _get_plan(model_cls)
        if plan.fields:
            needs_remap = False
            for k in data:
                if isinstance(k, str) and k not in plan.exact_names:
                    needs_remap = True
                    break
            if needs_remap:
                data = _remap_case_insensitive(data, plan.lower_to_name)

    try:
        instance = model_cls.model_validate(data)
    except _PydanticValidationError as exc:
        return None, exc.errors()
    return instance, []


def validation_error_payload(errors: Any) -> dict:
    """Format pydantic errors into a JSON-serializable response body."""
    return {
        "error": "ValidationError",
        "details": errors,
    }
