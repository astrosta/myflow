"""Adapter contract for the lazily imported cisco-ai-skill-scanner package."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agentkit.config.model_config import ModelConfig
from agentkit.skills.domain import SkillScanUnavailable
from agentkit.skills.security_scan import CiscoSkillScanner, LlmScanConfig


class _Result:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


def _payload(**overrides) -> dict:
    data = {
        "is_safe": True,
        "max_severity": "SAFE",
        "findings": [],
        "analyzers_used": ["static_analyzer", "llm_analyzer"],
    }
    data.update(overrides)
    return data


def test_scan_normalizes_findings_and_never_keeps_snippets(tmp_path) -> None:
    target = tmp_path / "skill"
    target.mkdir()
    (target / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n", encoding="utf-8")
    payload = _payload(
        is_safe=False,
        max_severity="high",
        findings=[
            {
                "rule_id": "R1",
                "title": "Prompt injection",
                "severity": "HIGH",
                "category": "prompt_injection",
                "file_path": "SKILL.md",
                "line_number": "7",
                "remediation": "remove it",
                "snippet": "SECRET BODY",
                "metadata": {"raw": "also secret"},
            }
        ],
    )
    seen: list[list[str]] = []

    def fake_scan(_path: str, *, analyzers):
        seen.append(analyzers)
        return _Result(payload)

    scanner = CiscoSkillScanner(
        scan_call=fake_scan,
        analyzer_builder=lambda: ["rule", "llm"],
        version="2.3.2",
        translator=lambda report: report,
    )

    statuses: list[str] = []
    outcome = scanner.scan(target, on_status=statuses.append)

    assert statuses == [
        "正在准备扫描器",
        "正在做规则检查，并交给模型分析",
        "正在把发现项写成中文",
    ]
    assert outcome.status == "completed"
    assert outcome.is_safe is False
    assert outcome.max_severity == "HIGH"
    assert outcome.scanner_version == "2.3.2"
    assert outcome.findings == (
        {
            "rule_id": "R1",
            "title": "Prompt injection",
            "severity": "HIGH",
            "category": "prompt_injection",
            "file_path": "SKILL.md",
            "line_number": 7,
            "remediation": "remove it",
        },
    )
    assert "SECRET BODY" not in str(outcome)
    # The same scan call receives both the rule and LLM analyzers.
    assert seen == [["rule", "llm"]]


def test_scan_translates_prose_without_changing_the_verdict(tmp_path) -> None:
    target = tmp_path / "skill"
    target.mkdir()
    payload = _payload(
        is_safe=False,
        max_severity="HIGH",
        findings=[
            {
                "rule_id": "R1",
                "title": "Prompt injection",
                "severity": "HIGH",
                "category": "prompt_injection",
                "file_path": "SKILL.md",
                "line_number": 7,
                "remediation": "remove it",
            }
        ],
    )

    def translate(report: str) -> str:
        rows = json.loads(report)
        rows[0]["title"] = "提示注入"
        rows[0]["remediation"] = "删除这段指令"
        rows[0]["severity"] = "LOW"
        rows.append({"rule_id": "NEW", "title": "新问题", "severity": "CRITICAL"})
        return json.dumps(rows, ensure_ascii=False)

    outcome = CiscoSkillScanner(
        scan_call=lambda _path, *, analyzers: _Result(payload),
        analyzer_builder=lambda: [],
        version="2.3.2",
        translator=translate,
    ).scan(target)

    assert outcome.status == "completed"
    assert outcome.max_severity == "HIGH"
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert finding["title"] == "提示注入"
    assert finding["remediation"] == "删除这段指令"
    assert finding["severity"] == "HIGH"
    assert finding["rule_id"] == "R1"
    assert finding["category"] == "prompt_injection"
    assert finding["file_path"] == "SKILL.md"
    assert finding["line_number"] == 7


def test_scan_exception_is_a_failed_result_not_a_raise(tmp_path) -> None:
    def boom(_path: str, *, analyzers):
        raise RuntimeError("scanner exploded")

    outcome = CiscoSkillScanner(
        scan_call=boom, analyzer_builder=lambda: [], version="2.3.2"
    ).scan(tmp_path)

    assert outcome.status == "failed"
    assert outcome.is_safe is False
    assert outcome.error is not None
    assert "RuntimeError" in outcome.error


def test_llm_that_never_ran_cannot_pass_as_a_rule_only_scan(tmp_path) -> None:
    payload = _payload(analyzers_used=["static_analyzer"])

    outcome = CiscoSkillScanner(
        scan_call=lambda _path, *, analyzers: _Result(payload),
        analyzer_builder=lambda: [],
        version="2.3.2",
    ).scan(tmp_path)

    assert outcome.status == "failed"
    assert outcome.is_safe is False
    assert outcome.error is not None and "LLM analyzer did not run" in outcome.error


def test_missing_rule_analyzers_cannot_pass_as_an_llm_only_scan(tmp_path) -> None:
    payload = _payload(analyzers_used=["llm_analyzer"])

    outcome = CiscoSkillScanner(
        scan_call=lambda _path, *, analyzers: _Result(payload),
        analyzer_builder=lambda: [],
        version="2.3.2",
    ).scan(tmp_path)

    assert outcome.status == "failed"
    assert outcome.is_safe is False
    assert outcome.error is not None and "rule analyzers did not run" in outcome.error


def test_llm_analyzer_error_fails_the_scan_even_when_rules_are_clean(tmp_path) -> None:
    payload = _payload(
        analyzers_failed=[{"analyzer": "llm_analyzer", "error": "rate limited"}]
    )

    outcome = CiscoSkillScanner(
        scan_call=lambda _path, *, analyzers: _Result(payload),
        analyzer_builder=lambda: [],
        version="2.3.2",
    ).scan(tmp_path)

    assert outcome.status == "failed"
    assert outcome.is_safe is False
    assert outcome.error is not None
    assert "LLM analyzer failed" in outcome.error
    assert "rate limited" in outcome.error


def test_analyzers_combine_rules_with_the_default_model(monkeypatch) -> None:
    model = ModelConfig(
        name="primary",
        use="langchain_deepseek:ChatDeepSeek",
        model="deepseek-flash",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
    )
    config = SimpleNamespace(
        agents=SimpleNamespace(default_model="primary"),
        models=[model],
        get_model_config=lambda name: model if name == "primary" else None,
    )
    monkeypatch.setattr("agentkit.config.get_app_config", lambda: config)

    analyzers = CiscoSkillScanner()._analyzers()

    names = {analyzer.get_name() for analyzer in analyzers}
    assert {"static_analyzer", "bytecode", "pipeline", "llm_analyzer"} <= names
    llm = next(analyzer for analyzer in analyzers if analyzer.get_name() == "llm_analyzer")
    assert llm.provider_config.model == "openai/deepseek-flash"
    assert llm.provider_config.base_url == "https://api.deepseek.com"
    assert llm.provider_config.is_openai_compatible is True
    assert llm.provider_config.api_key == "sk-test"
    assert llm.max_tokens == 32768
    assert llm.llm_policy.max_code_file_chars == 50_000
    assert llm.llm_policy.max_referenced_file_chars == 50_000
    assert llm.llm_policy.max_total_prompt_chars == 200_000
    assert "使用中文" not in llm.prompt_builder.threat_analysis_prompt
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_missing_default_model_is_typed_unavailable(monkeypatch) -> None:
    config = SimpleNamespace(
        agents=SimpleNamespace(default_model=None),
        models=[],
        get_model_config=lambda _name: None,
    )
    monkeypatch.setattr("agentkit.config.get_app_config", lambda: config)

    with pytest.raises(SkillScanUnavailable, match="default model"):
        CiscoSkillScanner()._analyzers()


def test_missing_scanner_package_is_a_typed_unavailable(monkeypatch, tmp_path) -> None:
    def blocked(name: str):
        if name == "skill_scanner":
            raise ImportError("not installed")
        raise AssertionError(name)

    monkeypatch.setattr("agentkit.skills.security_scan.importlib.import_module", blocked)

    with pytest.raises(SkillScanUnavailable, match="cisco-ai-skill-scanner"):
        CiscoSkillScanner().scan(tmp_path)
