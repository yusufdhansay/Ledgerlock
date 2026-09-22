"""Error codes, the application exception type, and response shaping.

DESIGN.md requires one error envelope everywhere:

    {"error": {"code": "...", "message": "..."}}

RULES.md requires the codes to be *distinguishable*, so a test or a load
test report can tell "this request was correctly rejected for
insufficient funds" apart from "this request 500'd". That distinction is
the difference between a load test that proves the overdraft guarantee
held and one that just proves the server stayed up.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging_config import get_logger

logger = get_logger(__name__)


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes.

    These are part of the API contract. Renaming one is a breaking change.
    """

    # ---- Request / routing -------------------------------------------
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # ---- Authentication / authorisation ------------------------------
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    TOKEN_REVOKED = "TOKEN_REVOKED"  # noqa: S105 - error code, not a secret
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105 - error code, not a secret
    FORBIDDEN = "FORBIDDEN"

    # ---- Accounts ----------------------------------------------------
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"

    # ---- Transactions / ledger ---------------------------------------
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    MALFORMED_IDEMPOTENCY_KEY = "MALFORMED_IDEMPOTENCY_KEY"
    TRANSACTION_NOT_FOUND = "TRANSACTION_NOT_FOUND"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    SAME_ACCOUNT_TRANSFER = "SAME_ACCOUNT_TRANSFER"
    LEDGER_IMMUTABLE = "LEDGER_IMMUTABLE"
    WRITE_CONFLICT = "WRITE_CONFLICT"


class LedgerlockError(Exception):
    """Base class for every error this application raises deliberately.

    Carries the HTTP status and the machine-readable code together so a
    route never has to remember which status pairs with which code.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: ErrorCode = ErrorCode.MALFORMED_REQUEST
    message: str = "Request could not be processed."

    def __init__(
        self,
        message: str | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        # Context is for structured logs only; it is never returned to the
        # client, so it can safely hold internal identifiers.
        self.context = context or {}
        super().__init__(self.message)


# ---------------------------------------------------------------------
# Authentication / authorisation
# ---------------------------------------------------------------------


class InvalidCredentialsError(LedgerlockError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.INVALID_CREDENTIALS
    # Deliberately does not say whether the email exists: that would turn
    # the login endpoint into an account enumeration oracle.
    message = "Email or password is incorrect."


class EmailAlreadyRegisteredError(LedgerlockError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.EMAIL_ALREADY_REGISTERED
    message = "An account with this email already exists."


class UnauthenticatedError(LedgerlockError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHENTICATED
    message = "Authentication required."


class TokenRevokedError(LedgerlockError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.TOKEN_REVOKED
    message = "This token has been revoked. Log in again."


class TokenExpiredError(LedgerlockError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.TOKEN_EXPIRED
    message = "This token has expired. Log in again."


class ForbiddenError(LedgerlockError):
    status_code = status.HTTP_403_FORBIDDEN
    code = ErrorCode.FORBIDDEN
    message = "You do not have access to this resource."


# ---------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------


class AccountNotFoundError(LedgerlockError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.ACCOUNT_NOT_FOUND
    message = "Account not found."


class AccountNotActiveError(LedgerlockError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.ACCOUNT_NOT_ACTIVE
    message = "Account is not ACTIVE and cannot take part in a transaction."


class UnsupportedCurrencyError(LedgerlockError):
    """Raised for a currency this deployment has no SYSTEM account for.

    The currency list is a whitelist because every currency needs its own
    SYSTEM boundary account, so accepting arbitrary codes would mean an
    unbounded number of them.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.UNSUPPORTED_CURRENCY
    message = "That currency is not supported by this deployment."


# ---------------------------------------------------------------------
# Transactions / ledger
# ---------------------------------------------------------------------


class InsufficientFundsError(LedgerlockError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.INSUFFICIENT_FUNDS
    message = "Source account has insufficient funds for this transfer."


class DuplicateSubmissionError(LedgerlockError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.DUPLICATE_SUBMISSION
    # PRD.md: a resubmitted key is *rejected*, not reprocessed and not
    # replayed. The point is that the transfer was applied exactly once.
    message = "This Idempotency-Key has already been used. The original "
    message += "transaction was not applied a second time."


class IdempotencyKeyRequiredError(LedgerlockError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    message = "An Idempotency-Key header is required to create a transaction."


class MalformedIdempotencyKeyError(LedgerlockError):
    """The header was present but not a usable key.

    Distinguished from IDEMPOTENCY_KEY_REQUIRED because "you forgot the
    header" and "your key is the wrong shape" need different client fixes.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.MALFORMED_IDEMPOTENCY_KEY
    message = (
        "Idempotency-Key must be 8-128 characters using only letters, "
        "digits, and the characters _ . : -"
    )


class TransactionNotFoundError(LedgerlockError):
    """No such transaction, or the caller was not party to it.

    Returned in both cases on purpose, so the route cannot be used to
    enumerate other people's transaction ids.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.TRANSACTION_NOT_FOUND
    message = "Transaction not found."


class CurrencyMismatchError(LedgerlockError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.CURRENCY_MISMATCH
    message = (
        "Source and destination accounts hold different currencies. "
        "Cross-currency conversion is out of scope (see PRD.md)."
    )


class SameAccountTransferError(LedgerlockError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = ErrorCode.SAME_ACCOUNT_TRANSFER
    message = "Source and destination accounts must be different."


class LedgerImmutableError(LedgerlockError):
    status_code = status.HTTP_405_METHOD_NOT_ALLOWED
    code = ErrorCode.LEDGER_IMMUTABLE
    message = (
        "Ledger entries are immutable. They cannot be updated or deleted. "
        "Record a compensating transaction instead."
    )


class WriteConflictError(LedgerlockError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.WRITE_CONFLICT
    message = (
        "The ledger was concurrently modified and this write could not be "
        "serialised after retrying. Retry with the same Idempotency-Key."
    )


# ---------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------


def error_response(
    status_code: int, code: ErrorCode | str, message: str
) -> JSONResponse:
    """Build the one and only error envelope used by this API."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": str(code), "message": message}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so *every* failure path returns the same envelope.

    Without the catch-all handler at the bottom, an unexpected exception
    would return FastAPI's default ``{"detail": ...}`` shape, and a client
    parsing ``error.code`` would break precisely when things are going
    wrong.
    """

    @app.exception_handler(LedgerlockError)
    async def handle_ledgerlock_error(
        request: Request, exc: LedgerlockError
    ) -> JSONResponse:
        # RULES.md: never fail silently. Every deliberate rejection is
        # logged with its reason and structured context.
        logger.warning(
            "request_rejected",
            extra={
                "error_code": str(exc.code),
                "status_code": exc.status_code,
                "path": request.url.path,
                "method": request.method,
                **exc.context,
            },
        )
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Summarise Pydantic's error list into one message, without echoing
        # the submitted values back (they can contain a password).
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'][1:]) or 'body'}: {err['msg']}"
            for err in exc.errors()
        )
        logger.warning(
            "request_validation_failed",
            extra={
                "error_code": str(ErrorCode.MALFORMED_REQUEST),
                "path": request.url.path,
                "method": request.method,
                "problems": problems,
            },
        )
        return error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorCode.MALFORMED_REQUEST,
            f"Request validation failed. {problems}",
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code_by_status = {
            status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
            status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
            status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
        }
        code = code_by_status.get(exc.status_code, ErrorCode.MALFORMED_REQUEST)
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Log the type and the traceback, return nothing internal to the
        # client: exception text can contain connection strings.
        logger.exception(
            "unhandled_exception",
            extra={
                "error_code": str(ErrorCode.INTERNAL_ERROR),
                "path": request.url.path,
                "method": request.method,
                "exception_type": type(exc).__name__,
            },
        )
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorCode.INTERNAL_ERROR,
            "An internal error occurred.",
        )
