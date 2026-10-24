"""`uv run python -m backend.scripts.migrate_skill_state`（M11 Task 16）。

M7 的两份 JSON 状态要迁到技能目录：

1. **全局开关**在 `extensions_config.json` 的 `skills.<name>.enabled`，迁到 PostgreSQL 的
   `skill_settings`（按内部 skill_id）。
2. **个人偏好**在 `skills_prefs.json` 的 `{user_id: {skill_name: bool}}`，键从旧名字改成
   skill_id（文件本身继续用，个人开关不搬进数据库）。

名字到 skill_id 的映射只来自**已批准的公共版本**（release.public_name）：私有草稿在旧
世界里根本不存在，猜测映射就是给自己造一个错的开关。

脚本可重入：已经迁过的开关和目标值相同就跳过，偏好里没有旧名字就整份文件不重写。
默认 dry-run，`--apply` 才落盘（和 migrate_user_buckets 同一套约定）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MigrationSummary:
    """一次迁移做了什么；测试和运维都靠它判断"是不是没东西可迁了"。"""

    mapped_skills: int = 0
    settings_written: int = 0
    pref_keys_rewritten: int = 0
    skipped_names: list[str] = field(default_factory=list)
    users_touched: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.settings_written + self.pref_keys_rewritten


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _legacy_global_switches(extensions: dict[str, Any]) -> dict[str, bool]:
    """`skills.<name>` 既可能是 bool，也可能是 `{"enabled": bool}`（M7 两种都见过）。"""
    raw = extensions.get("skills")
    if not isinstance(raw, dict):
        return {}
    switches: dict[str, bool] = {}
    for name, value in raw.items():
        if isinstance(value, bool):
            switches[str(name)] = value
        elif isinstance(value, dict) and isinstance(value.get("enabled"), bool):
            switches[str(name)] = bool(value["enabled"])
    return switches


async def migrate(
    *,
    store: Any,
    extensions_path: Path,
    prefs_path: Path,
    actor: str = "migration",
    apply: bool = True,
) -> MigrationSummary:
    """读旧 JSON、写 skill_settings、改偏好键。返回摘要（不抛单条失败）。"""

    from agentkit.persistence.schema import SkillSettingsRow
    from sqlalchemy import select

    summary = MigrationSummary()
    extensions = _read_json(Path(extensions_path))
    prefs = _read_json(Path(prefs_path))

    visible = await store.all_active()
    name_to_id: dict[str, str] = {}
    enabled_by_id: dict[str, bool] = {}
    for source in visible:
        if source.scope.value != "public":
            continue
        release = source.release
        if release is None:
            continue
        name_to_id[release.public_name] = source.version.skill_id
        enabled_by_id[source.version.skill_id] = bool(source.skill.enabled)
    summary.mapped_skills = len(name_to_id)

    async with store._sf() as session:  # noqa: SLF001 - 只读确认 settings 行是否存在
        existing = {
            row.skill_id: row.enabled
            for row in (await session.execute(select(SkillSettingsRow))).scalars()
        }

    for name, enabled in _legacy_global_switches(extensions).items():
        skill_id = name_to_id.get(name)
        if skill_id is None:
            summary.skipped_names.append(name)
            continue
        if existing.get(skill_id, True) == enabled and skill_id in existing:
            # 已经和目标一致：第二次运行就走到这里，什么都不写。
            continue
        if not apply:
            summary.settings_written += 1
            continue
        await store.set_global_enabled(skill_id, enabled, actor)
        existing[skill_id] = enabled
        summary.settings_written += 1

    skill_ids = set(name_to_id.values())
    rewritten = 0
    for user, entries in prefs.items():
        if not isinstance(entries, dict):
            continue
        changed = False
        updated: dict[str, Any] = {}
        for key, value in entries.items():
            key = str(key)
            target = name_to_id.get(key)
            if target is None or key in skill_ids:
                updated[key] = value
                continue
            # 同一个用户同时有旧名和新键时，新键（已经是 skill_id）说话。
            if target not in updated:
                updated[target] = value
                rewritten += 1
            changed = True
        if changed:
            prefs[user] = updated
            summary.users_touched.append(user)
    summary.pref_keys_rewritten = rewritten

    if summary.pref_keys_rewritten and apply:
        from agentkit.config.atomic_json import atomic_write_json

        atomic_write_json(Path(prefs_path), prefs)

    if apply and summary.changed:
        # 脚本绕过网关，必须自己发出跨 worker 失效提示；dry run 不写任何东西。
        bump = getattr(store, "bump_catalog_epoch", None)
        if callable(bump):
            await bump()

    return summary


def _report(summary: MigrationSummary) -> None:
    print(
        f"mapped {summary.mapped_skills} public skill(s): "
        f"{summary.settings_written} skill_settings row(s) written, "
        f"{summary.pref_keys_rewritten} preference key(s) rewritten "
        f"across {len(summary.users_touched)} user bucket(s)"
    )
    if summary.skipped_names:
        print(
            f"skipped {len(summary.skipped_names)} legacy name(s) without a public release: "
            + ", ".join(sorted(summary.skipped_names)[:10])
        )


async def run(
    *,
    extensions_path: Path,
    prefs_path: Path,
    actor: str,
    apply: bool,
) -> int:
    from agentkit.config import get_app_config
    from agentkit.persistence.engine import close_engine, get_session_factory, init_engine_from_config
    from agentkit.skills.store.sql import SqlSkillStore

    await init_engine_from_config(get_app_config().database)
    try:
        summary = await migrate(
            store=SqlSkillStore(get_session_factory()),
            extensions_path=extensions_path,
            prefs_path=prefs_path,
            actor=actor,
            apply=apply,
        )
        _report(summary)
        if not apply and summary.changed:
            print("[dry-run] nothing written; rerun with --apply")
        return 0
    finally:
        await close_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate JSON skill switches into skill_settings and skill_id prefs (M11 Task 16)."
    )
    parser.add_argument("--extensions-config", default=None, help="path to extensions_config.json")
    parser.add_argument("--skills-prefs", default=None, help="path to skills_prefs.json")
    parser.add_argument("--actor", default="migration", help="audit actor for skill_settings")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    args = parser.parse_args(argv)

    from agentkit.config.extensions_config import resolve_extensions_config_path
    from agentkit.config.skills_prefs import writable_skills_prefs_path

    extensions_path = (
        Path(args.extensions_config)
        if args.extensions_config
        else resolve_extensions_config_path()
    )
    if extensions_path is None:
        print("no extensions_config.json found; pass --extensions-config explicitly", flush=True)
        extensions_path = Path("extensions_config.json")
    prefs_path = (
        Path(args.skills_prefs) if args.skills_prefs else writable_skills_prefs_path()
    )

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(
        run(
            extensions_path=Path(extensions_path),
            prefs_path=Path(prefs_path),
            actor=args.actor,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
