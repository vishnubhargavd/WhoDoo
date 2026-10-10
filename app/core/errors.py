from typing import Any


class AppError(Exception):
    """An expected failure, rendered as {"error": {"code", "message", "details"}}."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str) -> AppError:
    return AppError(404, "not_found", f"{what} not found")


def forbidden(message: str = "You do not have permission to do this") -> AppError:
    return AppError(403, "forbidden", message)


def conflict(code: str, message: str, details: Any = None) -> AppError:
    return AppError(409, code, message, details)


def bad_request(code: str, message: str, details: Any = None) -> AppError:
    return AppError(400, code, message, details)
