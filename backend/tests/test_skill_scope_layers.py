"""M7 §2.4：两层开关（全局 + 个人）与各自的锁。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agentkit.config.extensions_config import extensions_config_write_lock
from agentkit.config.skills_prefs import (
    DEFAULT_USER_BUCKET,
    read_skills_prefs,
    user_bucket,
    write_user_skill_pref,
)
from agentkit.skills.prefs import effective_enabled, global_enabled_map
from agentkit.skills.prompt import render_skill_index
from agentkit.skills.storage import SkillRegistry, load_skills
from fakes.skill_tree import skills_config, write_skill


@pytest.fixture
def prefs_path(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "skills_prefs.json"
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(path))
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    return path


def _skill(tmp_path, *, enabled=True):
    write_skill(tmp_path, "public", "pdf-processing")
    return load_skills(skills_config(tmp_path), enabled_map={"pdf-processing": enabled})[0]


def test_effective_is_global_and_preference(tmp_path, prefs_path):
    skill = _skill(tmp_path, enabled=True)

    assert effective_enabled(skill) is True
    assert effective_enabled(skill, {}) is True
    assert effective_enabled(skill, {"pdf-processing": False}) is False

    globally_off = _skill(tmp_path, enabled=False)
    assert effective_enabled(globally_off, {}) is False
    assert effective_enabled(globally_off, {"pdf-processing": True}) is False


def test_a_globally_disabled_skill_stays_off_even_if_the_user_asks_for_it(
    tmp_path, prefs_path, monkeypatch
):
    """全局禁用往往意味着这个部署缺依赖或缺合规审批：个人偏好打开也无效。"""
    extensions = tmp_path / "extensions_config.json"
    extensions.write_text(
        json.dumps({"skills": {"pdf-processing": {"enabled": False}}}), encoding="utf-8"
    )
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(extensions))
    write_skill(tmp_path, "public", "pdf-processing")
    config = skills_config(tmp_path)

    write_user_skill_pref("u1", "pdf-processing", True)  # 写得进去，但不生效

    skills = load_skills(config, enabled_map=global_enabled_map())
    registry = SkillRegistry(config=config, user_id="u1")
    assert skills[0].enabled is False
    assert effective_enabled(skills[0], read_skills_prefs()["u1"]) is False
    assert registry.visible() == []
    assert registry.find("pdf-processing") is None


def test_a_personal_off_skill_leaves_the_index_and_slash(tmp_path, prefs_path):
    write_skill(tmp_path, "public", "pdf-processing")
    config = skills_config(tmp_path)
    registry = SkillRegistry(config=config, user_id="u1")
    assert registry.find("pdf-processing") is not None

    write_user_skill_pref("u1", "pdf-processing", False)
    off = SkillRegistry(config=config, user_id="u1")

    assert off.visible() == []
    assert off.find("pdf-processing") is None
    assert render_skill_index(off.visible(), max_description_chars=120) == ""
    # 另一个用户不受影响
    assert SkillRegistry(config=config, user_id="u2").find("pdf-processing") is not None


def test_two_users_keep_separate_preferences(tmp_path, prefs_path):
    write_skill(tmp_path, "public", "pdf-processing")

    write_user_skill_pref("u1", "pdf-processing", False)
    write_user_skill_pref("u2", "pdf-processing", True)

    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data == {"u1": {"pdf-processing": False}, "u2": {"pdf-processing": True}}


def test_no_user_id_falls_into_the_default_bucket(tmp_path, prefs_path):
    write_skill(tmp_path, "public", "pdf-processing")

    write_user_skill_pref(None, "pdf-processing", False)

    assert user_bucket(None) == DEFAULT_USER_BUCKET
    assert read_skills_prefs() == {DEFAULT_USER_BUCKET: {"pdf-processing": False}}
    assert SkillRegistry(config=skills_config(tmp_path), user_id=None).visible() == []


def test_the_two_layers_use_different_locks(tmp_path, prefs_path):
    """个人开关不碰 extensions_config.json，也不该被那把锁挡住。"""
    write_skill(tmp_path, "public", "pdf-processing")
    done = threading.Event()

    def write_pref() -> None:
        write_user_skill_pref("u1", "pdf-processing", False)
        done.set()

    with extensions_config_write_lock:
        worker = threading.Thread(target=write_pref)
        worker.start()
        assert done.wait(timeout=10), "preference write blocked on the extensions lock"
        worker.join(timeout=10)


def test_concurrent_preference_writes_do_not_lose_updates(tmp_path, prefs_path):
    write_skill(tmp_path, "public", "pdf-processing")
    rounds = 5

    def churn(user: str) -> None:
        for index in range(rounds):
            write_user_skill_pref(user, f"skill-{index}", index % 2 == 0)

    threads = [threading.Thread(target=churn, args=(f"u{n}",)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    data = read_skills_prefs()
    assert set(data) == {"u0", "u1", "u2"}
    for bucket in data.values():
        assert len(bucket) == rounds


def test_global_enabled_map_reads_the_extensions_file(tmp_path, monkeypatch):
    config_path = tmp_path / "extensions_config.json"
    config_path.write_text(
        json.dumps({"skills": {"pdf-processing": {"enabled": False}}}), encoding="utf-8"
    )
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(config_path))

    assert global_enabled_map() == {"pdf-processing": False}
