"""错误响应的唯一构造点：路由异常处理器与**中间件**共用。

Starlette 的中间件栈是 `ServerErrorMiddleware → 用户中间件 → ExceptionMiddleware → router`，
所以**用户中间件里抛出的异常不会**走 `app.add_exception_handler`——
它会被最外层的 ServerErrorMiddleware 变成 500。AuthMiddleware / CSRFMiddleware
因此直接返回 `coded_error_response(...)`，而不是 raise。

错误体形状与 M3 一致：`{detail, code, trace_id}`。
"""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse


class CodedError(Exception):
    """带 `status_code` / `code` 的域异常。子类只覆盖这两个类属性。"""

    status_code: ClassVar[int] = 400
    code: ClassVar[str] = "bad_request"


def trace_id_of(request: Request) -> str:
    """TraceMiddleware 没跑到时兜底生成一个，错误体里**永远不许**是 None。"""
    current = getattr(request.state, "trace_id", None)
    if isinstance(current, str) and current:
        return current
    generated = uuid4().hex
    request.state.trace_id = generated
    return generated


def error_body(request: Request, *, detail: str, code: str, errors: list[dict] | None = None) -> dict:
    body: dict = {"detail": detail, "code": code, "trace_id": trace_id_of(request)}
    if errors:
        body["errors"] = errors
    return body


def coded_error_response(request: Request, exc: CodedError) -> JSONResponse:
    """中间件专用。异常处理器走同一条 `error_body`，两种入口的响应体不会漂移。"""
    detail = str(exc) or exc.code
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(request, detail=detail, code=exc.code),
    )
