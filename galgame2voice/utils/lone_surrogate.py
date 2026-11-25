"""Turn lone UTF-16 surrogates in a request into a 422.

JSON allows escapes such as "\\ud800" that decode to half of a surrogate pair.
Python keeps that half in the string, but it cannot be written as UTF-8, so the
request failed with a 500 as soon as the string reached the database or was
echoed back in a validation error. Such text can only come from a broken client,
so the honest answer is a request error. A proper pair, such as an emoji written
as two escapes, decodes to one character and is unaffected.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

INVALID_TEXT_DETAIL = "请求里含有无效字符（不成对的 Unicode 代理字符），请删除后重试。"


def is_lone_surrogate_error(exc: UnicodeEncodeError) -> bool:
    return exc.encoding.replace("-", "").lower() == "utf8" and exc.reason == "surrogates not allowed"


async def lone_surrogate_handler(request: Request, exc: UnicodeEncodeError) -> JSONResponse:
    if not is_lone_surrogate_error(exc):
        # Any other encoding failure is a real bug; let the global handler report it.
        raise exc
    return JSONResponse(status_code=422, content={"detail": INVALID_TEXT_DETAIL})


def register_lone_surrogate_handler(app: FastAPI) -> None:
    app.add_exception_handler(UnicodeEncodeError, lone_surrogate_handler)
