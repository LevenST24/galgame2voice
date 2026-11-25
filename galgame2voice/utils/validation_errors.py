"""422 responses that survive request bodies containing Infinity or NaN.

Python's JSON parser accepts Infinity, NaN and numbers such as 1e400 (which become
infinity). When such a value fails validation, FastAPI's default 422 body echoes it
back as ``input``, and Starlette's JSONResponse refuses non-finite floats, so the
client got a 500 instead of the validation error.
"""

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # "inf", "-inf" or "nan"
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_json(item) for item in value]
    return value


async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": _finite_json(jsonable_encoder(exc.errors()))},
    )


def register_validation_error_handler(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
