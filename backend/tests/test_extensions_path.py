"""M7 §2.1：extensions_config.json 的路径解析和两种缺失语义。"""

from __future__ import annotations

import json

import pytest

from agentkit.config.extensions_config import (
    EXTENSIONS_CONFIG_ENV_NAMES,
    ExtensionsConfig,
    read_extensions_config_file,
    resolve_extensions_config_path,
    update_extensions_config,
    writable_extensions_config_path,
)


@pytest.fixture(autouse=True)
def _clear_path_env(monkeypatch):
    for name in EXTENSIONS_CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_explicit_path_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_extensions_config_path(tmp_path / "nope.json")


def test_env_path_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(tmp_path / "nope.json"))

    with pytest.raises(FileNotFoundError):
        resolve_extensions_config_path()


def test_env_path_is_used_when_it_exists(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))

    assert resolve_extensions_config_path() == path


def test_search_mode_without_a_file_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))

    assert resolve_extensions_config_path() is None
    assert ExtensionsConfig.from_file().mcp_servers == {}
    assert writable_extensions_config_path() == tmp_path / "extensions_config.json"


def test_search_mode_finds_the_project_root_file(tmp_path, monkeypatch):
    path = tmp_path / "extensions_config.json"
    path.write_text(json.dumps({"skills": {"pdf-processing": {"enabled": False}}}), encoding="utf-8")
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))

    assert resolve_extensions_config_path() == path
    config = ExtensionsConfig.from_file()
    assert config.skills["pdf-processing"].enabled is False


def test_from_file_keeps_chinese_and_ops_keys(tmp_path):
    path = tmp_path / "extensions_config.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {"github": {"type": "stdio", "command": "npx", "args": ["-y", "x"]}},
                "skills": {"中文技能": {"enabled": True}},
                "mcpInterceptors": ["ops.Interceptor"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config = ExtensionsConfig.from_file(path)

    assert config.skills["中文技能"].enabled is True
    assert config.mcp_servers["github"].command == "npx"
    # mcpInterceptors 是运维文件专属：读得进、原样写回，但 API 不提供写它的接口。
    assert config.to_file_dict()["mcpInterceptors"] == ["ops.Interceptor"]

def test_an_empty_file_is_unconfigured(tmp_path, monkeypatch):
    """空文件和只有空白的文件当"没配"，不让刚 cp 完模板的人撞 500。"""
    path = tmp_path / "extensions_config.json"
    path.write_text("", encoding="utf-8")
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(path))

    assert read_extensions_config_file(path) == {}
    assert ExtensionsConfig.from_file(path).mcp_servers == {}


def test_a_write_never_recreates_a_vanished_explicit_path(tmp_path, monkeypatch):
    """显式断言的路径中途消失要大声失败，不能悄悄写出一份新的空配置。"""
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(tmp_path / "gone.json"))

    with pytest.raises(FileNotFoundError):
        update_extensions_config(lambda data: data.update({"skills": {}}))

    assert not (tmp_path / "gone.json").exists()
