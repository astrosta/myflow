"""Adapter from agentkit skills to Cisco's cisco-ai-skill-scanner package.

Every scan runs two modes in one pass:

* deterministic rule analyzers (static YARA/AST, bytecode, pipeline and
  correlation), and
* an LLM semantic analyzer.

The LLM target is the deployment's default chat model (`agents.default_model`,
falling back to `models[0]`), resolved lazily at scan time.  There is no
separate scanner API key in myflow config: the scanner reuses the model's
`base_url` and `api_key` through an OpenAI-compatible route.  A missing default
model is a typed 503 instead of a silent rule-only scan.

The scanner is an optional dependency.  Nothing imports it at gateway startup:
the import happens inside scan() on a worker thread, only after the owner
clicks "security scan".  A missing package is a typed 503 at the API boundary,
not an import-time failure that takes the whole gateway down.

Only normalized finding metadata is returned.  Scanner findings may contain
code snippets, environment details or other sensitive context; those are not
persisted by the catalog.  Skill content is sent to the configured model
provider because the LLM pass is part of the requested scan.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentkit.skills.domain import SkillScanUnavailable

# 技能扫描会拉起 LiteLLM。远程价目表只用于估价，连不上 GitHub 时会空等重试。
# 扫描结果不依赖它，固定使用安装包里的本地备份。
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

logger = logging.getLogger(__name__)

SCANNER_NAME = "cisco-ai-skill-scanner"
SCANNER_MODULE = "skill_scanner"
# 扫描器默认 8192。威胁分析写满就会被截断并判失败，这里把单次输出抬到 32768。
LLM_MAX_TOKENS = 32768
# 默认策略里代码文件 15000 字、被引用文件 10000 字，超了就不整份送进 LLM。
# 总预算仍是 100000 时，加大单文件上限也装不进去，所以一起放宽。
LLM_MAX_CODE_FILE_CHARS = 50_000
LLM_MAX_REFERENCED_FILE_CHARS = 50_000
LLM_MAX_TOTAL_PROMPT_CHARS = 200_000
# 库函数没有命令行那种状态回调。这三句是同一次扫描里真正会停住的阶段，
# 由路由推给前端，代替 CLI 打在 stderr 上的那几行。
SCAN_STATUS_PREPARE = "正在准备扫描器"
SCAN_STATUS_ANALYZE = "正在做规则检查，并交给模型分析"
SCAN_STATUS_TRANSLATE = "正在把发现项写成中文"

SEVERITY_ORDER = ("SAFE", "INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
_SEVERITY_ALIASES = {value.upper(): value.upper() for value in SEVERITY_ORDER}
_LLM_ANALYZERS = frozenset({"llm_analyzer", "meta_analyzer"})
_RULE_ANALYZERS = frozenset({"static_analyzer", "bytecode", "pipeline", "correlation"})


def _text(value: object, limit: int = 512) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _severity(value: object) -> str:
    raw = str(getattr(value, "value", value) or "SAFE").upper()
    return _SEVERITY_ALIASES.get(raw, "SAFE")


def _normalize_findings(raw: object) -> tuple[dict[str, Any], ...]:
    """Keep only bounded, non-body finding metadata."""
    if not isinstance(raw, list):
        return ()
    findings: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        line = item.get("line_number")
        try:
            line_number = int(line) if line is not None else None
        except (TypeError, ValueError):
            line_number = None
        findings.append(
            {
                "rule_id": _text(item.get("rule_id") or item.get("id"), 128),
                "title": _text(item.get("title"), 256),
                "severity": _severity(item.get("severity")),
                "category": _text(
                    getattr(item.get("category"), "value", item.get("category")), 128
                ),
                "file_path": _text(item.get("file_path"), 1024) or None,
                "line_number": line_number,
                "remediation": _text(item.get("remediation"), 1024) or None,
            }
        )
    return tuple(findings)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

_TRANSLATE_PROMPT = """\
你是一名 AI Agent Skills 安全审核专家。

请将下面的 Skill 安全扫描报告转换为中文。

要求：
1. 所有风险描述和修复建议使用中文。
2. 保留 rule_id、severity、category 等机器可读字段。
3. 不得修改扫描结果的风险等级。
4. 不得新增扫描器未检测到的安全问题。
5. 保留文件路径和行号。
6. 输出 JSON 数组，元素字段与输入一致。

扫描报告：
{scan_report}
"""


def translate_scan_report(scan_report: str, llm: Any) -> str:
    """Ask the chat model to rewrite finding prose in Chinese."""

    response = llm.invoke(_TRANSLATE_PROMPT.format(scan_report=scan_report))
    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                parts.append(str(part.get("text") or ""))
        content = "".join(parts)
    return str(content)


def _translated_findings(findings: tuple[dict[str, Any], ...], raw: str) -> tuple[dict[str, Any], ...]:
    """Keep scanner identity fields; replace only the Chinese prose."""

    match = _JSON_FENCE.search(raw)
    payload = match.group(1) if match else raw
    start = payload.find("[")
    end = payload.rfind("]")
    if start < 0 or end < start:
        wrapped = payload.find("{")
        if wrapped >= 0:
            try:
                obj = json.loads(payload[wrapped:])
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, Mapping) and isinstance(obj.get("findings"), list):
                payload = json.dumps(obj["findings"], ensure_ascii=False)
                start = 0
                end = len(payload) - 1
    if start < 0 or end < start:
        raise ValueError("translator did not return a JSON array")
    parsed = json.loads(payload[start : end + 1])
    if not isinstance(parsed, list) or len(parsed) < len(findings):
        raise ValueError("translator dropped a finding")
    merged: list[dict[str, Any]] = []
    for original, item in zip(findings, parsed, strict=False):
        if not isinstance(item, Mapping):
            raise ValueError("translator returned a non-object finding")
        updated = dict(original)
        title = _text(item.get("title"), 256)
        remediation = _text(item.get("remediation"), 1024) or None
        if title:
            updated["title"] = title
        if remediation:
            updated["remediation"] = remediation
        merged.append(updated)
    return tuple(merged)


def _llm_failure(data: Mapping[str, Any]) -> str | None:
    """Reject a scan that silently degraded to rules only.

    The scanner reports `analyzers_used` and `analyzers_failed`; either an LLM
    analyzer that never ran or one that raised means the caller did not get the
    rule + LLM result it asked for, even when no HIGH finding exists.
    """
    used = data.get("analyzers_used")
    used_names = {str(name) for name in used} if isinstance(used, list) else set()
    if not used_names & _RULE_ANALYZERS:
        return "the rule analyzers did not run; LLM-only results are not accepted"
    if "llm_analyzer" not in used_names:
        return "the LLM analyzer did not run; rule-only results are not accepted"
    failures = data.get("analyzers_failed")
    if isinstance(failures, list):
        for item in failures:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("analyzer")) in _LLM_ANALYZERS:
                detail = _text(item.get("error") or "unknown error", 300)
                return f"the LLM analyzer failed: {detail}"
    return None


@dataclass(frozen=True, slots=True)
class LlmScanConfig:
    """Connection details for the scanner's LLM semantic pass."""

    model: str
    api_key: str | None = None
    base_url: str | None = None
    provider: str | None = None


def _provider_for_use(use: str) -> str:
    """Map a myflow model class path onto a scanner provider id.

    myflow talks to DeepSeek/OpenAI-style gateways through ChatOpenAI-compatible
    clients, so everything that is not clearly Anthropic or Ollama stays on the
    OpenAI-compatible route with the configured base URL.
    """
    lowered = (use or "").lower()
    if "anthropic" in lowered:
        return "anthropic"
    if "ollama" in lowered:
        return "ollama"
    return "openai-compatible"


def default_llm_scan_config() -> LlmScanConfig:
    """Resolve the same default model the chat runtime uses.

    Imported lazily so the gateway can boot, and rule-only callers can be
    exercised, without loading config or the model stack at import time.
    """
    from agentkit.config import get_app_config

    config = get_app_config()
    name = config.agents.default_model
    if not name and config.models:
        name = config.models[0].name
    if not name:
        raise SkillScanUnavailable(
            "no default model configured; set agents.default_model to run LLM scans"
        )
    model_config = config.get_model_config(name)
    if model_config is None:
        raise SkillScanUnavailable(
            f"default model {name!r} is not listed in models[]; cannot run LLM scans"
        )
    return LlmScanConfig(
        model=model_config.model,
        api_key=model_config.api_key or None,
        base_url=model_config.base_url or None,
        provider=_provider_for_use(model_config.use),
    )


@dataclass(frozen=True, slots=True)
class SecurityScanOutcome:
    """What the adapter hands back before the catalog persists it."""

    status: str  # completed | failed
    is_safe: bool
    max_severity: str
    findings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    scanner_version: str | None = None
    duration_ms: int = 0
    error: str | None = None


class SkillSecurityScanner(Protocol):
    """The small seam used by the author service and route tests."""

    def scan(self, skill_directory: Path) -> SecurityScanOutcome: ...


class CiscoSkillScanner:
    """Lazy import wrapper that runs rule analyzers plus one LLM pass."""

    def __init__(
        self,
        *,
        scan_call: Callable[..., Any] | None = None,
        analyzer_builder: Callable[[], list[Any]] | None = None,
        llm_config: LlmScanConfig | None = None,
        version: str | None = None,
        translator: Callable[[str], str] | None = None,
    ) -> None:
        self._scan_call = scan_call
        self._analyzer_builder = analyzer_builder
        self._llm_config = llm_config
        self._version = version
        self._translator = translator

    def scan(
        self,
        skill_directory: Path,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> SecurityScanOutcome:
        def status(message: str) -> None:
            if on_status is None:
                return
            try:
                on_status(message)
            except Exception:
                logger.warning("skill scan status callback failed", exc_info=True)

        started = time.monotonic()
        status(SCAN_STATUS_PREPARE)
        scanner = self._load_scanner()
        analyzers = self._analyzers()
        status(SCAN_STATUS_ANALYZE)
        try:
            result = scanner(str(skill_directory), analyzers=analyzers)
            data = result.to_dict() if hasattr(result, "to_dict") else result
            if not isinstance(data, Mapping):
                raise TypeError("skill scanner returned an unsupported result")
            findings = _normalize_findings(data.get("findings"))
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            failure = _llm_failure(data)
            if failure is None and findings:
                status(SCAN_STATUS_TRANSLATE)
                findings = self._translate_findings(findings)
            if failure is not None:
                return SecurityScanOutcome(
                    status="failed",
                    is_safe=False,
                    max_severity=_severity(data.get("max_severity")),
                    findings=findings,
                    scanner_version=self._scanner_version(),
                    duration_ms=duration_ms,
                    error=failure,
                )
            return SecurityScanOutcome(
                status="completed",
                is_safe=bool(data.get("is_safe")),
                max_severity=_severity(data.get("max_severity")),
                findings=findings,
                scanner_version=self._scanner_version(),
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 - a failed scan is persisted, not hidden
            logger.warning(
                "cisco-ai-skill-scanner failed for %s (%s)",
                _text(skill_directory.name, 128),
                type(exc).__name__,
            )
            return SecurityScanOutcome(
                status="failed",
                is_safe=False,
                max_severity="SAFE",
                scanner_version=self._scanner_version(),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=f"{type(exc).__name__}: {_text(exc)}",
            )

    def _translate_findings(
        self, findings: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        """Rewrite title and remediation in Chinese. A bad translation keeps the English findings."""

        report = json.dumps(list(findings), ensure_ascii=False)
        try:
            if self._translator is not None:
                raw = self._translator(report)
            else:
                from agentkit.models.factory import create_chat_model

                raw = translate_scan_report(
                    report,
                    create_chat_model(thinking_enabled=False, attach_tracing=False),
                )
            return _translated_findings(findings, raw)
        except Exception:
            logger.warning("skill scan translation failed; keeping the scanner text", exc_info=True)
            return findings

    def _analyzers(self) -> list[Any]:
        """Build rule analyzers plus the LLM analyzer for the default model."""
        if self._analyzer_builder is not None:
            return self._analyzer_builder()
        try:
            llm = self._llm_config or default_llm_scan_config()
        except SkillScanUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - config problems are a typed 503 too
            raise SkillScanUnavailable(
                f"the default model for LLM scanning could not be resolved: {_text(exc, 300)}"
            ) from exc
        try:
            from skill_scanner.core.analyzer_factory import build_analyzers
            from skill_scanner.core.scan_policy import ScanPolicy
        except ImportError as exc:
            raise SkillScanUnavailable(
                "cisco-ai-skill-scanner is not installed; install the skills extra "
                "(uv sync --extra skills)"
            ) from exc
        try:
            policy = ScanPolicy.default()
            policy.llm_analysis.max_code_file_chars = LLM_MAX_CODE_FILE_CHARS
            policy.llm_analysis.max_referenced_file_chars = LLM_MAX_REFERENCED_FILE_CHARS
            policy.llm_analysis.max_total_prompt_chars = LLM_MAX_TOTAL_PROMPT_CHARS
            analyzers = build_analyzers(
                policy,
                use_llm=True,
                llm_model=llm.model,
                llm_api_key=llm.api_key,
                llm_base_url=llm.base_url,
                llm_provider=llm.provider,
                llm_max_tokens=LLM_MAX_TOKENS,
            )
        except SkillScanUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - surface a typed 503, never a 500
            raise SkillScanUnavailable(
                f"security scan analyzers could not be configured: {_text(exc, 300)}"
            ) from exc
        if not any(
            str(getattr(analyzer, "get_name", lambda: "")()) in _LLM_ANALYZERS
            for analyzer in analyzers
        ):
            raise SkillScanUnavailable(
                "cisco-ai-skill-scanner did not build the requested LLM analyzer"
            )
        logger.info(
            "Security scan uses %d rule analyzer(s) plus LLM model %s",
            len(analyzers) - 1,
            llm.model,
        )
        return analyzers

    def _load_scanner(self) -> Callable[..., Any]:
        if self._scan_call is not None:
            return self._scan_call
        try:
            module = importlib.import_module(SCANNER_MODULE)
        except ImportError as exc:
            raise SkillScanUnavailable(
                "cisco-ai-skill-scanner is not installed; install the skills extra "
                "(uv sync --extra skills)"
            ) from exc
        scan = getattr(module, "scan_skill", None)
        if not callable(scan):
            raise SkillScanUnavailable(
                "the installed cisco-ai-skill-scanner exposes no scan_skill entrypoint"
            )
        return scan

    def _scanner_version(self) -> str | None:
        if self._version is not None:
            return self._version
        try:
            self._version = importlib.metadata.version(SCANNER_NAME)
        except importlib.metadata.PackageNotFoundError:
            return None
        return self._version


__all__ = [
    "CiscoSkillScanner",
    "LlmScanConfig",
    "SCANNER_NAME",
    "SEVERITY_ORDER",
    "SecurityScanOutcome",
    "SkillSecurityScanner",
    "default_llm_scan_config",
    "translate_scan_report",
]
