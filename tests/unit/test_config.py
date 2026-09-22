"""Configuration parsing and refusal-to-start behaviour.

This file exists because of a real bug. `SUPPORTED_CURRENCIES=USD,EUR` in
the environment crashed the containerised API at startup during Phase 5,
after passing the entire local test suite, because pydantic-settings treats
any list-typed field as "complex" and tries to `json.loads` the environment
value before any validator runs. It went unnoticed locally only because the
development `.env` predated the setting and so fell through to the default.

The lesson generalises: configuration parsing is code, it has edge cases,
and "the app boots on my machine" tests it only for the values that happen
to be set there. So every supported input form is asserted here, along with
every case where the app is supposed to refuse to start.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    MINIMUM_JWT_SECRET_LENGTH,
    PLACEHOLDER_JWT_SECRET,
    Settings,
)

VALID_SECRET = "a-perfectly-adequate-test-signing-key-0123456789abcdef"


def build_settings(**overrides: object) -> Settings:
    """Construct Settings directly, bypassing the .env file."""
    values: dict[str, object] = {"jwt_secret_key": VALID_SECRET}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# SUPPORTED_CURRENCIES parsing
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USD,EUR", ["USD", "EUR"]),
        ("USD", ["USD"]),
        (" usd , eur ,GBP ", ["USD", "EUR", "GBP"]),
        ('["USD","JPY"]', ["USD", "JPY"]),
        ("USD,USD,EUR", ["USD", "EUR"]),  # deduplicated, order kept
        ("USD,,EUR", ["USD", "EUR"]),  # empty segments dropped
        (["usd", "eur"], ["USD", "EUR"]),  # already a list
    ],
)
async def test_supported_currencies_accepts_every_documented_form(
    raw: object, expected: list[str]
) -> None:
    """Comma-separated, JSON array, and an actual list all work.

    The comma-separated form is the one that broke in a container. It is the
    form documented in .env.example, so it is the one most likely to be used.
    """
    settings = build_settings(supported_currencies=raw)

    assert settings.supported_currencies == expected


async def test_supported_currencies_reads_a_comma_separated_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test proper: read from the environment, not kwargs.

    Passing the value as a keyword argument does not exercise
    pydantic-settings' environment source, which is where the JSON decoding
    happened. This test goes through the environment, so it would have caught
    the original bug.
    """
    monkeypatch.setenv("JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv("SUPPORTED_CURRENCIES", "USD,EUR,GBP,INR")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.supported_currencies == ["USD", "EUR", "GBP", "INR"]


async def test_supported_currencies_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.delenv("SUPPORTED_CURRENCIES", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.supported_currencies == ["USD", "EUR", "GBP", "INR"]


@pytest.mark.parametrize("raw", ["US", "USDD", "US1", "USD,E", ""])
async def test_supported_currencies_rejects_invalid_codes(raw: str) -> None:
    """A malformed currency code must stop startup, not be quietly dropped.

    Every currency needs a SYSTEM boundary account provisioned for it, so a
    junk code would produce a junk boundary account.
    """
    with pytest.raises(ValidationError):
        build_settings(supported_currencies=raw)


# ---------------------------------------------------------------------
# The JWT signing key: the app must refuse to start without a real one
# ---------------------------------------------------------------------


async def test_jwt_secret_key_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no default signing key, by design.

    A hardcoded fallback is the most common way a service ships with a
    publicly known signing key. The variable is removed explicitly because
    the test suite's own conftest sets it for every other test.
    """
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


async def test_the_env_example_placeholder_is_rejected() -> None:
    """Copying .env.example without editing it must fail loudly.

    Otherwise the placeholder becomes a real, published signing key.
    """
    with pytest.raises(ValidationError) as exc_info:
        build_settings(jwt_secret_key=PLACEHOLDER_JWT_SECRET)

    assert "placeholder" in str(exc_info.value).lower()


async def test_a_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_settings(jwt_secret_key="x" * (MINIMUM_JWT_SECRET_LENGTH - 1))


async def test_a_long_enough_jwt_secret_is_accepted() -> None:
    settings = build_settings(jwt_secret_key="y" * MINIMUM_JWT_SECRET_LENGTH)

    assert len(settings.jwt_secret_key) == MINIMUM_JWT_SECRET_LENGTH


@pytest.mark.parametrize("algorithm", ["none", "RS256", "ES256", "HS128", ""])
async def test_only_hmac_algorithms_are_allowed(algorithm: str) -> None:
    """The algorithm is whitelisted in configuration, not taken from tokens.

    Accepting an arbitrary algorithm name is the root of the `alg: none` and
    RS256-to-HS256 confusion attacks. `RS256` is rejected here not because it
    is weak but because this deployment signs with a symmetric key, and
    allowing an asymmetric name alongside a shared secret is exactly the
    confusion.
    """
    with pytest.raises(ValidationError):
        build_settings(jwt_algorithm=algorithm)


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
async def test_supported_hmac_algorithms_are_accepted(algorithm: str) -> None:
    assert build_settings(jwt_algorithm=algorithm).jwt_algorithm == algorithm


# ---------------------------------------------------------------------
# Remaining settings
# ---------------------------------------------------------------------


@pytest.mark.parametrize("rounds", [3, 17, 0, -1])
async def test_bcrypt_rounds_outside_the_sane_range_are_rejected(
    rounds: int,
) -> None:
    """Too low would be insecure; too high would stall the event loop pool."""
    with pytest.raises(ValidationError):
        build_settings(bcrypt_rounds=rounds)


@pytest.mark.parametrize(
    ("raw", "expected"), [("info", "INFO"), ("Debug", "DEBUG"), ("ERROR", "ERROR")]
)
async def test_log_level_is_normalised(raw: str, expected: str) -> None:
    assert build_settings(log_level=raw).log_level == expected


async def test_an_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_settings(log_level="chatty")


@pytest.mark.parametrize("minutes", [0, -5, 1441])
async def test_token_expiry_outside_the_allowed_range_is_rejected(
    minutes: int,
) -> None:
    """No non-expiring tokens, and no tokens valid for longer than a day."""
    with pytest.raises(ValidationError):
        build_settings(access_token_expire_minutes=minutes)


async def test_unrelated_environment_variables_do_not_break_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI and container runtimes inject plenty of unrelated variables."""
    monkeypatch.setenv("JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.setenv("MONGODB_TEST_URI", "mongodb://elsewhere:27017")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("HOSTNAME", "some-pod-abc123")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.jwt_secret_key == VALID_SECRET
