"""Application configuration, loaded from the environment.

Every secret comes from an environment variable (RULES.md). The JWT
signing key deliberately has no default: a hardcoded fallback is the
single most common way a "secure" service ships with a publicly known
signing key, so this app refuses to start without one being supplied.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: The placeholder shipped in .env.example. Treated as "not configured"
#: so that copying the example file without editing it fails loudly
#: instead of running with a known key.
PLACEHOLDER_JWT_SECRET = "replace-me-with-a-generated-secret"  # noqa: S105 - this is the value we refuse, not a credential

MINIMUM_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Unrelated env vars (e.g. MONGODB_TEST_URI, CI vars) must not
        # crash startup.
        extra="ignore",
    )

    # ---- MongoDB -----------------------------------------------------
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017/?directConnection=true",
        description=(
            "Connection string for the MongoDB replica set. Must point at a "
            "replica set, not a standalone mongod: multi-document "
            "transactions are unavailable on a standalone and this "
            "application's atomicity guarantee depends on them."
        ),
    )
    mongodb_db_name: str = Field(default="ledgerlock", min_length=1)

    # ---- JWT ---------------------------------------------------------
    jwt_secret_key: str = Field(
        ...,  # required: no default, by design
        description="HMAC signing key for access tokens. Must be supplied.",
    )
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)

    # ---- Password hashing --------------------------------------------
    bcrypt_rounds: int = Field(
        default=12,
        ge=4,
        le=16,
        description=(
            "bcrypt cost factor. 12 is the production default. The test "
            "suite lowers it to 4 purely so that fixtures which create many "
            "users do not spend most of their runtime hashing; it is never "
            "lowered for a real deployment."
        ),
    )

    # ---- Application -------------------------------------------------
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # `NoDecode` is required, not cosmetic. Without it, pydantic-settings
    # treats any list-typed field as "complex" and tries to json.loads the
    # environment value *before* any validator runs, so a perfectly
    # reasonable `SUPPORTED_CURRENCIES=USD,EUR` raises a SettingsError at
    # startup. NoDecode hands the raw string to the validator below instead.
    # This was found by the containerised API failing to boot in Phase 5,
    # having passed every local test, because the local .env predated the
    # setting and so fell through to the default.
    supported_currencies: Annotated[list[str], NoDecode] = Field(
        default=["USD", "EUR", "GBP", "INR"],
        description=(
            "Currencies accounts may be opened in. A whitelist rather than "
            "free text: every currency needs its own SYSTEM boundary account, "
            "and an unbounded set would mean an unbounded number of those. "
            "Accepts a comma-separated list or a JSON array."
        ),
    )

    @field_validator("supported_currencies", mode="before")
    @classmethod
    def parse_currency_list(cls, value: object) -> object:
        """Accept `USD,EUR` as well as `["USD","EUR"]` from the environment."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [
                part.strip().upper() for part in stripped.split(",") if part.strip()
            ]
        return value

    @field_validator("supported_currencies")
    @classmethod
    def validate_currency_codes(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("SUPPORTED_CURRENCIES must list at least one currency")
        normalised = [code.strip().upper() for code in value]
        for code in normalised:
            if len(code) != 3 or not code.isalpha():
                raise ValueError(
                    f"{code!r} is not a 3-letter ISO 4217 alphabetic currency code"
                )
        # Deduplicate while keeping declared order.
        return list(dict.fromkeys(normalised))

    # ---- Rate limiting (in-process; see MEMORY.md known limitations) --
    rate_limit_auth: str = Field(default="10/minute")
    rate_limit_transactions: str = Field(default="100/minute")
    rate_limit_default: str = Field(default="200/minute")

    @field_validator("jwt_secret_key")
    @classmethod
    def reject_placeholder_or_weak_secret(cls, value: str) -> str:
        if value == PLACEHOLDER_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET_KEY is still the placeholder from .env.example. "
                'Generate one with: python -c "import secrets; '
                'print(secrets.token_urlsafe(64))"'
            )
        if len(value) < MINIMUM_JWT_SECRET_LENGTH:
            raise ValueError(
                f"JWT_SECRET_KEY must be at least {MINIMUM_JWT_SECRET_LENGTH} "
                f"characters; got {len(value)}."
            )
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def only_allow_symmetric_hmac(cls, value: str) -> str:
        """Whitelist the algorithm rather than trusting the token header.

        Accepting an arbitrary algorithm name from configuration (and, at
        decode time, from the token itself) is how "alg: none" and
        RS256->HS256 confusion attacks work. Only HMAC-SHA variants are
        allowed, and the decoder is pinned to this single value.
        """
        allowed = {"HS256", "HS384", "HS512"}
        if value not in allowed:
            raise ValueError(f"JWT_ALGORITHM must be one of {sorted(allowed)}")
        return value

    @field_validator("log_level")
    @classmethod
    def normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return level


@lru_cache
def get_settings() -> Settings:
    """Return the singleton settings object.

    Cached so the .env file is parsed once. Tests that need different
    configuration call ``get_settings.cache_clear()`` after adjusting the
    environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env
