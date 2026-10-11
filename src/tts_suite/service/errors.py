"""What the service answers when a request is wrong, apart from the errors of the suite."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A request the service refuses: bad input (400), no or a wrong token (401)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "detail": self.detail}


def bad_request(detail: str) -> ServiceError:
    return ServiceError(400, "bad_request", detail)
