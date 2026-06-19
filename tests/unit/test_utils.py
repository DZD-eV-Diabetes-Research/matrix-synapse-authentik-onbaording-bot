"""Unit tests for the nested-dict helpers."""

import pytest

from onbot.utils import dict_has_nested_attr, get_nested_dict_val_by_path


def test_get_nested_value() -> None:
    data = {"a": {"b": {"c": 42}}}
    assert get_nested_dict_val_by_path(data, ["a", "b", "c"]) == 42


def test_get_nested_missing_raises_without_fallback() -> None:
    with pytest.raises(KeyError):
        get_nested_dict_val_by_path({"a": 1}, ["a", "b"])


def test_get_nested_missing_returns_fallback_including_none() -> None:
    assert get_nested_dict_val_by_path({}, ["x"], fallback_val=None) is None
    assert get_nested_dict_val_by_path({}, ["x"], fallback_val="def") == "def"


def test_has_nested_attr() -> None:
    data = {"attributes": {"is_chatroom": True, "empty": ""}}
    assert dict_has_nested_attr(data, ["attributes", "is_chatroom"])
    assert not dict_has_nested_attr(data, ["attributes", "missing"])
    assert dict_has_nested_attr(data, ["attributes", "empty"])
    assert not dict_has_nested_attr(data, ["attributes", "empty"], must_have_val=True)
