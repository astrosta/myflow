"""§17.5 的 reset 脚本：只删知识库两套 collection，默认 dry-run。"""

from __future__ import annotations

from agentkit.knowledge.config import MilvusConfig
from backend.scripts.reset_knowledge_milvus import (
    load_vector_store_config,
    reset,
    target_collections,
)


class FakeClient:
    def __init__(self, existing: set[str], *, drop_error: Exception | None = None) -> None:
        self.existing = set(existing)
        self.dropped: list[str] = []
        self.drop_error = drop_error
        self.closed = False

    def has_collection(self, name: str) -> bool:
        return name in self.existing

    def drop_collection(self, name: str) -> None:
        if self.drop_error is not None:
            raise self.drop_error
        self.dropped.append(name)
        self.existing.discard(name)

    def close(self) -> None:
        self.closed = True


def _config() -> MilvusConfig:
    return MilvusConfig(uri="http://milvus:19530", collection="kb")


def test_target_collections_drops_the_parent_first() -> None:
    assert target_collections("kb") == ["kb_parents", "kb"]


def test_load_vector_store_config_merges_shared_and_knowledge_blocks(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MILVUS_TOKEN", "secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "vector_store:\n"
        "  uri: http://milvus:19530\n"
        "  token: $MILVUS_TOKEN\n"
        "knowledge_base:\n"
        "  vector_store:\n"
        "    collection: kb_custom\n"
        "models:\n"
        "  - name: x\n"
        "    use: y\n"
        "    api_key: $UNSET_KEY_THAT_MUST_NOT_BE_EXPANDED\n",
        encoding="utf-8",
    )

    config = load_vector_store_config(config_path)

    assert config.collection == "kb_custom"
    assert config.uri == "http://milvus:19530"
    # 只展开 vector_store 块：模型 key 没导出也不该拦住删除 collection。
    assert config.token == "secret"


def test_dry_run_reports_without_dropping() -> None:
    client = FakeClient({"kb", "kb_parents"})

    code = reset(config=_config(), dry_run=True, client_factory=lambda _config: client)

    assert code == 0
    assert client.dropped == []
    assert client.closed is True


def test_apply_drops_both_collections_and_skips_missing_ones() -> None:
    client = FakeClient({"kb", "kb_parents"})

    code = reset(config=_config(), dry_run=False, client_factory=lambda _config: client)

    assert code == 0
    assert client.dropped == ["kb_parents", "kb"]

    again = FakeClient({"kb"})
    assert reset(config=_config(), dry_run=False, client_factory=lambda _config: again) == 0
    assert again.dropped == ["kb"]


def test_missing_uri_is_an_actionable_error() -> None:
    called = False

    def factory(_config: MilvusConfig) -> FakeClient:  # pragma: no cover - 不该被调用
        nonlocal called
        called = True
        return FakeClient(set())

    config = MilvusConfig(collection="kb")
    assert reset(config=config, dry_run=False, client_factory=factory) == 2
    assert called is False


def test_drop_failure_is_reported_but_the_other_collection_is_still_attempted() -> None:
    client = FakeClient({"kb", "kb_parents"}, drop_error=RuntimeError("boom"))

    code = reset(config=_config(), dry_run=False, client_factory=lambda _config: client)

    assert code == 1
    assert client.closed is True
