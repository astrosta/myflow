"""Domain exceptions and validation failures share one error body."""

from __future__ import annotations

import logging
from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentkit.authz import ADMIN_ROLE
from agentkit.contextforge.models import (
    ContextForgeAuthError,
    ContextForgeConflict,
    ContextForgeError,
    ContextForgeNotFound,
    ContextForgeProtocolError,
    ContextForgeTimeout,
    ContextForgeUnavailable,
    ContextForgeValidationError,
)
from agentkit.knowledge.errors import KnowledgeError
from agentkit.persistence.base import InvalidCursor
from agentkit.persistence.json_compat import InvalidMetadataFilterError
from agentkit.persistence.thread_meta.base import ThreadOwnershipConflictError
from backend.app.gateway.auth.errors import (
    AccountDeleted,
    AccountDisabled,
    AccountNotFound,
    AdminAlreadyExists,
    AuthUnavailable,
    CannotModifySelf,
    ChannelNotBound,
    CsrfFailed,
    InvalidCredentials,
    InvalidEmail,
    InvalidPassword,
    InvalidScope,
    InvalidUsername,
    LastAdmin,
    LoginRateLimited,
    OriginNotAllowed,
    PasswordTooLong,
    PasswordTooShort,
    RegistrationClosed,
    SessionRequired,
    SetupRequired,
    TooManyPats,
    Unauthenticated,
    UnknownOwner,
    UserAlreadyExists,
    UserNotFound,
    UsernameTaken,
    WrongPassword,
)
from backend.app.gateway.auth.audit import log_admin_cross_user_denied
from backend.app.gateway.error_response import error_body, trace_id_of
from backend.app.gateway.services import RunNotFound, StartRunError, ThreadNotFound

#: M9 §2.12 / §2.13 / §2.15 的域异常。全部自带 `status_code` + `code`。
_M9_ERRORS = (
    Unauthenticated,
    AuthUnavailable,
    AccountNotFound,
    InvalidUsername,
    UsernameTaken,
    InvalidCredentials,
    WrongPassword,
    AccountDisabled,
    AccountDeleted,
    InvalidEmail,
    InvalidPassword,
    PasswordTooShort,
    PasswordTooLong,
    LoginRateLimited,
    AdminAlreadyExists,
    RegistrationClosed,
    SessionRequired,
    SetupRequired,
    UnknownOwner,
    ChannelNotBound,
    LastAdmin,
    CannotModifySelf,
    UserNotFound,
    UserAlreadyExists,
    TooManyPats,
    InvalidScope,
    OriginNotAllowed,
    CsrfFailed,
)

logger = logging.getLogger(__name__)

class ModelNotFound(Exception):
    """The model catalog has no entry with this name."""

    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "model_not_found"


class UploadInvalid(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_upload"


class UploadTooLarge(Exception):
    status_code: ClassVar[int] = 413
    code: ClassVar[str] = "upload_too_large"


class UploadLimitReached(Exception):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "upload_limit_reached"


class UploadNotFound(Exception):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "upload_not_found"


class ArtifactPathInvalid(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_artifact_path"


class ArtifactNotFound(Exception):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "artifact_not_found"


class InvalidStateUpdate(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_state_update"


class CompactionDisabled(Exception):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "compaction_disabled"


class SummarizationFailed(Exception):
    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "summarization_failed"


class McpServerNotFound(Exception):
    """PATCH /api/mcp/config 的 name 不在 extensions_config.json 里（M7 §2.4）。"""

    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "mcp_server_not_found"


class ContextForgeDisabled(Exception):
    """The contextforge block is disabled in this deployment."""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "contextforge_disabled"


class InvalidToolHeaderAPI(Exception):
    """A masked header had no stored value to restore."""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_tool_header"


class SkillNotFound(Exception):
    """技能不存在、已被删除，或不属于当前用户。"""

    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "skill_not_found"


class SkillRuntimeUnavailable(Exception):
    """git 来源的技能运行时没装上：需要权威目录的请求必须失败，不能假装成功。"""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "skill_runtime_unavailable"


class SkillScannerUnavailable(Exception):
    """cisco-ai-skill-scanner 没有安装或无法导入。"""

    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "skill_scanner_unavailable"


class SkillPackageInvalid(Exception):
    """A staging path or package failed validation (Git catalog author API)."""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_skill_package"


class SkillConflictAPI(Exception):
    """A quota, scan gate, or version precondition blocked a write."""

    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "skill_conflict"


class StdioCommandRejected(Exception):
    """API 注册/启用的 stdio command 不在允许列表（M7 §5.7）。

    detail 里只说允许的裸名字集合，不回显被拒绝的命令行。
    """

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "stdio_command_rejected"


class InvalidExtensionsConfig(Exception):
    """打码串没有原值可还原，或写入校验失败（且不是命令允许列表）。"""

    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_extensions_config"


class MemoryConflictAPI(Exception):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "memory_conflict"


class ProfileConflictAPI(Exception):
    status_code: ClassVar[int] = 409
    code: ClassVar[str] = "profile_conflict"


class InvalidMemory(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_memory"


class InvalidMemoryProfile(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_memory_profile"


class InvalidMemoryURIAPI(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_memory_uri"


class MemoryRequestInvalid(Exception):
    status_code: ClassVar[int] = 422
    code: ClassVar[str] = "invalid_memory_request"


class MemoryRuntimeUnavailable(Exception):
    status_code: ClassVar[int] = 503
    code: ClassVar[str] = "memory_runtime_unavailable"


class MemoryNotFoundAPI(Exception):
    status_code: ClassVar[int] = 404
    code: ClassVar[str] = "memory_not_found"


class MemoryKindUnsupported(Exception):
    status_code: ClassVar[int] = 501
    code: ClassVar[str] = "memory_kind_unsupported"


class MemoryCorrupted(Exception):
    """Fixed detail: a corrupt store path must not leak into the response."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "memory_corrupted"

    def __init__(self, detail: str = "Memory data is corrupt") -> None:
        super().__init__(detail)


_PERSISTENCE: dict[type[BaseException], tuple[int, str]] = {
    ThreadOwnershipConflictError: (409, "thread_ownership_conflict"),
    InvalidMetadataFilterError: (400, "invalid_metadata_filter"),
    InvalidCursor: (422, "invalid_cursor"),
}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StartRunError, _handle_coded_error)
    app.add_exception_handler(ModelNotFound, _handle_coded_error)
    for exc_type in (
        UploadInvalid,
        UploadTooLarge,
        UploadLimitReached,
        UploadNotFound,
        ArtifactPathInvalid,
        ArtifactNotFound,
        InvalidStateUpdate,
        CompactionDisabled,
        SummarizationFailed,
        McpServerNotFound,
        ContextForgeDisabled,
        InvalidToolHeaderAPI,
        ContextForgeError,
        ContextForgeAuthError,
        ContextForgeConflict,
        ContextForgeNotFound,
        ContextForgeProtocolError,
        ContextForgeTimeout,
        ContextForgeUnavailable,
        ContextForgeValidationError,
        SkillNotFound,
        SkillRuntimeUnavailable,
        SkillScannerUnavailable,
        SkillPackageInvalid,
        SkillConflictAPI,
        StdioCommandRejected,
        InvalidExtensionsConfig,
        MemoryConflictAPI,
        ProfileConflictAPI,
        InvalidMemory,
        InvalidMemoryProfile,
        InvalidMemoryURIAPI,
        MemoryRequestInvalid,
        MemoryNotFoundAPI,
        MemoryRuntimeUnavailable,
        MemoryKindUnsupported,
        MemoryCorrupted,
        KnowledgeError,
        *_M9_ERRORS,
    ):
        app.add_exception_handler(exc_type, _handle_coded_error)
    for exc_type in _PERSISTENCE:
        app.add_exception_handler(exc_type, _handle_persistence_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected)


def _trace_id(request: Request) -> str:
    return trace_id_of(request)


def _error_body(request: Request, *, detail: str, code: str, errors: list[dict] | None = None) -> dict:
    """中间件（`error_response.coded_error_response`）共用同一个函数，响应体不会漂移。"""
    return error_body(request, detail=detail, code=code, errors=errors)


#: "别人的资源"与"不存在的资源"在**响应**上完全一样（都 404，不泄露存在性），
#: 只有 admin 会因此多一条审计：那正是合规要看的东西（M9 §2.16）。
_CROSS_USER_RESOURCES: tuple[tuple[type[BaseException], str], ...] = (
    (ThreadNotFound, "thread"),
    (RunNotFound, "run"),
)


def _audit_cross_user_denial(request: Request, exc: BaseException) -> None:
    """在**唯一**的 404 出口记账。

    写在每个路由的 `raise` 旁边迟早会漏掉一处，而漏掉的表现是"审计日志缺行"——
    没有报错、没有指标，只有事后翻日志时才发现少了一条。
    """
    resource = next(
        (name for cls, name in _CROSS_USER_RESOURCES if isinstance(exc, cls)), None
    )
    if resource is None:
        return
    auth = getattr(request.state, "auth", None)
    if getattr(auth, "role", None) != ADMIN_ROLE:
        return
    log_admin_cross_user_denied(
        user_id=getattr(auth, "user_id", None),
        resource=resource,
        resource_id=request.url.path,
    )


async def _handle_coded_error(request: Request, exc: Exception) -> JSONResponse:
    _audit_cross_user_denial(request, exc)
    response = JSONResponse(
        status_code=exc.status_code,
        content=_error_body(request, detail=str(exc), code=exc.code),
    )
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, int) and retry_after > 0:
        # 429 带 `Retry-After`：前端不用从 detail 里抠秒数，中间层也能照着退避。
        response.headers["Retry-After"] = str(retry_after)
    return response


async def _handle_persistence_error(request: Request, exc: Exception) -> JSONResponse:
    for cls in type(exc).__mro__:
        if cls in _PERSISTENCE:
            status, code = _PERSISTENCE[cls]
            return JSONResponse(status_code=status, content=_error_body(request, detail=str(exc), code=code))
    raise RuntimeError(f"{type(exc).__name__} was registered without a mapping")


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body(
            request,
            detail="Request validation failed",
            code="invalid_request",
            errors=[{"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in exc.errors()],
        ),
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    body = _error_body(request, detail="Internal server error", code="internal_error")
    response = JSONResponse(status_code=500, content=body)
    # Starlette's outermost server-error middleware sends this response itself,
    # so the trace middleware never gets to append the header.
    if body["trace_id"]:
        response.headers["X-Trace-Id"] = body["trace_id"]
    return response
