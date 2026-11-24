import re
from functools import lru_cache
from typing import Type, TypeVar, Dict, Any, List, get_type_hints
from pydantic import create_model

T = TypeVar("T")

class DotDict(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


def _normalize(name: str) -> str:
    """Strip underscores/spaces and lowercase, so both sides compare fairly:
    'TRANSACTION_REFERENCE' -> 'transactionreference'
    'TransactionReference'  -> 'transactionreference'
    """
    return re.sub(r"[_\s]", "", name).lower()

@lru_cache(maxsize=None)
def _build_binder(target_cls: Type[T]):
    annotations = get_type_hints(target_cls)
    field_definitions = {
        name: (typ, getattr(target_cls, name, None))
        for name, typ in annotations.items()
    }
    ValidationModel = create_model(f"{target_cls.__name__}Validator", **field_definitions)
    field_lookup = {_normalize(name): name for name in annotations}
    return annotations, ValidationModel, field_lookup


def bind_to_instance(data: Dict[str, Any], target_cls: Type[T]) -> T:
    annotations, ValidationModel, field_lookup = _build_binder(target_cls)

    mapped_data = {
        matched_field: value
        for key, value in data.items()
        if (matched_field := field_lookup.get(_normalize(key))) is not None
    }

    validated = ValidationModel(**mapped_data)

    instance = object.__new__(target_cls)
    for name in annotations:
        setattr(instance, name, getattr(validated, name))

    return instance


def bind_to_list(data_list: List[Dict[str, Any]], target_cls: Type[T]) -> List[T]:
    _build_binder(target_cls)
    return [bind_to_instance(item, target_cls) for item in data_list]

