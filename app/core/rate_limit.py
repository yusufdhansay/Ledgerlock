"""Rate limiting for public-facing endpoints.

Limits are read through callables rather than baked in at import time, so a
deployment can change them by environment variable and the test suite can
switch them off without reaching into module state. `exempt_when` is checked
per request, which is what makes `RATE_LIMIT_ENABLED=false` work at runtime.

Two honest caveats, both repeated in MEMORY.md:

* **The counters are per process.** `slowapi` keeps them in memory, so with N
  replicas the effective global limit is roughly N times the configured
  number. Making it exact needs shared storage such as Redis. This is a rate
  limiter, not a distributed rate limiter, and calling it the latter would be
  a lie.
* **It is a courtesy control, not a defence against a determined attacker.**
  Keying on client IP means anyone with a pool of addresses can step around
  it. Its job is to stop one misbehaving client from monopolising the
  service, and to make credential stuffing against `/auth/login` slow enough
  to be unattractive.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.core.logging_config import get_logger

logger = get_logger(__name__)


def rate_limit_key(request: Request) -> str:
    """Identify the caller for rate limiting purposes.

    Prefers the authenticated user id, falling back to the client address.
    Keying authenticated traffic by user rather than by IP matters in both
    directions: several users behind one NAT do not consume each other's
    budget, and one user cannot multiply their budget by rotating addresses.

    `request.state.user_id` is set by `get_current_user`, which FastAPI
    resolves before the endpoint runs, so it is populated by the time this is
    called on an authenticated route.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"
    return f"ip:{get_remote_address(request)}"


def rate_limiting_is_disabled(_request: Request | None = None) -> bool:
    """Used as `exempt_when`, so the switch is honoured per request.

    The test suite disables limiting globally (many tests fire a hundred
    requests at once, and a limiter would make them fail for the wrong
    reason) and re-enables it in the tests that exist to verify it.
    """
    return not get_settings().rate_limit_enabled


def auth_limit() -> str:
    """Limit for credential endpoints. Tightest, since these are guessable."""
    return get_settings().rate_limit_auth


def transaction_limit() -> str:
    """Limit for endpoints that write to the ledger."""
    return get_settings().rate_limit_transactions


def default_limit() -> str:
    """Limit for everything else."""
    return get_settings().rate_limit_default


limiter = Limiter(
    key_func=rate_limit_key,
    # headers_enabled stays OFF deliberately. With it on, slowapi tries to
    # inject X-RateLimit-* headers into whatever the endpoint returned, and
    # these endpoints return Pydantic models rather than Response objects, so
    # it raises "parameter `response` must be an instance of
    # starlette.responses.Response" on every rate-limited route. Retry-After
    # is set by hand in the handler below, which is the header that actually
    # matters to a client.
    headers_enabled=False,
    # Errors inside the limiter must never take down a request path that
    # moves money. If the limiter itself breaks, requests proceed unlimited
    # and the failure is logged, rather than every transfer returning 500.
    swallow_errors=True,
)


async def handle_rate_limit_exceeded(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Return the project's standard error envelope for a 429.

    slowapi's own handler returns its own shape, which would break DESIGN.md's
    guarantee that every error response is
    `{"error": {"code": ..., "message": ...}}`.
    """
    logger.warning(
        "rate_limited",
        extra={
            "error_code": str(ErrorCode.RATE_LIMITED),
            "path": request.url.path,
            "method": request.method,
            "limit": str(exc.detail),
        },
    )
    # Retry-After in whole seconds, taken from the window of the limit that
    # was breached, so a client has something concrete to wait for instead of
    # guessing and hammering.
    retry_after_seconds = 60
    try:
        retry_after_seconds = int(exc.limit.limit.get_expiry())
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        pass

    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "code": str(ErrorCode.RATE_LIMITED),
                "message": (
                    "Too many requests. Slow down and retry after the "
                    "interval given in the Retry-After header."
                ),
            }
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


def register_rate_limiting(app: FastAPI) -> None:
    """Attach the limiter and its handler to the application."""
    settings = get_settings()

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, handle_rate_limit_exceeded)  # type: ignore[arg-type]

    logger.info(
        "rate_limiting_configured",
        extra={
            "enabled": settings.rate_limit_enabled,
            "auth": settings.rate_limit_auth,
            "transactions": settings.rate_limit_transactions,
            "default": settings.rate_limit_default,
            # Stated in the log because it is a real operational limitation,
            # not a footnote.
            "scope": "per-process counters; not shared across replicas",
        },
    )
