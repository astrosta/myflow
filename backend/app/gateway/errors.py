"""Domain exceptions and validation failures share one error body."""

from __future__ import annotations

import logging
from typing import ClassVar
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentkit.persistence.base import InvalidCursor
from agentkit.persistence.json_compat import InvalidMetadataFilterError
from agentkit.persistence.thread_meta.base import ThreadOwnershipConflictError
from backend.app.gateway.services import StartRunError

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
    ):
        app.add_exception_handler(exc_type, _handle_coded_error)
    for exc_type in _PERSISTENCE:
        app.add_exception_handler(exc_type, _handle_persistence_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected)


def _trace_id(request: Request) -> str:
    current = getattr(request.state, "trace_id", None)
    if isinstance(current, str) and current:
        return current
    generated = uuid4().hex
    request.state.trace_id = generated
    return generated


def _error_body(request: Request, *, detail: str, code: str, errors: list[dict] | None = None) -> dict:
    body: dict = {"detail": detail, "code": code, "trace_id": _trace_id(request)}
    if errors:
        body["errors"] = errors
    return body


async def _handle_coded_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(request, detail=str(exc), code=exc.code),
    )


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
