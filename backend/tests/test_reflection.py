from pathlib import Path

import pytest

from agentkit.reflection.resolvers import resolve_class, resolve_variable


def test_resolve_variable_returns_module_attribute():
    assert resolve_variable("math:pi") == pytest.approx(3.14159, rel=1e-4)


def test_resolve_variable_checks_expected_type():
    assert resolve_variable("math:pi", float) == pytest.approx(3.14159, rel=1e-4)
    assert resolve_variable("math:pi", (int, float)) == pytest.approx(3.14159, rel=1e-4)


def test_resolve_variable_rejects_wrong_type():
    with pytest.raises(ValueError, match="is not of type"):
        resolve_variable("math:pi", int)


def test_resolve_variable_rejects_invalid_path():
    with pytest.raises(ImportError, match="expected format: module_path:variable_name"):
        resolve_variable("math.pi")


def test_resolve_variable_missing_root_module():
    with pytest.raises(ImportError, match="Make sure the module is installed"):
        resolve_variable("definitely_not_installed_pkg_xyz:Foo")


def test_resolve_variable_failed_nested_import():
    with pytest.raises(ImportError, match="Failed to import module"):
        resolve_variable("json.this_submodule_does_not_exist:loads")


def test_resolve_variable_missing_attribute():
    with pytest.raises(AttributeError, match="has no attribute not_a_real_attr"):
        resolve_variable("math:not_a_real_attr")


def test_resolve_class_returns_class():
    assert resolve_class("pathlib:Path") is Path


def test_resolve_class_accepts_matching_base():
    assert resolve_class("pathlib:Path", Path) is Path


def test_resolve_class_rejects_unrelated_base():
    with pytest.raises(ValueError, match="is not a subclass"):
        resolve_class("pathlib:Path", str)


def test_resolve_class_rejects_non_class():
    with pytest.raises(ValueError, match="is not of type"):
        resolve_class("math:pi")
