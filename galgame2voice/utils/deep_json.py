"""Requests whose body is nested too deeply get a Chinese 400, on every platform.

Depending on the platform's stack and Python version, a body nested a few thousand
levels deep either fails in the JSON parser (FastAPI answers an English 400, "There
was an error parsing the body") or parses and then overflows the encoder that echoes
it back in a 422 (a 500). Both now give the same message.
"""

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

NESTED_TOO_DEEP_MESSAGE = "请求内容的层级太深（嵌套过多），无法处理。请检查发送的数据。"


def nested_too_deep_response() -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": NESTED_TOO_DEEP_MESSAGE})


async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 400 and isinstance(exc.__cause__, RecursionError):
        return nested_too_deep_response()
    return await http_exception_handler(request, exc)


def register_deep_json_handler(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
