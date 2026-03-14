import pytest
from src.utils.diff import DictDiff

def test_dict_diff_simple_change():
    old = {"a": 1, "b": 2}
    new = {"a": 1, "b": 3}
    expected = {"b": {"before": 2, "after": 3}}
    assert DictDiff.compute(old, new) == expected

def test_dict_diff_added_field():
    old = {"a": 1}
    new = {"a": 1, "b": 2}
    expected = {"b": {"before": None, "after": 2}}
    assert DictDiff.compute(old, new) == expected

def test_dict_diff_removed_field():
    old = {"a": 1, "b": 2}
    new = {"a": 1}
    expected = {"b": {"before": 2, "after": None}}
    assert DictDiff.compute(old, new) == expected

def test_dict_diff_exclude_fields():
    old = {"a": 1, "b": 2, "c": 3}
    new = {"a": 10, "b": 20, "c": 30}
    exclude = {"a", "c"}
    expected = {"b": {"before": 2, "after": 20}}
    assert DictDiff.compute(old, new, exclude=exclude) == expected

def test_dict_diff_no_changes():
    old = {"a": 1, "b": 2}
    new = {"a": 1, "b": 2}
    assert DictDiff.compute(old, new) == {}

def test_dict_diff_empty_dicts():
    assert DictDiff.compute({}, {}) == {}

def test_dict_diff_nested_values():
    # DictDiff.compute is currently shallow as per implementation
    old = {"a": {"x": 1}}
    new = {"a": {"x": 2}}
    expected = {"a": {"before": {"x": 1}, "after": {"x": 2}}}
    assert DictDiff.compute(old, new) == expected
