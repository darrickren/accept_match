"""Error contract (plan section 6): every error body is {request_id, code, message, retryable, detail}.

Unknown routes keep FastAPI's default {"detail": "Not Found"} on purpose: the
frontend reads that as "endpoint not implemented yet", distinct from a missing
record (404 with a contract body).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

LOG = logging.getLogger("acceptmatch.backend")
CURRENT_REQUEST: contextvars.ContextVar[str] = contextvars.ContextVar("acceptmatch_request_id", default="-")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False, detail: Any = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.retryable, self.detail = status, code, message, retryable, detail


def not_found(code: str, message: str) -> ApiError:
    return ApiError(404, code, message)


def conflict(code: str, message: str, detail: Any = None, retryable: bool = False) -> ApiError:
    return ApiError(409, code, message, retryable=retryable, detail=detail)


def new_request_id() -> str:
    return "req-" + uuid.uuid4().hex[:16]


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = new_request_id()
        request.state.request_id = rid
    return rid


def body(rid: str, code: str, message: str, retryable: bool = False, detail: Any = None) -> dict:
    return {"request_id": rid, "code": code, "message": message, "retryable": retryable, "detail": detail}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(body(request_id(request), exc.code, exc.message, exc.retryable, exc.detail),
                            status_code=exc.status, headers={"X-Request-ID": request_id(request)})

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Field locations and messages only: never echo input values (they may contain passwords).
        detail = [{"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", ""))[:200]}
                  for err in exc.errors()][:10]
        return JSONResponse(body(request_id(request), "VALIDATION_ERROR", "Some fields are missing or invalid.",
                                 False, detail), status_code=422, headers={"X-Request-ID": request_id(request)})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        LOG.exception("unhandled error on %s %s (%s)", request.method, request.url.path, request_id(request))
        return JSONResponse(body(request_id(request), "INTERNAL", "Unexpected server error.", True, None),
                            status_code=500, headers={"X-Request-ID": request_id(request)})


def describe(exc: Optional[BaseException]) -> str:
    return type(exc).__name__ if exc else ""
