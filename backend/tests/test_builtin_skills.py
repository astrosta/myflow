import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentkit.config.reload_boundary import STARTUP_ONLY_FIELDS, STARTUP_ONLY_PREFIX
from agentkit.config.runtime_paths import resolve_path
from agentkit.config.skills_config import BuiltinSkillsConfig, SkillsConfig
from agentkit.skills import builtin as builtin_loader
from agentkit.skills.builtin import (
    builtin_names,
    clear_builtin_cache,
    load_builtin_skills,
    resolve_builtin_root,
)
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import SkillCategory
from fakes.skill_tree import skills_config, symlinks_available, write_skill

BUILTIN_WARNING_PREFIX = "built-in skills directory"


@pytest.fixture(autouse=True)
def _clear_builtin_cache():
    clear_builtin_cache()
    yield
    clear_builtin_cache()


def builtin_warnings(caplog) -> list[str]:
    """本模块发出的根目录告警（缺失或读不了），按发生顺序。"""
    return [record.message for record in caplog.records if BUILTIN_WARNING_PREFIX in record.message]


def _create_directory_link(link: Path, target: Path, kind: str) -> None:
    """建 symlink 或 Windows junction；宿主不支持时跳过，不把环境差异当失败。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        if not symlinks_available(link.parent):
            pytest.skip("symlinks unavailable")
        os.symlink(target, link, target_is_directory=True)
        return
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"cannot create a junction: {completed.stderr or completed.stdout}")


@pytest.fixture(params=["symlink", "junction"])
def linked_builtin_root(request, tmp_path: Path):
    target_parent = tmp_path / "outside"
    write_skill(target_parent, "builtin", "demo")
    target = target_parent / "skills" / "builtin"
    link = tmp_path / "skills" / "builtin"
    _create_directory_link(link, target, request.param)
    try:
        yield link
    finally:
        try:
            link.unlink()
        except OSError:
            try:
                link.rmdir()
            except OSError:
                pass


def test_builtin_config_defaults_and_enum_values():
    config = SkillsConfig()
    assert config.builtin.enabled is True
    assert config.builtin.path == "skills/builtin"
    assert config.builtin.root_dir() == resolve_path("skills/builtin")
    assert SkillScope.BUILTIN == "builtin"
    assert SkillCategory.BUILTIN == "builtin"


@pytest.mark.parametrize("path", ["", "   ", ".", "..", "skills/../builtin"])
def test_builtin_config_rejects_blank_or_escaping_paths(path: str):
    with pytest.raises(ValidationError):
        BuiltinSkillsConfig(path=path)


def test_builtin_config_rejects_a_filesystem_root():
    roots = ["/"]
    anchor = Path.cwd().anchor
    if anchor:
        roots.append(anchor)
    for root in roots:
        with pytest.raises(ValidationError):
            BuiltinSkillsConfig(path=root)


def test_builtin_config_accepts_normal_relative_and_absolute_paths(tmp_path: Path):
    assert BuiltinSkillsConfig(path="skills/builtin").path == "skills/builtin"
    absolute = str(tmp_path / "skills" / "builtin")
    assert BuiltinSkillsConfig(path=absolute).path == absolute


def test_resolve_builtin_root_resolves_relative_to_project_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    root = tmp_path / "skills" / "builtin"
    root.mkdir(parents=True)

    assert resolve_builtin_root(SkillsConfig()) == root.resolve()


def test_builtin_fields_are_startup_only():
    paths = ("skills.builtin.enabled", "skills.builtin.path")
    assert set(paths) <= STARTUP_ONLY_FIELDS.keys()

    for path in paths:
        description = BuiltinSkillsConfig.model_fields[path.rsplit(".", 1)[-1]].description or ""
        assert description.startswith(f"{STARTUP_ONLY_PREFIX} {STARTUP_ONLY_FIELDS[path]}")


def test_builtin_loader_sets_scope_and_container_path(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo", description="Demo skill")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

    skills = load_builtin_skills(config)

    assert [skill.name for skill in skills] == ["demo"]
    assert skills[0].scope is SkillScope.BUILTIN
    assert skills[0].category is SkillCategory.BUILTIN
    assert skills[0].enabled is True
    assert skills[0].container_file == "/mnt/skills/builtin/demo/SKILL.md"
    names = builtin_names(config)
    assert isinstance(names, frozenset), "builtin_names 必须是 frozenset，保留名检查靠它做集合运算"
    assert names == frozenset({"demo"})


def test_builtin_loader_is_disabled_by_config(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"enabled": False, "path": str(tmp_path / "skills" / "builtin")})
    assert load_builtin_skills(config) == ()


def test_resolve_builtin_root_rejects_a_linked_root(linked_builtin_root: Path):
    config = skills_config(
        linked_builtin_root.parents[1],
        builtin={"path": str(linked_builtin_root)},
    )

    with pytest.raises(ValueError, match="symlink or junction"):
        resolve_builtin_root(config)


def test_builtin_loader_rejects_a_linked_root(linked_builtin_root: Path, caplog):
    config = skills_config(
        linked_builtin_root.parents[1],
        builtin={"path": str(linked_builtin_root)},
    )

    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()

    assert len(builtin_warnings(caplog)) == 1
    assert not builtin_loader._cache, "不合法根不能留下缓存"


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, ValueError])
def test_builtin_loader_degrades_when_root_resolution_fails(
    tmp_path: Path, caplog, monkeypatch, error_type: type[Exception]
):
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

    def boom(_config: SkillsConfig):
        raise error_type("unsafe root")

    monkeypatch.setattr(builtin_loader, "resolve_builtin_root", boom)
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()

    assert len(builtin_warnings(caplog)) == 1


def test_a_resolution_failure_drops_a_cached_result(tmp_path: Path, monkeypatch):
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
    assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]
    assert builtin_loader._cache

    def boom(_config: SkillsConfig):
        raise ValueError("unsafe root")

    monkeypatch.setattr(builtin_loader, "resolve_builtin_root", boom)

    assert load_builtin_skills(config) == ()
    assert builtin_loader._cache == {}


def test_builtin_loader_warns_once_for_a_missing_root(tmp_path: Path, caplog):
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "missing")})
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()
    assert len(builtin_warnings(caplog)) == 1


def test_clear_builtin_cache_resets_the_warned_missing_root(tmp_path: Path, caplog):
    """告警去重靠 warned-roots 集合；clear_builtin_cache() 必须把它一起清掉，否则重载后永远静默。"""
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "missing")})
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()
        assert len(builtin_warnings(caplog)) == 1

        clear_builtin_cache()

        assert load_builtin_skills(config) == ()
        assert len(builtin_warnings(caplog)) == 2


def test_missing_root_recovers_after_the_directory_appears(tmp_path: Path):
    config = skills_config(
        tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")}
    )
    assert load_builtin_skills(config) == ()
    write_skill(tmp_path, "builtin", "demo")
    assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]


def test_builtin_cache_reuses_the_scan_until_it_is_cleared(tmp_path: Path, monkeypatch):
    """缓存命中必须真的跳过扫描：只比较结果的话，每次重扫也能"通过"。"""
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
    scans: list[Path] = []
    real_scan = builtin_loader.scan_skill_category

    def counting_scan(category_root, **kwargs):
        scans.append(category_root)
        return real_scan(category_root, **kwargs)

    monkeypatch.setattr(builtin_loader, "scan_skill_category", counting_scan)

    first = load_builtin_skills(config)
    second = load_builtin_skills(config)

    assert [skill.name for skill in first] == ["demo"]
    assert second is first, "指纹没变时必须返回缓存里的同一个 tuple，而不是重扫的结果"
    assert len(scans) == 1

    clear_builtin_cache()

    assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]
    assert len(scans) == 2


def test_builtin_cache_invalidates_when_skill_md_changes(tmp_path: Path):
    write_skill(tmp_path, "builtin", "demo", description="first")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
    first = load_builtin_skills(config)
    skill_file = tmp_path / "skills" / "builtin" / "demo" / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo\ndescription: second\n---\n# second\n", encoding="utf-8"
    )
    future = 2_000_000_000_000_000_000
    os.utime(skill_file, ns=(future, future))

    second = load_builtin_skills(config)

    assert first[0].description == "first"
    assert second[0].description == "second"


def test_builtin_loader_degrades_when_the_root_cannot_be_read(
    tmp_path: Path, caplog, monkeypatch
):
    """is_dir() 过了不等于读得到：根目录 iterdir 抛 PermissionError 时不能把异常抛给启动/列表。"""
    write_skill(tmp_path, "builtin", "demo")
    root = tmp_path / "skills" / "builtin"
    config = skills_config(tmp_path, builtin={"path": str(root)})
    denied = root.resolve()
    real_iterdir = Path.iterdir

    def permission_denied(self: Path):
        if self == denied:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", permission_denied)
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()
        monkeypatch.undo()
        # 失败结果不进缓存：权限恢复后必须立刻发现技能。
        assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]

    assert len(builtin_warnings(caplog)) == 1


def test_builtin_loader_degrades_when_the_scan_fails(tmp_path: Path, caplog, monkeypatch):
    """根目录在 is_dir() 与扫描之间被删掉（FileNotFoundError）同样只能退化成空。"""
    write_skill(tmp_path, "builtin", "demo")
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

    def vanished(category_root, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(category_root))

    monkeypatch.setattr(builtin_loader, "scan_skill_category", vanished)
    with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
        assert load_builtin_skills(config) == ()
        assert load_builtin_skills(config) == ()
        monkeypatch.undo()
        assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]

    assert len(builtin_warnings(caplog)) == 1
