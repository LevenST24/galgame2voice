"""Turn ids too big for SQLite into a plain "not found".

SQLite stores integers in 64 bits. A request that carries a larger id, in the
path or the body, makes the database driver raise OverflowError before the
query runs, which used to surface as a 500. No row can have such an id, so the
honest answer is 404.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_SQLITE_OVERFLOW_MESSAGE = "too large to convert to SQLite INTEGER"
NOT_FOUND_DETAIL = "找不到对应的记录：编号超出范围。请刷新页面后重试。"


async def sqlite_overflow_handler(request: Request, exc: OverflowError) -> JSONResponse:
    if _SQLITE_OVERFLOW_MESSAGE not in str(exc):
        # Any other overflow is a real bug; let the global handler report it.
        raise exc
    return JSONResponse(status_code=404, content={"detail": NOT_FOUND_DETAIL})


def register_sqlite_overflow_handler(app: FastAPI) -> None:
    app.add_exception_handler(OverflowError, sqlite_overflow_handler)
