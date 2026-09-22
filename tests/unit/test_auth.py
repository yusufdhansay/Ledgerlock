"""Phase 1 tests: authentication flows.

Covers the happy paths, and the failure paths that are easy to get subtly
wrong: duplicate registration under a unique index, case-folded emails,
the fact that a password hash never leaves the service, and that a
revoked token actually stops working.
"""

from __future__ import annotations

import asyncio

import jwt
import pytest
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import BCRYPT_MAX_PASSWORD_BYTES

VALID_PASSWORD = "a-perfectly-fine-password"


async def register_user(
    client: AsyncClient,
    email: str = "ada@example.com",
    password: str = VALID_PASSWORD,
) -> dict:
    response = await client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------


async def test_register_returns_token_and_user_without_password(
    client: AsyncClient,
) -> None:
    body = await register_user(client)

    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_at"]
    assert body["user"]["email"] == "ada@example.com"
    assert body["user"]["id"]

    # The hash must never appear in a response, under any key name.
    serialised = str(body)
    assert "password" not in serialised.lower()
    assert "$2b$" not in serialised, "a bcrypt hash leaked into the response"


async def test_register_stores_a_bcrypt_hash_not_the_plaintext(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    await register_user(client)

    document = await app_database["users"].find_one({"email": "ada@example.com"})
    assert document is not None
    assert "password" not in document, "plaintext password field was stored"
    assert document["hashed_password"].startswith("$2b$")
    assert VALID_PASSWORD not in document["hashed_password"]
    assert await verify_password(VALID_PASSWORD, document["hashed_password"]) is True


async def test_register_rejects_duplicate_email(client: AsyncClient) -> None:
    await register_user(client)

    response = await client.post(
        "/auth/register",
        json={"email": "ada@example.com", "password": VALID_PASSWORD},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_register_treats_email_case_insensitively(client: AsyncClient) -> None:
    """Emails are lower-cased before storage.

    Otherwise `Ada@example.com` and `ada@example.com` are two accounts as
    far as the unique index is concerned, which is an account takeover
    vector in any system that later compares addresses case-insensitively.
    """
    await register_user(client, email="ada@example.com")

    response = await client.post(
        "/auth/register",
        json={"email": "ADA@Example.COM", "password": VALID_PASSWORD},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


async def test_concurrent_registration_of_same_email_creates_exactly_one_user(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Uniqueness must come from the index, not a check-then-insert.

    Fired concurrently, a read-then-write existence check would let both
    requests through. The unique index on `users.email` is what makes this
    hold.
    """
    payload = {"email": "race@example.com", "password": VALID_PASSWORD}
    responses = await asyncio.gather(
        *(client.post("/auth/register", json=payload) for _ in range(8))
    )

    created = [r for r in responses if r.status_code == 201]
    conflicted = [r for r in responses if r.status_code == 409]

    assert len(created) == 1, f"{len(created)} registrations succeeded, expected 1"
    assert len(conflicted) == 7
    assert all(
        r.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED" for r in conflicted
    )
    assert (
        await app_database["users"].count_documents({"email": "race@example.com"}) == 1
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"email": "not-an-email", "password": VALID_PASSWORD}, "invalid email"),
        ({"email": "ada@example.com", "password": "short"}, "password too short"),
        ({"email": "ada@example.com"}, "missing password"),
        ({"password": VALID_PASSWORD}, "missing email"),
        (
            {
                "email": "ada@example.com",
                "password": VALID_PASSWORD,
                "is_admin": True,
            },
            "unexpected field must be rejected, not silently ignored",
        ),
    ],
)
async def test_register_rejects_malformed_payloads(
    client: AsyncClient, payload: dict, reason: str
) -> None:
    response = await client.post("/auth/register", json=payload)

    assert response.status_code == 422, reason
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_register_rejects_password_over_bcrypt_byte_limit(
    client: AsyncClient,
) -> None:
    """A password longer than bcrypt's 72-byte input limit is refused.

    Accepting it would mean bcrypt silently ignores the tail, so two
    different long passwords sharing a 72-byte prefix would both
    authenticate.
    """
    too_long = "x" * (BCRYPT_MAX_PASSWORD_BYTES + 1)

    response = await client.post(
        "/auth/register", json={"email": "ada@example.com", "password": too_long}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_register_rejects_multibyte_password_over_byte_limit(
    client: AsyncClient,
) -> None:
    """The limit is on bytes, not characters.

    24 three-byte characters is 72 bytes and fits; 25 is 75 bytes and does
    not, even though the string is well under any character-count limit.
    """
    within = "\u20ac" * 24  # 72 bytes
    over = "\u20ac" * 25  # 75 bytes

    ok = await client.post(
        "/auth/register", json={"email": "within@example.com", "password": within}
    )
    rejected = await client.post(
        "/auth/register", json={"email": "over@example.com", "password": over}
    )

    assert ok.status_code == 201, ok.text
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------


async def test_login_with_correct_password_returns_token(client: AsyncClient) -> None:
    await register_user(client)

    response = await client.post(
        "/auth/login",
        json={"email": "ada@example.com", "password": VALID_PASSWORD},
    )

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]
    assert response.json()["user"]["email"] == "ada@example.com"


async def test_login_is_case_insensitive_on_email(client: AsyncClient) -> None:
    await register_user(client, email="ada@example.com")

    response = await client.post(
        "/auth/login",
        json={"email": "  ADA@Example.com  ", "password": VALID_PASSWORD},
    )

    assert response.status_code == 200, response.text


async def test_login_with_wrong_password_is_rejected(client: AsyncClient) -> None:
    await register_user(client)

    response = await client.post(
        "/auth/login",
        json={"email": "ada@example.com", "password": "definitely-not-the-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    client: AsyncClient,
) -> None:
    """The two failure modes must not be tellable apart by a client.

    Same status, same code, same message. A different response for "no such
    user" would turn this endpoint into an account enumeration oracle.
    """
    await register_user(client, email="ada@example.com")

    wrong_password = await client.post(
        "/auth/login",
        json={"email": "ada@example.com", "password": "wrong-password-here"},
    )
    unknown_email = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password-here"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


# ---------------------------------------------------------------------
# Authenticated access, logout, revocation
# ---------------------------------------------------------------------


async def test_me_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_me_returns_the_authenticated_user(client: AsyncClient) -> None:
    body = await register_user(client)
    token = body["access_token"]

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "ada@example.com"
    assert "hashed_password" not in response.json()


async def test_logout_revokes_the_token(client: AsyncClient) -> None:
    body = await register_user(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    logout = await client.post("/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True

    after = await client.get("/auth/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_logout_twice_is_idempotent(client: AsyncClient) -> None:
    """Revoking an already-revoked token must not 500.

    The blacklist has a unique index on `jti`, so a plain insert would
    raise a duplicate-key error the second time. It uses an upsert instead.
    The second call is rejected as TOKEN_REVOKED by the auth dependency,
    which is the correct outcome, not a server error.
    """
    body = await register_user(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    first = await client.post("/auth/logout", headers=headers)
    second = await client.post("/auth/logout", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_revocation_record_stores_jti_not_the_token(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The blacklist must not store a usable credential."""
    body = await register_user(client)
    token = body["access_token"]
    await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    record = await app_database["revoked_tokens"].find_one({})
    assert record is not None
    assert record["jti"]
    assert token not in str(record), "the raw token was stored in the blacklist"


async def test_expired_token_is_rejected(client: AsyncClient) -> None:
    body = await register_user(client)
    user_id = body["user"]["id"]
    settings = get_settings()

    # Forge a correctly signed token that expired an hour ago. Signed with
    # the real key, so this proves expiry is checked rather than the
    # signature doing all the work.
    expired = jwt.encode(
        {
            "sub": user_id,
            "jti": "expired-token-jti",
            "typ": "access",
            "iat": 1_600_000_000,
            "exp": 1_600_003_600,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {expired}"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_token_signed_with_the_wrong_key_is_rejected(
    client: AsyncClient,
) -> None:
    body = await register_user(client)

    forged = jwt.encode(
        {
            "sub": body["user"]["id"],
            "jti": "forged",
            "typ": "access",
            "iat": 2_000_000_000,
            "exp": 4_000_000_000,
        },
        "an-attackers-own-key-which-is-long-enough-to-look-real",
        algorithm="HS256",
    )

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_unsigned_alg_none_token_is_rejected(client: AsyncClient) -> None:
    """The `alg: none` attack must fail.

    The decoder is pinned to the configured HMAC algorithm rather than
    trusting the token's own header, so an unsigned token is not accepted
    no matter what it claims about itself.
    """
    body = await register_user(client)

    unsigned = jwt.encode(
        {
            "sub": body["user"]["id"],
            "jti": "unsigned",
            "typ": "access",
            "iat": 2_000_000_000,
            "exp": 4_000_000_000,
        },
        key="",
        algorithm="none",
    )

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {unsigned}"}
    )

    assert response.status_code == 401


async def test_token_for_a_deleted_user_is_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    body = await register_user(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    await app_database["users"].delete_many({})

    response = await client.get("/auth/me", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header_value",
    ["", "Bearer", "Basic abcdef", "Token abcdef", "bearer", "Bearer    "],
)
async def test_malformed_authorization_headers_are_rejected(
    client: AsyncClient, header_value: str
) -> None:
    response = await client.get("/auth/me", headers={"Authorization": header_value})

    assert response.status_code == 401


# ---------------------------------------------------------------------
# Hashing primitives
# ---------------------------------------------------------------------


async def test_hashing_the_same_password_twice_gives_different_hashes() -> None:
    """Salting must be per-hash, or equal passwords are visibly equal."""
    first = await hash_password(VALID_PASSWORD)
    second = await hash_password(VALID_PASSWORD)

    assert first != second
    assert await verify_password(VALID_PASSWORD, first)
    assert await verify_password(VALID_PASSWORD, second)


async def test_verify_password_rejects_a_wrong_password() -> None:
    hashed = await hash_password(VALID_PASSWORD)

    assert await verify_password("not-the-password", hashed) is False


async def test_verify_password_rejects_a_malformed_hash() -> None:
    """A corrupt stored hash fails verification rather than raising."""
    assert await verify_password(VALID_PASSWORD, "not-a-bcrypt-hash") is False


async def test_issued_tokens_have_unique_jtis() -> None:
    """jti must be unpredictable and unique, since it is the revocation key.

    A shared or guessable jti would let revoking one token revoke another,
    or let one user revoke another user's session.
    """
    jtis = {create_access_token("665f1b2c8a4e5f0012ab34cd").jti for _ in range(50)}

    assert len(jtis) == 50
