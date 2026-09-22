"""User models: registration, login, tokens, and the stored document."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.common import MongoDocument, utc_now

#: bcrypt hashes at most the first 72 bytes of input and silently ignores
#: the rest. Accepting a longer password would mean two different
#: passwords that share a 72-byte prefix both authenticate, so the limit
#: is enforced explicitly rather than hidden inside the hash call.
BCRYPT_MAX_PASSWORD_BYTES = 72

MINIMUM_PASSWORD_LENGTH = 12

Password = Annotated[
    str,
    Field(
        min_length=MINIMUM_PASSWORD_LENGTH,
        max_length=BCRYPT_MAX_PASSWORD_BYTES,
        description=(
            f"At least {MINIMUM_PASSWORD_LENGTH} characters and at most "
            f"{BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded "
            "(bcrypt's input limit)."
        ),
        examples=["correct-horse-battery-staple"],
    ),
]


def _validate_password_byte_length(value: str) -> str:
    encoded_length = len(value.encode("utf-8"))
    if encoded_length > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes when "
            f"UTF-8 encoded; got {encoded_length}. Multi-byte characters "
            "count more than once."
        )
    return value


# ---------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------


class UserRegisterRequest(BaseModel):
    """Payload for POST /auth/register."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(examples=["ada@example.com"])
    password: Password

    @field_validator("password")
    @classmethod
    def check_password_byte_length(cls, value: str) -> str:
        return _validate_password_byte_length(value)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        """Lower-case the address.

        Stored lower-cased so the unique index on `email` cannot be
        side-stepped by registering the same address with different
        capitalisation.
        """
        return value.strip().lower()


class UserLoginRequest(BaseModel):
    """Payload for POST /auth/login."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(examples=["ada@example.com"])
    # No length constraints on login: rejecting a too-short password here
    # would leak the password policy and add a needless failure mode. The
    # value is only ever compared against a hash.
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def normalise_email(cls, value: str) -> str:
        return value.strip().lower()


# ---------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------


class UserPublic(BaseModel):
    """A user as exposed by the API. Never includes the password hash."""

    id: str = Field(examples=["665f1b2c8a4e5f0012ab34cd"])
    email: EmailStr
    created_at: datetime

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> UserPublic:
        return cls(
            id=str(document["_id"]),
            email=document["email"],
            created_at=document["created_at"],
        )


class TokenResponse(BaseModel):
    """Access token issued on register/login."""

    access_token: str
    token_type: str = Field(default="bearer")
    expires_at: datetime = Field(
        description="ISO 8601 UTC instant at which this token stops being valid."
    )
    user: UserPublic


class LogoutResponse(BaseModel):
    """Confirmation that a token was revoked."""

    revoked: bool = Field(default=True)
    message: str = Field(default="Token revoked.")


# ---------------------------------------------------------------------
# Stored documents
# ---------------------------------------------------------------------


class UserDocument(MongoDocument):
    """The `users` collection document."""

    id: ObjectId | None = Field(default=None, alias="_id")
    email: EmailStr
    # The bcrypt hash. Named explicitly so no call site mistakes it for a
    # plaintext password, and so the log redaction filter matches it.
    hashed_password: str
    created_at: datetime = Field(default_factory=utc_now)


class RevokedTokenDocument(MongoDocument):
    """The `revoked_tokens` collection document (the token blacklist).

    Keyed by the token's `jti` claim rather than the raw token string, so
    the blacklist never stores a usable credential. A TTL index on
    `expires_at` lets MongoDB drop entries once the token would have
    expired anyway, which keeps the collection bounded without a cron job.
    """

    id: ObjectId | None = Field(default=None, alias="_id")
    jti: str = Field(description="The JWT ID claim of the revoked token.")
    user_id: ObjectId
    expires_at: datetime = Field(
        description="Original token expiry; the TTL index prunes on this."
    )
    revoked_at: datetime = Field(default_factory=utc_now)
