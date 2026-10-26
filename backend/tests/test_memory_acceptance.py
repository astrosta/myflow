"""Task 15 acceptance contracts that are cheap and deterministic."""

from __future__ import annotations

from pathlib import Path

from agentkit.agents.memory.canonical.metrics import MemoryMetrics

ROOT = Path(__file__).resolve().parents[1] / "packages/harness/agentkit/agents/memory/canonical"


def test_metrics_count_without_content() -> None:
    metrics = MemoryMetrics()
    metrics.inc("semantic_active", 2)
    metrics.observe("milvus_p95_ms", 12)
    snapshot = metrics.snapshot()
    assert snapshot["semantic_active"] == 2
    assert snapshot["milvus_p95_ms_avg"] == 12
    assert "content" not in snapshot


def test_canonical_sources_do_not_use_store_manager() -> None:
    sources = "".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    assert "create_memory_store_manager" not in sources


def test_canonical_profile_has_no_pin_field() -> None:
    assert "pinned" not in (ROOT / "profile.py").read_text(encoding="utf-8")
    assert "pinned" not in (ROOT / "domain.py").read_text(encoding="utf-8")
