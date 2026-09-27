"""
start_run、sse_comsumer、format_sse
"""
from pydantic.json_schema import ConfigDict
from backend.app.gateway.run_models import RunCreateRequest, RunCaller, RunLauch
from backend.app.gateway.deps import RunServices
from fastapi import HTTPException
from agentkit.trace_context import TRACE_METADATA_KEY, ensure_trace_id
from agentkit.config.app_config import get_app_config, AppConfig
from agentkit.agents.leader_agent.runtime_keys import (
    CHECKPOINT_COORDINATE_KEYS, CLIENT_ASSEMBLY_KEY_TYPES, CLIENT_ASSEMBLY_KEYS,
    CLIENT_IDENTITY_KEYS, CONTEXT_ONLY_KEYS, SERVER_OWNED_KEYS,
)
from agentkit.utils.ids import validate_thread_id
from agentkit.runtime.stream_modes import normalize_stream_modes, UnsupportedStreamModeError
from typing import ClassVar, Any
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import convert_to_messages
import hashlib
import logging
import asyncio

logger = logging.getLogger(__name__)
_CLIENT_INPUT_CHANNELS = frozenset({"messages"})
_CLIENT_MESSAGE_TYPES = frozenset({"human"})
REQUEST_SECRETS_KEY = "secrets" 

# user_role / authz_attributes：M9 的 Principal 从 runtime.context 读它们，客户端不能预置
_DROPPED_KEYS = (
    SERVER_OWNED_KEYS | CLIENT_IDENTITY_KEYS | CHECKPOINT_COORDINATE_KEYS | CONTEXT_ONLY_KEYS
    | {"user_role", "authz_attributes"}
)

_CREDDENTIAL_METADATA_KEYS = frozenset({"auth_token"})

# 客户端不能伪造的 additional_kwargs 键。M3 为空，但剥离函数和调用点现在就接好；
# M6 并入 PROVENANCE_KEYS | {ORIGINAL_USER_CONTENT_KEY, "hide_from_ui", ...}（M6 §4.1、§4.7）。
SERVER_OWNED_MESSAGE_KEYS: frozenset[str] = frozenset()

#保留的metadata key，不放到RunConfigable的metadata里面，TRACE_METADATA_KEY重新生成
_RESERVED_METADATA_KEYS = CLIENT_IDENTITY_KEYS | {TRACE_METADATA_KEY}

# 默认循环次数
DEFAULT_RECURSION_LIMIT = 100
_PASSTHROUGH_CONFIG_KEYS = frozenset({"tags", "run_name"})
_HANDLED_CONFIG_KEYS = _PASSTHROUGH_CONFIG_KEYS | {"recursion_limit", "metadata", "configurable", "context"}

# thread metadata存储超时时间
THREAD_METADATA_SETUP_TIMEOUT_SECONDS = 5.0

class StartRunError(Exception):
    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"

class RunPermissionDenied(StartRunError): status_code, code = 403, "permission_denied"
class RunRequestInvalid(StartRunError):     status_code, code = 422, "invalid_run_request"
class ModelNotAllowed(StartRunError):       status_code, code = 400, "model_not_allowed"
class ThreadNotFound(StartRunError):        status_code, code = 404, "thread_not_found"
class AssistantNotFound(StartRunError):     status_code, code = 404, "assistant_not_found"
class CheckpointNotFound(StartRunError):    status_code, code = 404, "checkpoint_not_found"
class ThreadBusy(StartRunError):            status_code, code = 409, "thread_busy"
class IdempotencyKeyReused(StartRunError):  status_code, code = 409, "idempotency_key_reused"

async def start_run(
    body: RunCreateRequest,
    *,
    thread_id: str,
    caller: RunCaller,
    services: RunServices,
    idempotency_key: str | None = None,
    require_existing_thread: bool = False,
) -> RunLauch:
    """
    """
    app_config = get_app_config()
    ctx = services.run_context

    # 判断如果要取消旧run，是否有权限
    _require_cancel_capability(caller, body.multitask_strategy) 

    # 判断thread_id是否有效
    _validate_thread_id(thread_id)

    # 拒绝metadata携带凭证数据（checkpoint持久化时会落库）
    _reject_metadata_secrets(body)

    # 规范化stream_mode
    stream_modes = _normalize_stream_modes(body.stream_mode) 

    # 检查客户端配置
    switches = collect_client_switches(body)   

    # 检查模型白名单
    model_name = _check_model_allowed(switches.get("model_name"), app_config) 

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
    if model_name is None:
        return None
    if model_name in app_config.model_config.allowed_models:
        return model_name
    raise ModelNotAllowed(f"Model {model_name} is not allowed")

async def _thread_access_allowed(thread_store, thread_id: str, caller: RunCaller, *, require_existing: bool) -> bool:
    if caller.user_id is None: #检查thread id是否存在
        return not require_existing or await thread_store.exists(thread_id, user_id=None) is not None
    return await thread_store.check_access(thread_id, caller.user_id, require_existing=require_existing)

def resolve_agent_factory(assistant_id: str ):
    if assistant_id != "lead_agent":
        raise AssistantNotFound(f"Assistant {assistant_id} not found")
    from agentkit.agents.leader_agent.agent import assemble_agent
    return assemble_agent

def normalize_input(raw: dict|None, *, command: Any, trusted: bool) -> dict:
    if command is not None:
        raise RunRequestInvalid(f"command {command!r} not allowed")

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
        try:
            [message] = convert_to_messages([item])
        except (ValueError, TypeError, NotImplementedError) as exc:
            raise RunRequestInvalid(f"Invalid message at input.messages[{index}]: {exc}") from exc
        if not trusted:
            if message.type not in _CLIENT_MESSAGE_TYPES:
                raise RunRequestInvalid(f"input.messages[{index}]: role {message.type!r} is not accepted from clients")
            message = _strip_server_owned_keys(message)
        converted.append(message)
    return {**raw, "messages": converted}


def _strip_server_owned_keys(message: BaseMessage) -> BaseMessage:
    kept = {k: v for k, v in message.additional_kwargs.items() if k not in SERVER_OWNED_MESSAGE_KEYS}
    if kept == message.additional_kwargs:
        return message
    return message.model_copy(update={"additional_kwargs": kept})

def _build_run_metadata(raw: dict|None) -> dict:
    metadata = {k:v for k,v in raw.items() if k not in _RESERVED_METADATA_KEYS}
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
    if stored.get("input") != body.input or record.assistant_id != body.assistant_id:
        raise IdempotencyKeyReused("Idempotency-Key already used with a different request")

def _persisted_kwargs(body: RunCreateRequest) -> dict:
    return {
        "input": body.input,                              # 原始值：幂等比对和 run API 回显都用它
        "config": redact_config_secrets(body.config),
        "context": redact_context_secrets(body.context),
    }

async def _abandon_unattached_run(services: RunServices, record: RunRecord, exc: BaseException) -> None:
    await services.run_manager.fail_start_if_pending(record.run_id, error=f"Failed to attach run worker: {exc!r}")
    await services.bridge.publish_end(record.run_id)


async def run_after_metadata(
    record: RunRecord,
    run_ctx: RunContext,
    *,
    bridge: StreamBridge,
    run_manager: RunManager,
    owner_user_id: str | None,
    require_existing_thread: bool,
) -> None:
    """worker task 的入口：尽力补 thread 行，然后把控制权交给 run_agent"""
    interrupted: asyncio.CancelledError | None = None
    try:
        failure = await _ensure_thread_metadata_bounded(
            record, run_ctx.thread_store, owner_user_id=owner_user_id, require_existing=require_existing_thread,
        )
    except asyncio.CancelledError as exc:
        interrupted, failure = exc, None
        record.abort_event.set()

    if failure is not None:
        if require_existing_thread:
            await run_manager.fail_start_if_pending(record.run_id, error=f"Thread verification failed: {failure!r}")
        else:
            logger.warning("Failed to ensure thread_meta for %s (non-fatal): %s", record.run_id, failure)
    
    await run_agent(bridge, run_manager, record, run_ctx)
    if interrupted is not None:
        raise interrupted
    
async def _ensure_thread_metadata_bounded(record, thread_store, *, owner_user_id, require_existing) -> BaseExecption | None:
    """最多等 5 秒，abort 一到就放手。返回 None 表示就绪或已被 abort；否则返回失败原因。"""
    setup = asyncio.create_task(
        _ensure_thread_metadata(thread_store, record, owner_user_id=owner_user_id, require_existing=require_existing)
    )
    aborted = asyncio.create_task(record.abort_event.wait())
    try:
        done, _ =await asyncio.wait(
            {setup, aborted}, timeout=THREAD_METADATA_SETUP_TIMEOUT_SECONDS, return_when = asyncio.FIRST_COMPLETED
        )
    finally:
        aborted.cancel()
        if not setup.done():
            setup.cancel()
            setup.add_done_callback(_log_detached_setup_result)
    
    if setup in done:
        return None if setup.cancelled() else setup.exception()
    if aborted in done:
        return None
    return TimeoutError(f"thread metadate setup exceeded {THREAD_METADATA_SETUP_TIMEOUT_SECONDS} seconds")

async def _ensure_thread_metadata(thread_store, record, *, owner_user_id, require_existing) -> BaseException | None:
    if await thread_store.get_metadata(record.run_id) is not None:
        return
    
    if require_existing:
        return LookupError(f"Thread {record.thread_id} was deleted during run admission")
    
    await thread_store.create(
        record.thread_id,
        assistant_id=record.assistant_id,
        metadata={k:v for k, v in record.metadata.items() if k not in _RESERVED_METADATA_KEYS},
    )

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
