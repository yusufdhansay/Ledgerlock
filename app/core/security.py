"""Password hashing, access tokens, revocation, and the auth dependency.

Details here that are deliberate rather than incidental:

* bcrypt is CPU-bound and takes tens to hundreds of milliseconds. Calling
  it directly inside an async route would block the event loop and stall
  every other in-flight request, which would both be a bug and would
  corrupt the Phase 6 load test numbers. Both hashing and verification are
  therefore run in a worker thread.
* The JWT decoder is pinned to the single configured algorithm and
  requires the claims it depends on. Trusting the `alg` header from the
  token is the root of the classic "alg: none" and RS256->HS256 confusion
  attacks.
* Revocation is keyed on the token's `jti`, not the token string, so the
  blacklist never stores a credential that would work if leaked.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import bcrypt
import jwt
from anyio import to_thread
from bson import ObjectId
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core import db
from app.core.config import Settings, get_settings
from app.core.errors import (
    ForbiddenError,
    TokenExpiredError,
    TokenRevokedError,
    UnauthenticatedError,
)
from app.core.logging_config import get_logger
from app.models.common import utc_now

logger = get_logger(__name__)

#: Bearer scheme with auto_error disabled so a missing or malformed header
#: is turned into this application's own error envelope rather than
#: FastAPI's default `{"detail": ...}` shape (DESIGN.md).
bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token.")

TOKEN_TYPE_ACCESS = "access"  # noqa: S105 - a JWT claim value, not a secret

#: A real bcrypt hash of a random throwaway value, used to equalise the
#: cost of a login attempt for an address that does not exist. Without
#: this, "no such user" returns fast and "wrong password" returns slowly,
#: which is a usable account-enumeration side channel even though the
#: error message is identical.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_bytes(32), bcrypt.gensalt(rounds=4)).decode()


# ---------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------


def _hash_password_blocking(plain_password: str, rounds: int) -> str:
    encoded = plain_password.encode("utf-8")
    if len(encoded) > 72:
        # Guarded by the Pydantic model too. Repeated here because bcrypt
        # would otherwise silently ignore everything past 72 bytes, making
        # two different passwords interchangeable.
        raise ValueError("password exceeds bcrypt's 72-byte input limit")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=rounds)).decode()


def _verify_password_blocking(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except ValueError:
        # Malformed stored hash. Treated as a failed verification rather
        # than a 500, and logged without the hash itself.
        logger.error("password_hash_malformed", extra={})
        return False


async def hash_password(plain_password: str, *, rounds: int | None = None) -> str:
    """Hash a password with bcrypt, off the event loop."""
    settings = get_settings()
    return await to_thread.run_sync(
        _hash_password_blocking, plain_password, rounds or settings.bcrypt_rounds
    )


async def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a password against its bcrypt hash, off the event loop."""
    return await to_thread.run_sync(
        _verify_password_blocking, plain_password, hashed_password
    )


async def waste_time_like_a_real_verification(plain_password: str) -> None:
    """Spend roughly the cost of a verification, then discard the result.

    Called on the "email not found" path so that path's latency does not
    distinguish it from "email found, wrong password".
    """
    await to_thread.run_sync(_verify_password_blocking, plain_password, _DUMMY_HASH)


# ---------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------


class IssuedToken(NamedTuple):
    """An access token plus the metadata a caller needs to report or revoke it."""

    token: str
    jti: str
    expires_at: datetime


def create_access_token(
    user_id: ObjectId | str,
    *,
    settings: Settings | None = None,
) -> IssuedToken:
    """Mint a signed access token for a user."""
    active_settings = settings or get_settings()
    issued_at = utc_now()
    expires_at = issued_at + timedelta(
        minutes=active_settings.access_token_expire_minutes
    )
    # Random jti: the revocation key. Must be unpredictable so one user
    # cannot pre-emptively revoke another user's future token.
    jti = secrets.token_urlsafe(24)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "jti": jti,
        "typ": TOKEN_TYPE_ACCESS,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(
        payload,
        active_settings.jwt_secret_key,
        algorithm=active_settings.jwt_algorithm,
    )
    return IssuedToken(token=token, jti=jti, expires_at=expires_at)


def decode_access_token(
    token: str, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Verify and decode a token, or raise a Ledgerlock auth error.

    The algorithm list is pinned to the configured one, and the claims this
    application depends on are declared required, so a token missing `exp`
    is rejected rather than treated as non-expiring.
    """
    active_settings = settings or get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            active_settings.jwt_secret_key,
            algorithms=[active_settings.jwt_algorithm],
            options={
                "require": ["sub", "jti", "exp", "iat"],
                "verify_exp": True,
                "verify_signature": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except InvalidTokenError as exc:
        # Covers bad signature, wrong algorithm, missing required claim,
        # malformed structure. The reason is logged, not returned: telling a
        # caller *why* their forged token failed only helps them forge a
        # better one.
        logger.warning("token_rejected", extra={"reason": type(exc).__name__})
        raise UnauthenticatedError("Access token is invalid.") from exc

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise UnauthenticatedError("Access token is invalid.")

    return payload


# ---------------------------------------------------------------------
# Revocation (the token blacklist)
# ---------------------------------------------------------------------


async def revoke_token(jti: str, user_id: ObjectId, expires_at: datetime) -> None:
    """Add a token's jti to the blacklist. Idempotent.

    Uses an upsert rather than an insert so that logging out twice with the
    same token succeeds both times instead of raising a duplicate-key
    error on the unique `jti` index.
    """
    await db.revoked_tokens_collection().update_one(
        {"jti": jti},
        {
            "$setOnInsert": {
                "jti": jti,
                "user_id": user_id,
                "expires_at": expires_at,
                "revoked_at": utc_now(),
            }
        },
        upsert=True,
    )
    logger.info("token_revoked", extra={"user_id": str(user_id)})


async def is_token_revoked(jti: str) -> bool:
    """True if this jti is on the blacklist."""
    found = await db.revoked_tokens_collection().find_one(
        {"jti": jti}, projection={"_id": 1}
    )
    return found is not None


# ---------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------


class AuthenticatedUser(NamedTuple):
    """The caller, resolved from a valid, non-revoked token."""

    id: ObjectId
    email: str
    jti: str
    token_expires_at: datetime


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    """Resolve the authenticated caller, or raise.

    Order matters: signature and expiry are checked before the database is
    touched, so an unauthenticated flood cannot be turned into a database
    read amplification.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError()
    if credentials.scheme.lower() != "bearer":
        raise UnauthenticatedError("Authorization header must use the Bearer scheme.")

    payload = decode_access_token(credentials.credentials)
    jti = str(payload["jti"])

    if await is_token_revoked(jti):
        raise TokenRevokedError()

    user_id_raw = str(payload["sub"])
    if not ObjectId.is_valid(user_id_raw):
        raise UnauthenticatedError("Access token is invalid.")
    user_id = ObjectId(user_id_raw)

    user_document = await db.users_collection().find_one(
        {"_id": user_id}, projection={"email": 1}
    )
    if user_document is None:
        # Validly signed token for a user that no longer exists.
        raise UnauthenticatedError("Access token is invalid.")

    expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=utc_now().tzinfo)

    # Stashed for handlers and middleware that want to attribute a request.
    request.state.user_id = str(user_id)

    return AuthenticatedUser(
        id=user_id,
        email=str(user_document["email"]),
        jti=jti,
        token_expires_at=expires_at,
    )


def require_account_owner(
    account_document: dict[str, Any], user: AuthenticatedUser
) -> None:
    """Raise unless `user` owns `account_document`.

    Returns 403, not 404, only because the caller has already been shown to
    be authenticated; the account's existence is not itself a secret worth
    protecting in this system.
    """
    if account_document.get("owner_id") != user.id:
        raise ForbiddenError(
            "You do not own this account.",
            context={
                "account_id": str(account_document.get("_id")),
                "user_id": str(user.id),
            },
        )
