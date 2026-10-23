"""Error envelope shared by API handlers (FS-00 extends with domain codes)."""

from __future__ import annotations

from fastapi import HTTPException


class AppError(HTTPException):
    """Structured application error with a stable machine-readable code."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message, "details": details or {}})
        self.code = code
