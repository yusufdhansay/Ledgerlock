"""Authentication routes: register, login, logout, and whoami.

Routes stay thin (DESIGN.md): validate, call into the security layer,
shape the response. The only logic that lives here is the ordering that
makes the responses non-leaky, which is commented where it matters.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pymongo.errors import DuplicateKeyError

from app.core import db
from app.core.errors import EmailAlreadyRegisteredError, InvalidCredentialsError
from app.core.logging_config import get_logger
from app.core.security import (
    AuthenticatedUser,
    create_access_token,
    get_current_user,
    hash_password,
    revoke_token,
    verify_password,
    waste_time_like_a_real_verification,
)
from app.models.user import (
    LogoutResponse,
    TokenResponse,
    UserDocument,
    UserLoginRequest,
    UserPublic,
    UserRegisterRequest,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a user and receive an access token",
    responses={
        409: {"description": "EMAIL_ALREADY_REGISTERED"},
        422: {"description": "MALFORMED_REQUEST"},
    },
)
async def register(payload: UserRegisterRequest) -> TokenResponse:
    """Create a user account.

    Passwords are hashed with bcrypt and never stored or logged in
    plaintext. Uniqueness of the email is enforced by a unique index on
    `users.email`, not by a prior existence check: a check-then-insert
    would let two concurrent registrations of the same address both pass
    the check.
    """
    hashed = await hash_password(payload.password)
    user = UserDocument(email=payload.email, hashed_password=hashed)

    try:
        result = await db.users_collection().insert_one(user.to_bson())
    except DuplicateKeyError as exc:
        raise EmailAlreadyRegisteredError(
            context={"email_domain": payload.email.split("@")[-1]}
        ) from exc

    document = {
        "_id": result.inserted_id,
        "email": user.email,
        "created_at": user.created_at,
    }
    issued = create_access_token(result.inserted_id)

    logger.info("user_registered", extra={"user_id": str(result.inserted_id)})

    return TokenResponse(
        access_token=issued.token,
        expires_at=issued.expires_at,
        user=UserPublic.from_document(document),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange credentials for an access token",
    responses={401: {"description": "INVALID_CREDENTIALS"}},
)
async def login(payload: UserLoginRequest) -> TokenResponse:
    """Authenticate and receive an access token.

    Both failure modes (unknown email, wrong password) return the identical
    `INVALID_CREDENTIALS` error, and the unknown-email path deliberately
    performs a throwaway bcrypt verification so the two take comparable
    time. Together that keeps this endpoint from being an account
    enumeration oracle.
    """
    user_document = await db.users_collection().find_one({"email": payload.email})

    if user_document is None:
        await waste_time_like_a_real_verification(payload.password)
        raise InvalidCredentialsError(context={"reason": "email_not_found"})

    if not await verify_password(payload.password, user_document["hashed_password"]):
        raise InvalidCredentialsError(
            context={
                "reason": "password_mismatch",
                "user_id": str(user_document["_id"]),
            }
        )

    issued = create_access_token(user_document["_id"])

    logger.info("user_logged_in", extra={"user_id": str(user_document["_id"])})

    return TokenResponse(
        access_token=issued.token,
        expires_at=issued.expires_at,
        user=UserPublic.from_document(user_document),
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Revoke the access token used to make this request",
    responses={401: {"description": "UNAUTHENTICATED / TOKEN_REVOKED"}},
)
async def logout(
    user: AuthenticatedUser = Depends(get_current_user),
) -> LogoutResponse:
    """Revoke the presented token.

    A stateless JWT cannot be un-issued, so revocation is a blacklist entry
    keyed on the token's `jti`, checked on every authenticated request. The
    entry is pruned automatically by a TTL index once the token would have
    expired on its own.
    """
    await revoke_token(user.jti, user.id, user.token_expires_at)
    return LogoutResponse()


@router.get(
    "/me",
    response_model=UserPublic,
    status_code=status.HTTP_200_OK,
    summary="Return the authenticated user",
    responses={401: {"description": "UNAUTHENTICATED / TOKEN_REVOKED"}},
)
async def read_current_user(
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserPublic:
    """Return the caller's own user record. Never includes the password hash."""
    document = await db.users_collection().find_one({"_id": user.id})
    if document is None:  # pragma: no cover - get_current_user already checked
        raise InvalidCredentialsError()
    return UserPublic.from_document(document)
