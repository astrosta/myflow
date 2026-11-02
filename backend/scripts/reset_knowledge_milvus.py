"""`uv run python -m backend.scripts.reset_knowledge_milvus`（设计文档 §17.5）。

父子块这轮的 schema **不向后兼容**：老知识库 collection 没有 `parent_id` 字段，写入会
直接抛 `KnowledgeUnavailable`（要删旧 collection 才能重建）。本脚本只处理 Milvus 侧：

1. 删父 collection（`{collection}_parents`）——先删，避免留下没人引用的孤儿父块；
2. 删子 collection（`{collection}`），下次入库按新 schema 重建。

SQL 元数据、上传文件不在本脚本范围：那是网关的删除接口（删知识库/删文档）负责的部分，
两边都做完才算 §17.5 的"删库重建"。默认 dry-run，`--apply` 才真删。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from agentkit.config.app_config import AppConfig
from agentkit.knowledge.config import MilvusConfig
from agentkit.knowledge.vector_store.milvus import PARENT_COLLECTION_SUFFIX


def target_collections(collection: str) -> list[str]:
    """父 collection 在前：中途失败时不会留下孤儿父块。"""
    return [collection + PARENT_COLLECTION_SUFFIX, collection]


def load_vector_store_config(config_path: Path | None = None) -> MilvusConfig:
    """只读 `vector_store` 两个块。

    整份 AppConfig 会展开所有 `$ENV`（模型 key 之类），但删 collection 不需要模型凭据：
    运维脚本不该因为某个 key 没导出就跑不起来。合并规则和 AppConfig 一致——
    顶层 `vector_store` 是共享连接参数，`knowledge_base.vector_store` 覆盖知识库自己的契约。
    """
    path = AppConfig.resolve_config_path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} is not a mapping")
    shared = raw.get("vector_store") or {}
    knowledge = (raw.get("knowledge_base") or {}).get("vector_store") or {}
    merged = {**shared, **knowledge}
    return MilvusConfig.model_validate(AppConfig.resolve_env_variables(merged))


def _default_client_factory(config: MilvusConfig) -> Any:
    try:
        from pymilvus import MilvusClient
    except ImportError:  # pragma: no cover - 只在没装 knowledge extra 时触发
        raise SystemExit(
            "pymilvus is not installed; run with the knowledge extra "
            "(`uv run --extra knowledge python -m backend.scripts.reset_knowledge_milvus`)"
        ) from None
    kwargs: dict[str, Any] = {"uri": config.uri}
    if config.token:
        kwargs["token"] = config.token
    if config.db_name:
        kwargs["db_name"] = config.db_name
    return MilvusClient(**kwargs)


def reset(
    *,
    config: MilvusConfig,
    dry_run: bool,
    client_factory: Callable[[MilvusConfig], Any] | None = None,
) -> int:
    """返回失败数；单个 collection 失败继续，最后非零退出。"""
    if not config.uri:
        print(
            "knowledge_base.vector_store.uri is empty: nothing to reset. "
            "Set it in config.yaml first.",
            file=sys.stderr,
        )
        return 2
    client = (client_factory or _default_client_factory)(config)
    failures = 0
    try:
        for name in target_collections(config.collection):
            try:
                exists = bool(client.has_collection(name))
            except Exception as exc:  # noqa: BLE001 - 连接问题要原样报给运维
                print(f"cannot inspect {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
                continue
            if not exists:
                print(f"skip {name}: collection does not exist")
                continue
            if dry_run:
                print(f"[dry-run] drop {name}")
                continue
            try:
                client.drop_collection(name)
            except Exception as exc:  # noqa: BLE001
                print(f"drop {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                failures += 1
                continue
            print(f"dropped {name}")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    if dry_run:
        print("[dry-run] nothing was dropped; re-run with --apply")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drop the knowledge Milvus collections so the new schema can rebuild (§17.5)."
    )
    parser.add_argument("--config", default=None, help="config.yaml path (default: resolve it)")
    parser.add_argument(
        "--collection",
        default=None,
        help="override knowledge_base.vector_store.collection (default: config.yaml)",
    )
    parser.add_argument("--apply", action="store_true", help="actually drop (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    args = parser.parse_args(argv)
    config = load_vector_store_config(Path(args.config) if args.config else None)
    if args.collection:
        # 走 model_validate 而不是 model_copy：collection 名的正则校验不能被绕过。
        config = MilvusConfig.model_validate(
            {**config.model_dump(), "collection": args.collection}
        )
    print(f"milvus {config.uri or '(no uri)'} db={config.db_name or 'default'}")
    return reset(config=config, dry_run=not args.apply or args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
