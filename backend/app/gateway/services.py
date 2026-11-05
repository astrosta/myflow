"""
start_run、sse_comsumer、format_sse
"""
import asyncio
import hashlib
import logging
import json
import re
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from fastapi import Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages
from langgraph.types import Command

from agentkit.agents.leader_agent import (
    CHECKPOINT_COORDINATE_KEYS,
    CLIENT_ASSEMBLY_KEY_TYPES,
    CLIENT_ASSEMBLY_KEYS,
    CLIENT_IDENTITY_KEYS,
    CONTEXT_ONLY_KEYS,
    SERVER_OWNED_KEYS,
)
from agentkit.agents.middlewares.pii_middleware import PII_VAULT_CONTEXT_KEY
from agentkit.agents.middlewares.provenance import PROVENANCE_KEYS
from agentkit.subagents.status_contract import SUBAGENT_TOOL_MESSAGE_KEYS
from agentkit.privacy import PIIRedactor, PIIVault, redact_raw_input
from agentkit.utils.messages import ORIGINAL_USER_CONTENT_KEY
from agentkit.config import AppConfig, get_app_config
from agentkit.models.catalog import VENDOR_SEPARATOR, split_model_name
from agentkit.runtime.context import RunContext
from agentkit.runtime.secret_context import REDACTED_CONTEXT_KEYS, REQUEST_SECRETS_KEY
from agentkit.runtime.runs import (
    DisconnectMode,
    RunManager,
    RunRecord,
    RunStatus,
    ConflictError,
)
from agentkit.runtime.stream_bridge import (
    StreamBridge,
    StreamGap,
    HEARTBEAT_SENTINEL,
    END_SENTINEL,
)
from agentkit.runtime.stream_modes import (
    UnsupportedStreamModeError,
    normalize_stream_modes,
)
from agentkit.trace_context import TRACE_METADATA_KEY, ensure_trace_id
from agentkit.utils import new_run_id, validate_thread_id
from agentkit.runtime.runs import (
    run_after_metadata,
    AgentFactory,
)

from .deps import RunServices
from .run_models import RunCaller, RunCreateRequest, RunLaunch

logger = logging.getLogger(__name__)
#: 厂商侧模型 id 的字符集（`deepseek-chat`、`gpt-4o-mini`、`claude-3-5-sonnet`…）。
_VENDOR_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_CLIENT_INPUT_CHANNELS = frozenset({"messages"})
_CLIENT_MESSAGE_TYPES = frozenset({"human"})

# user_role / authz_attributes：M9 的 Principal 从 runtime.context 读它们，客户端不能预置
_DROPPED_KEYS = (
    SERVER_OWNED_KEYS | CLIENT_IDENTITY_KEYS | CHECKPOINT_COORDINATE_KEYS | CONTEXT_ONLY_KEYS
    | {"user_role", "authz_attributes"}
)

_CREDDENTIAL_METADATA_KEYS = frozenset({"auth_token"})

#: 客户端可以塞进来的身份别名（§2.1 的 `_CLIENT_IDENTITY_KEYS`）。
_CLIENT_IDENTITY_KEYS = ("user_id", "owner_id", "auth_user", "auth_user_id")

# 客户端不能伪造的 additional_kwargs 键。internal 请求只保留 original_user_content。
# M8 §9.3：子代理的终态元数据（subagent_status / stop_reason / error / brief / sha256 /
# model_name / token_usage）只接受**本进程 task 工具**盖过的值——回放的历史消息里
# 带着这些键，一律当伪造处理。
_SERVER_OWNED_MESSAGE_KEYS = PROVENANCE_KEYS | SUBAGENT_TOOL_MESSAGE_KEYS | {
    ORIGINAL_USER_CONTENT_KEY,
    "hide_from_ui",
    "dynamic_context_reminder",
    "reminder_date",
    # 画像注入标记只能由服务端 OpenViking capture middleware 盖。
    "memory_profile",
    "memory_profile_etag",
    "memory_profile_revision",
    "memory_profile_sha256",
    "memory_profile_tokens",
    "memory_profile_truncated",
}
SERVER_OWNED_MESSAGE_KEYS: frozenset[str] = _SERVER_OWNED_MESSAGE_KEYS

#保留的metadata key，不放到RunConfigable的metadata里面，TRACE_METADATA_KEY重新生成
_RESERVED_METADATA_KEYS = CLIENT_IDENTITY_KEYS | {TRACE_METADATA_KEY}

# 默认循环次数
DEFAULT_RECURSION_LIMIT = 100
_PASSTHROUGH_CONFIG_KEYS = frozenset({"tags", "run_name"})
_HANDLED_CONFIG_KEYS = _PASSTHROUGH_CONFIG_KEYS | {"recursion_limit", "metadata", "configurable", "context"}



class StartRunError(Exception):
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"

class RunPermissionDenied(StartRunError): status_code, code = 403, "permission_denied"
class RunRequestInvalid(StartRunError):     status_code, code = 422, "invalid_run_request"
class ModelNotAllowed(StartRunError):       status_code, code = 400, "model_not_allowed"
class ThreadNotFound(StartRunError):        status_code, code = 404, "thread_not_found"
class AssistantNotFound(StartRunError):     status_code, code = 404, "assistant_not_found"
class CheckpointNotFound(StartRunError):    status_code, code = 404, "checkpoint_not_found"
class RunNotFound(StartRunError):           status_code, code = 404, "run_not_found"
class RunNotLocal(StartRunError):          status_code, code = 409, "run_not_local"
class RunNotCancellable(StartRunError):    status_code, code = 409, "run_not_cancellable"
class ThreadBusy(StartRunError):            status_code, code = 409, "thread_busy"
class IdempotencyKeyReused(StartRunError):  status_code, code = 409, "idempotency_key_reused"
class ClientDisconnected(StartRunError): status_code, code = 499, "client_disconnected"# 499：客户端在等待期间断开

async def start_run(
    body: RunCreateRequest,
    *,
    thread_id: str,
    caller: RunCaller,
    services: RunServices,
    idempotency_key: str | None = None,
    require_existing_thread: bool = False,
) -> RunLaunch:
    """
    """
    app_config = get_app_config()
    ctx = services.run_context

    # 客户端声明的身份一律**删掉**（不是校验后使用）：所有入口共用这一处（§2.1）
    strip_client_identity(body)

    # 判断如果要取消旧run，是否有权限
    _require_cancel_capability(caller, body.multitask_strategy) 

    # 判断thread_id是否有效
    _validate_thread_id(thread_id)

    # 拒绝metadata携带凭证数据（checkpoint持久化时会落库）
    _reject_metadata_secrets(body)

    # PII 边界：在 normalize / 幂等比较 / run record 之前替换输入。mapping 只挂在
    # runtime.context 里，绝不进入 body.input、RunRecord.kwargs 或 checkpoint。
    pii_vault = _redact_run_input(
        body,
        app_config,
        vault_id=_pii_vault_id(thread_id, idempotency_key, body.input),
    )

    # 规范化stream_mode
    stream_modes = _normalize_stream_modes(body.stream_mode) 

    # 检查客户端配置
    switches = collect_client_switches(body)   

    # 检查模型白名单
    model_name = _check_model_allowed(switches.get("model_name"), app_config) 

    # 推理等级必须是该模型在 config.yaml 里公布的候选之一（和前端下拉同源）
    _check_reasoning_effort(switches.get("reasoning_effort"), model_name, app_config)

    # 判断是否有权限访问thread
    if not await _thread_access_allowed(ctx.thread_store, thread_id, caller, require_existing=require_existing_thread):
        raise ThreadNotFound(f"Thread {thread_id} not found") 
   
    # 获取agent工厂
    agent_factory = resolve_agent_factory(body.assistant_id) 

    # 规范化input
    graph_input = normalize_input(body.input, command=body.command, trusted=caller.is_internal) 

    # 整理metadata
    run_metadata = _build_run_metadata(body.metadata)         

    # 组装RunnableConfig
    config = build_run_config(thread_id, body.config, run_metadata, app_config=app_config) 

    # 选择checkpoint
    await apply_checkpoint(config, body, thread_id=thread_id, checkpointer=ctx.checkpointer)

    # 合并客户端配置
    merge_client_switches(config, switches)

    # 写入服务端权威值
    run_id = new_run_id()
    inject_server_owned(config, caller, run_id=run_id, trace_id=run_metadata[TRACE_METADATA_KEY], body_context=body.context) 
    if pii_vault is not None:
        config["context"][PII_VAULT_CONTEXT_KEY] = pii_vault

    # 准入
    scoped_key = scope_idempotency_key(caller, thread_id, idempotency_key)
    try:
        record = await services.run_manager.create_or_reject(                            # 13
            thread_id,
            body.assistant_id,
            run_id=run_id,
            on_disconnect=DisconnectMode(body.on_disconnect),
            multitask_strategy=body.multitask_strategy,
            metadata=run_metadata,
            kwargs=_persisted_kwargs(body),
            model_name=model_name,
            user_id=caller.user_id,
            idempotency_key=scoped_key,
        )
    except ConflictError as exc:
        raise ThreadBusy(str(exc)) from exc

    if record.run_id != run_id:                      # 命中幂等键：拿到的是已有的 run
        _ensure_same_admission(record, body)
        return RunLaunch(record=record, reused=True)

    # 挂worker创建run
    run_ctx = ctx.for_run(
        app_config=app_config, agent_factory=agent_factory, run_config=config,
        graph_input=graph_input, stream_modes=tuple(stream_modes),
    )
    worker = run_after_metadata(
        record, run_ctx, bridge=services.bridge, run_manager=services.run_manager,
        owner_user_id=caller.user_id, require_existing_thread=require_existing_thread,
    )
    try:
        record.task = asyncio.create_task(worker, name=f"run:{record.run_id}")          # 14
    except BaseException as exc:
        worker.close()
        await _abandon_unattached_run(services, record, exc)
        raise
    return RunLaunch(record=record, reused=False)

def _pii_vault_id(
    thread_id: str, idempotency_key: str | None, raw_input: Any
) -> str | None:
    """Stable handle namespace for idempotent replays; random for ordinary runs.

    The raw-input digest is part of the namespace so a reused Idempotency-Key with a
    changed sensitive value cannot compare equal after tokenization.
    """
    if not idempotency_key:
        return None
    serialized = json.dumps(raw_input, ensure_ascii=False, sort_keys=True, default=str)
    material = f"{thread_id}\0{idempotency_key}\0{serialized}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def _redact_run_input(
    body: RunCreateRequest,
    app_config: AppConfig,
    *,
    vault_id: str | None = None,
) -> PIIVault | None:
    """Redact the raw request input once, before it is persisted or normalized."""
    if not app_config.pii.enabled or not isinstance(body.input, dict):
        return None
    vault = PIIVault(vault_id=vault_id) if vault_id else PIIVault()
    body.input = redact_raw_input(body.input, PIIRedactor(vault))
    return vault


def strip_client_identity(body: RunCreateRequest) -> None:
    """就地删掉客户端声明的身份（M9 §2.1）。

    `metadata` / `config.configurable` / `config.context` / `context` 四处同一条规则。
    **不是"校验后使用"，是删掉**：唯一权威来源是服务端认证注入的
    `runtime.context.user_id`（`inject_server_owned` 写），
    所以这里删得越干净，下游就越不可能从 body 里再读一次。

    HTTP、IM、调度、嵌入四条入口都走 `start_run`，剥身份只有这一处。
    """
    config = body.config if isinstance(body.config, dict) else {}
    for bag in (body.metadata, config.get("configurable"), config.get("context"), body.context):
        if isinstance(bag, dict):
            for key in _CLIENT_IDENTITY_KEYS:
                bag.pop(key, None)


def _require_cancel_capability(caller: RunCaller, strategy: str) -> None:
    if strategy != "reject" and not caller.allows("runs:cancel"):
        raise RunPermissionDenied(f"multitask_strategy {strategy!r} requires 'runs:cancel' permission")

def _validate_thread_id(thread_id: str) -> None:
    try:
        validate_thread_id(thread_id)
    except ValueError as exc:
        raise RunRequestInvalid(str(exc)) from exc



def _reject_metadata_secrets(body: RunCreateRequest) -> None:
    config_metadata = (body.config or {}).get("metadata")

    for where, bag in (("metadata", body.metadata), ("config.metadata", config_metadata)):
        if isinstance(bag, dict) and (hit := _CREDDENTIAL_METADATA_KEYS & bag.keys()):
            raise RunRequestInvalid(f"{where} contains sensitive data: {hit}")

def _normalize_stream_modes(raw: str | list[str] | None) -> list[str]:
    try:
        return normalize_stream_modes(raw)
    except UnsupportedStreamModeError as exc:
        raise RunRequestInvalid(str(exc)) from exc

def _check_model_allowed(model_name: str | None, app_config: AppConfig) -> str| None:
    """白名单 = config.yaml 的 `models[].name`，或厂商目录里的 `<name>@<vendor-id>`。

    后者是 `/api/models` 从厂商 `/models` 拉到的新模型：它没有独立配置，
    复用 `<name>` 那条的 use/base_url/api_key，只把 vendor 侧模型名换掉。

    以前这里读 `app_config.model_config.allowed_models`：那是 Pydantic 的配置字典，
    根本没有这个属性——客户端一旦真传 `model_name` 就是 AttributeError 500。
    """
    if model_name is None:
        return None
    entry, vendor = split_model_name(model_name)
    if app_config.get_model_config(entry) is None:
        raise ModelNotAllowed(f"Model {model_name} is not allowed")
    if VENDOR_SEPARATOR in model_name:
        # `flash@` 这种空 vendor id 是坏输入，别悄悄当成裸模型名
        if not vendor or not _VENDOR_MODEL_ID.fullmatch(vendor):
            raise ModelNotAllowed(f"Model {model_name} is not allowed")
    return model_name


def _effective_model_name(model_name: str | None, app_config: AppConfig) -> str | None:
    """和 agent 装配同一套回退：请求 → agents.default_model → models[0]。"""

    if model_name is not None:
        return model_name
    if app_config.agents.default_model is not None:
        return app_config.agents.default_model
    return app_config.models[0].name if app_config.models else None


def _check_reasoning_effort(
    reasoning_effort: str | None, model_name: str | None, app_config: AppConfig
) -> str | None:
    """只接受目标模型公布的等级：配置里的候选，或厂商 profile 报出来的候选。"""

    if reasoning_effort is None:
        return None
    effective = _effective_model_name(model_name, app_config)
    entry, _vendor = split_model_name(effective) if effective else (None, None)
    model_config = app_config.get_model_config(entry) if entry else None
    if model_config is None:
        raise RunRequestInvalid(f"Cannot resolve reasoning effort for model {effective!r}")
    if not model_config.supports_reasoning_effort:
        raise RunRequestInvalid(
            f"Model {effective} does not support reasoning_effort"
        )
    published = model_config.reasoning_effort_ids()
    if published and reasoning_effort not in published:
        raise RunRequestInvalid(
            f"reasoning_effort {reasoning_effort!r} is not published by model {effective}; "
            f"allowed: {published}"
        )
    return reasoning_effort

async def _thread_access_allowed(thread_store, thread_id: str, caller: RunCaller, *, require_existing: bool) -> bool:
    if caller.user_id is None: #检查thread id是否存在
        return not require_existing or await thread_store.get(thread_id, user_id=None) is not None
    return await thread_store.check_access(thread_id, caller.user_id, require_existing=require_existing)

def resolve_agent_factory(assistant_id: str) -> AgentFactory:
    if assistant_id != "lead_agent":
        raise AssistantNotFound(f"Assistant {assistant_id} not found")
    from agentkit.agents.leader_agent import assemble_agent
    return assemble_agent

_COMMAND_DECISIONS = frozenset({"approve", "reject"})


def _normalize_resume_command(command: Any) -> Command:
    """Accept only the HITL resume shape this gateway exposes.

    LangGraph Command can also update state or jump to a node. Those are
    graph-control primitives, not client input, so the HTTP boundary accepts
    only {"resume": {"decisions": [...]}} and rebuilds the object itself.
    """

    if not isinstance(command, dict):
        raise RunRequestInvalid("command must be an object")
    if unknown := set(command) - {"resume"}:
        raise RunRequestInvalid(f"command only supports resume; got {sorted(unknown)}")
    resume = command.get("resume")
    if not isinstance(resume, dict):
        raise RunRequestInvalid("command.resume must be an object")
    if unknown := set(resume) - {"decisions"}:
        raise RunRequestInvalid(f"command.resume only supports decisions; got {sorted(unknown)}")
    decisions = resume.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise RunRequestInvalid("command.resume.decisions must be a non-empty list")

    cleaned: list[dict[str, str]] = []
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise RunRequestInvalid(f"command.resume.decisions[{index}] must be an object")
        if unknown := set(decision) - {"type", "message"}:
            raise RunRequestInvalid(
                f"command.resume.decisions[{index}] has unsupported keys: {sorted(unknown)}"
            )
        kind = decision.get("type")
        if kind not in _COMMAND_DECISIONS:
            raise RunRequestInvalid(
                f"command.resume.decisions[{index}].type must be approve or reject"
            )
        item = {"type": kind}
        message = decision.get("message")
        if message is not None:
            if not isinstance(message, str):
                raise RunRequestInvalid(
                    f"command.resume.decisions[{index}].message must be a string"
                )
            if len(message) > 2_000:
                raise RunRequestInvalid(
                    f"command.resume.decisions[{index}].message is too long"
                )
            item["message"] = message
        cleaned.append(item)
    return Command(resume={"decisions": cleaned})


def normalize_input(raw: dict|None, *, command: Any, trusted: bool) -> dict | Command:
    if command is not None:
        if raw:
            raise RunRequestInvalid("input and command are mutually exclusive")
        return _normalize_resume_command(command)

    if raw is None:
        return {}

    if not trusted and (extra := raw.keys() - _CLIENT_INPUT_CHANNELS):
        raise RunRequestInvalid(f"untrusted input contains extra channels: {extra}")

    messages = raw.get("messages")
    if messages is None:
        return dict(raw)

    if not isinstance(messages, list):
        raise RunRequestInvalid(f"messages must be a list, got {type(messages)}")

    converted = []
    for index, item in enumerate(messages):
        if isinstance(item, dict):
            strip_server_owned_message_keys(item, internal=trusted)
        try:
            [message] = convert_to_messages([item])
        except (ValueError, TypeError, NotImplementedError) as exc:
            raise RunRequestInvalid(f"Invalid message at input.messages[{index}]: {exc}") from exc
        if not isinstance(item, dict):
            message = _strip_message_object(message, internal=trusted)
        if not trusted:
            if message.type not in _CLIENT_MESSAGE_TYPES:
                raise RunRequestInvalid(f"input.messages[{index}]: role {message.type!r} is not accepted from clients")
            if message.id == "__user_profile__":
                message = message.model_copy(update={"id": None})
        converted.append(message)
    return {**raw, "messages": converted}


def strip_server_owned_message_keys(message: dict, *, internal: bool) -> None:
    """就地删除客户端伪造的服务端键。internal=True 时保留 original_user_content。"""
    kwargs = message.get("additional_kwargs")
    if not isinstance(kwargs, dict):
        return
    for key in _SERVER_OWNED_MESSAGE_KEYS:
        if internal and key == ORIGINAL_USER_CONTENT_KEY:
            continue
        kwargs.pop(key, None)


def _strip_message_object(message: BaseMessage, *, internal: bool) -> BaseMessage:
    carrier = {"additional_kwargs": dict(message.additional_kwargs)}
    strip_server_owned_message_keys(carrier, internal=internal)
    if carrier["additional_kwargs"] == message.additional_kwargs:
        return message
    return message.model_copy(update={"additional_kwargs": carrier["additional_kwargs"]})

def _build_run_metadata(raw: dict|None) -> dict:
    metadata = raw if isinstance(raw, dict) else {}
    metadata[TRACE_METADATA_KEY] = ensure_trace_id()
    return metadata

# 组装RunableConfig
def build_run_config(thread_id: str, request_config: dict | None, run_metadata: dict, *, app_config: AppConfig) -> dict:
    request_config = request_config or {}
    if unknown := request_config.keys() - _HANDLED_CONFIG_KEYS:
        raise RunRequestInvalid(f"unknown config keys: {unknown}")
    
    config = {key: request_config[key] for key in _PASSTHROUGH_CONFIG_KEYS if key in request_config}
    config["recursion_limit"] = _clamp_recursion_limit(request_config.get("recursion_limit"), app_config.max_recursion_limit)
    client_metadata = request_config.get("metadata") or {}
    config["metadata"] = {
        **{k: v for k, v in client_metadata.items() if k not in _RESERVED_METADATA_KEYS},
        **run_metadata,
    }

    config["configurable"] = {"thread_id": thread_id, "checkpoint_ns": ""}
    config["context"] = {"thread_id": thread_id}
    return config

def _clamp_recursion_limit(value: Any, ceiling: int) -> int:
    if value is None:
        return DEFAULT_RECURSION_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RunRequestInvalid("config.recursion_limit must be a positive integer")
    if value > ceiling:
        logger.warning("Clamped client recursion_limit %d to %d", value, ceiling)
    return min(value, ceiling)


async def apply_checkpoint(config: dict, body: RunCreateRequest, *, thread_id: str, checkpointer) -> None:
    checkpoint = body.checkpoint or {}
    checkpoint_id = checkpoint.get("checkpoint_id") or body.checkpoint_id
    if not checkpoint_id:
        return
    if checkpoint.get("thread_id") not in (None, thread_id):
        raise RunRequestInvalid("checkpoint.thread_id does not match the request thread_id")

    coords = {
        "thread_id": thread_id,
        "checkpoint_ns": str(checkpoint.get("checkpoint_ns") or ""),
        "checkpoint_id": str(checkpoint_id),
    }
    if (checkpoint_map := checkpoint.get("checkpoint_map")) is not None:
        coords["checkpoint_map"] = checkpoint_map
    if await checkpointer.aget_tuple({"configurable": coords}) is None:
        raise CheckpointNotFound(f"Checkpoint {checkpoint_id} not found")
    config["configurable"].update(coords)

def scope_idempotency_key(caller: RunCaller, thread_id: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        raise RunRequestInvalid("Idempotency-Key must not be blank")
    material = "\0".join((caller.kind, caller.user_id or "", thread_id, raw))   # raw 是任意字符串，用 \0 分隔
    return "run:" + hashlib.sha256(material.encode()).hexdigest()

def _ensure_same_admission(record: RunRecord, body: RunCreateRequest) -> None:
    stored = record.kwargs or {}
    if (
        stored.get("input") != body.input
        or stored.get("command") != body.command
        or record.assistant_id != body.assistant_id
    ):
        raise IdempotencyKeyReused("Idempotency-Key already used with a different request")

def redact_config_secrets(config: dict | None) -> dict | None:
    """Copy of the client config safe to store and echo. The live run config is untouched."""
    if config is None:
        return None
    return _redact_secrets(config)


def redact_context_secrets(context: dict | None) -> dict | None:
    """Same strip as ``redact_config_secrets``, for the top-level context bag."""
    if context is None:
        return None
    return _redact_secrets(context)


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_secrets(item)
            for key, item in value.items()
            if not _is_redacted_key(key)
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _is_redacted_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return (
        key == REQUEST_SECRETS_KEY
        or key == "trace_id"
        or key == TRACE_METADATA_KEY
        or key in REDACTED_CONTEXT_KEYS  # slash 源路径 / 技能策略决策 / 激活台账（M7 §3.6）
        or key.startswith("__")
    )


def _persisted_kwargs(body: RunCreateRequest) -> dict:
    return {
        "input": body.input,                              # 原始值：幂等比对和 run API 回显都用它
        "command": body.command,                          # resume 的幂等比对也要看决策
        "config": redact_config_secrets(body.config),
        "context": redact_context_secrets(body.context),
    }

async def _abandon_unattached_run(services: RunServices, record: RunRecord, exc: BaseException) -> None:
    await services.run_manager.fail_start_if_pending(record.run_id, error=f"Failed to attach run worker: {exc!r}")
    await services.bridge.publish_end(record.run_id)

def _validate_secrets(where: str, value: Any) -> dict[str, str]:
    """Request secrets stay a flat string map. They are copied so the body is not aliased."""
    if not isinstance(value, dict):
        raise RunRequestInvalid(f"{where}.secrets must be an object of strings")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RunRequestInvalid(f"{where}.secrets must be an object of strings")
        cleaned[key] = item
    return cleaned


def collect_client_switches(body: RunCreateRequest) -> dict[str, Any]:
    config = body.config or {}
    bags = (
        ("config.configurable", config.get("configurable")),
        ("config.context", config.get("context")),
        ("body.context", body.context),
    )

    switches: dict[str, Any] = {}
    for where, bag in bags:
        if bag is None:
            continue
        if not isinstance(bag, dict):
            raise RunRequestInvalid(f"{where} must be a dict, got {type(bag)}")
        for key, value in bag.items():
            if key.startswith("__") or key in _DROPPED_KEYS:
                continue
            if key in CLIENT_ASSEMBLY_KEYS:
                if not isinstance(value, CLIENT_ASSEMBLY_KEY_TYPES[key]):
                    raise RunRequestInvalid(f"{where}.{key} must be {CLIENT_ASSEMBLY_KEY_TYPES[key]}, got {type(value)}")
                switches[key] = value
            elif key == REQUEST_SECRETS_KEY:
                if where == "config.configurable":
                    raise RunRequestInvalid("secrets must go in context: configurable is persisted into checkpoints")
                switches[key] = _validate_secrets(where,value)
            else:
                raise RunRequestInvalid(f"{where}.{key} is not a supported run option")

    return switches

def merge_client_switches(config: dict, switches: dict[str, Any]) -> None:
    for key, value in switches.items():
        if key in CLIENT_ASSEMBLY_KEYS:
            config["configurable"][key] = value          # 工厂读这里
        config["context"][key] = value                   # 工具和中间件读 runtime.context

def inject_server_owned(config: dict, caller: RunCaller, *, run_id: str, trace_id: str, body_context: dict | None) -> None:
    context = config["context"]
    context.update(
        user_id=caller.user_id,
        user_role=caller.user_role,
        is_internal=caller.is_internal,
        run_id=run_id,
        trace_id=trace_id,
    )

    if not caller.is_internal or not body_context:
        return
    
    if body_context.get("non_interactive") is True:
        config["configurable"]["non_interactive"] = True
        context["non_interactive"] = True
    if isinstance(channel_user_id := body_context.get("channel_user_id"), str):
        context["channel_user_id"] = channel_user_id     # 只进 context

def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """字段顺序 event -> data -> id -> 空行，对齐 LangGraph Platform 的 wire 格式。"""
    frame = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n"
    if event_id:
        frame += f"id: {event_id}\n"
    return frame + "\n"

async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
    *,
    apply_on_disconnect: bool = True,
):
    subscription = bridge.subscribe(record.run_id, last_event_id=request.headers.get("Last-Event-ID"))
    try:
        async for entry in subscription:
            if await request.is_disconnected():
                break

            if isinstance(entry, StreamGap):
                yield format_sse("gap",{
                    "code": "stream_replay_gap",
                    "run_id": record.run_id,
                    "requested_event_id": entry.requested_event_id,
                    "earliest_available_event_id": entry.earliest_available_event_id,
                    "latest_available_event_id": entry.latest_available_event_id,
                    "recovery": "reload_durable_state",
                })
                return
            
            if entry is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None)
                return
            
            yield format_sse(entry.event, entry.data, event_id=entry.id or None)
    finally:
        try:
            await subscription.aclose()
        except Exception:
            logger.warning("Failed to close bridge subscription", exc_info=True)

        if apply_on_disconnect and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)

def sse_response(services: RunServices, launch: RunLaunch, request: Request, *, apply_on_disconnect: bool = True) -> StreamingResponse:
    record = launch.record
    return StreamingResponse(
        sse_consumer(
            services.bridge, record, request, services.run_manager, apply_on_disconnect=apply_on_disconnect
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Location": f"/api/threads/{record.thread_id}/runs/{record.run_id}",
        },
    )


async def delete_thread(services: RunServices, thread_id: str, caller: RunCaller) -> None:
    """Refuse the delete while a run is still pending or running, then drop the projection row."""
    active = await services.run_store.list_by_thread(
        thread_id,
        user_id=caller.user_id,
        limit=1,
        statuses=frozenset({RunStatus.pending, RunStatus.running}),
    )
    if active:
        raise ThreadBusy(f"Thread {thread_id} has an active run")
    row = await services.run_context.thread_store.get(thread_id, user_id=caller.user_id)
    if row is None:
        raise ThreadNotFound(f"Thread {thread_id} not found")
    await services.run_context.thread_store.delete(thread_id, user_id=caller.user_id)


@asynccontextmanager
async def reserve_checkpoint_write(services: RunServices, thread_id: str, *, user_id: str | None):
    """Hold the thread while a checkpoint write runs. An active operation becomes ThreadBusy."""
    from agentkit.runtime.runs.schemas import ThreadOperationKind

    try:
        async with services.run_manager.reserve_thread_operation(
            thread_id,
            kind=ThreadOperationKind.checkpoint_write,
            user_id=user_id,
        ):
            yield
    except ConflictError as exc:
        raise ThreadBusy(str(exc)) from exc


async def compact_thread_context(
    services: RunServices,
    thread_id: str,
    *,
    user_id: str | None,
    force: bool,
    keep: tuple | None,
    model_name: str | None,
    raise_on_failure: bool,
):
    """Run the shared summarizer and write one new checkpoint. None means there was nothing to compact."""
    del user_id, raise_on_failure
    from agentkit.agents.middlewares.summarization_middleware import (
        SummaryGenerationError,
        compact_thread_context as write_compacted_state,
        create_summarization_middleware,
    )
    from backend.app.gateway.errors import CompactionDisabled, SummarizationFailed

    middleware = create_summarization_middleware(keep=keep, run_model_name=model_name)
    if middleware is None:
        raise CompactionDisabled("Summarization is disabled")
    try:
        outcome = await write_compacted_state(
            services.run_context.checkpointer,
            thread_id,
            middleware=middleware,
            force=force,
        )
    except SummaryGenerationError as exc:
        raise SummarizationFailed("Failed to compact thread context") from exc
    if not outcome.compacted:
        return None
    return outcome


async def wait_for_run_completion(
    services: RunServices, record: RunRecord, request: Request, *, apply_on_disconnect: bool = True
) -> RunRecord:
    """阻塞到这条 run 的 END。返回从 store 重读的记录，不是内存里那份快照。"""
    cursor: str | None = None
    try:
        while True:
            ended = False
            subscription = services.bridge.subscribe(record.run_id, last_event_id=cursor)
            try:
                async for entry in subscription:
                    if await request.is_disconnected():           # ★ 心跳给了这个判断一个节拍
                        raise ClientDisconnected(f"Client disconnected while waiting for {record.run_id}")
                    if isinstance(entry, StreamGap):
                        cursor = entry.latest_available_event_id   # 从窗口尾部续订，不重放
                        break                                      # 换一条订阅继续等
                    if entry is HEARTBEAT_SENTINEL:
                        continue
                    if entry is END_SENTINEL:
                        ended = True
                        break
                    cursor = entry.id                               # 只推进游标，不解析载荷
            finally:
                try:
                    await subscription.aclose()                    # 同 §8.1：退出循环不会替你关
                except Exception:
                    logger.warning("Failed to close bridge subscription", exc_info=True)
            if ended:
                break
    except ClientDisconnected:
        if apply_on_disconnect and record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await services.run_manager.cancel(record.run_id)
        raise                                          # 路由不要用当前 checkpoint 拼一个 200
    final = await services.run_manager.get(record.run_id, user_id=record.user_id)
    return final or record