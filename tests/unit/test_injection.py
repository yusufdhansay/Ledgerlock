"""NoSQL injection: no user input may reach a MongoDB query as an operator.

RULES.md requires that a MongoDB query is never built from raw, unvalidated
user input, and that operators like `$where` can never be constructed from it.
This is MongoDB's analogue of SQL injection and it has a distinctive shape:
because queries are documents rather than strings, the attack is not a quoted
string breaking out of its quotes, it is a *JSON object* arriving where a
scalar was expected. A filter of `{"email": {"$ne": null}}` matches every
user, and `$where` executes server-side JavaScript.

Two layers stop it here, and both are tested:

1. Pydantic types every external field as a scalar with a pattern or a
   constraint, so a dict or a list is rejected at the edge with
   MALFORMED_REQUEST and never reaches a service function.
2. Identifiers are additionally constrained to validated 24-hex ObjectId
   strings, so even a string that looks like an operator cannot be used as an
   id.

The tests below submit operator payloads in every position an external value
can occupy: request bodies, path parameters, query parameters, and headers.
"""

from __future__ import annotations

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

VALID_PASSWORD = "a-perfectly-fine-password"

#: Payloads that would be dangerous if they reached a query document.
OPERATOR_PAYLOADS: list[object] = [
    {"$ne": None},
    {"$gt": ""},
    {"$regex": ".*"},
    {"$exists": True},
    {"$where": "1 == 1"},
    {"$nin": []},
    ["$ne", None],
]


async def register(client: AsyncClient, email: str = "victim@example.com") -> dict:
    response = await client.post(
        "/auth/register", json={"email": email, "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------
# Authentication: the highest-value target
# ---------------------------------------------------------------------


@pytest.mark.parametrize("payload", OPERATOR_PAYLOADS)
async def test_login_rejects_an_operator_object_as_the_email(
    client: AsyncClient, payload: object
) -> None:
    """The classic MongoDB auth bypass must not work.

    `{"email": {"$ne": null}, "password": ...}` is the textbook attack: in a
    system that passes the body straight into `find_one`, the filter matches
    the first user in the collection and the attacker is then only one password
    check away, or past it entirely if the password is handled the same way.
    Here `email` is typed `EmailStr`, so a dict never becomes a filter value.
    """
    await register(client)

    response = await client.post(
        "/auth/login", json={"email": payload, "password": VALID_PASSWORD}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


@pytest.mark.parametrize("payload", OPERATOR_PAYLOADS)
async def test_login_rejects_an_operator_object_as_the_password(
    client: AsyncClient, payload: object
) -> None:
    """A dict password must not slip past verification.

    In a naive implementation the password is compared inside the query, so
    `{"$ne": null}` authenticates without knowing anything. Here the password
    is a `str` and is only ever passed to bcrypt, never into a filter.
    """
    await register(client)

    response = await client.post(
        "/auth/login", json={"email": "victim@example.com", "password": payload}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_login_with_an_operator_shaped_string_does_not_authenticate(
    client: AsyncClient,
) -> None:
    """A *string* that looks like an operator is just a wrong password.

    Worth distinguishing from the dict cases: the string `{"$ne": null}` is
    valid JSON-as-text but is treated as an ordinary value, so it fails
    authentication normally rather than being interpreted.
    """
    await register(client)

    response = await client.post(
        "/auth/login",
        json={"email": "victim@example.com", "password": '{"$ne": null}'},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.parametrize("payload", OPERATOR_PAYLOADS)
async def test_registration_rejects_operator_objects(
    client: AsyncClient, payload: object
) -> None:
    response = await client.post(
        "/auth/register", json={"email": payload, "password": VALID_PASSWORD}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Identifiers in paths
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_id",
    [
        '{"$ne": null}',
        '{"$gt": ""}',
        "$where",
        "*",
        "null",
        "0" * 24 + "extra",
        "' OR 1=1",
        "%24ne",
    ],
)
async def test_account_paths_reject_anything_that_is_not_an_object_id(
    client: AsyncClient, raw_id: str
) -> None:
    """Every account path parameter is a validated 24-hex ObjectId string.

    Because the type is checked before the handler runs, none of these reaches
    a filter document. The response is MALFORMED_REQUEST, not a 404, which is
    the tell that it was rejected at the edge rather than looked up and missed.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    for path in (f"/accounts/{raw_id}", f"/accounts/{raw_id}/balance"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 422, f"{path} returned {response.status_code}"
        assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


@pytest.mark.parametrize("traversal", ["../../admin", "..%2f..%2fadmin", "./../x"])
async def test_path_traversal_in_an_account_id_reaches_nothing(
    client: AsyncClient, traversal: str
) -> None:
    """Traversal sequences are handled by routing, not by validation.

    Separated from the test above because the outcome is legitimately
    different: a URL containing `../` is normalised by the client and the
    router before any parameter validation happens, so the request lands on a
    different path (or no path) and returns 404 rather than 422. Both are
    correct refusals; asserting 422 here would have been asserting the wrong
    mechanism. What matters is that it is never a success.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    response = await client.get(f"/accounts/{traversal}/balance", headers=headers)

    assert response.status_code in (
        404,
        422,
    ), f"traversal returned {response.status_code}"
    assert response.status_code != 200


@pytest.mark.parametrize("raw_id", ['{"$ne": null}', "$where", "not-an-id"])
async def test_transaction_path_rejects_anything_that_is_not_an_object_id(
    client: AsyncClient, raw_id: str
) -> None:
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    response = await client.get(f"/transactions/{raw_id}", headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Transfer bodies
# ---------------------------------------------------------------------


@pytest.mark.parametrize("payload", OPERATOR_PAYLOADS)
async def test_transfer_rejects_operator_objects_in_account_ids(
    client: AsyncClient, payload: object
) -> None:
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    account = await client.post(
        "/accounts", json={"currency": "USD", "label": "S"}, headers=headers
    )
    account_id = account.json()["id"]

    for field in ("source_account_id", "destination_account_id"):
        request_body = {
            "source_account_id": account_id,
            "destination_account_id": account_id,
            "amount_minor": 100,
            "currency": "USD",
        }
        request_body[field] = payload  # type: ignore[assignment]

        response = await client.post(
            "/transactions",
            json=request_body,
            headers={**headers, "Idempotency-Key": "injection-probe-key-0001"},
        )

        assert response.status_code == 422, field
        assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


@pytest.mark.parametrize(
    "amount",
    [{"$gt": 0}, ["100"], "100", None, True, {"$where": "1==1"}],
)
async def test_transfer_rejects_non_integer_amounts(
    client: AsyncClient, amount: object
) -> None:
    """Amounts must be plain integers.

    `True` is in this list on purpose: Python treats `bool` as a subclass of
    `int`, so a permissive validator would happily accept `True` as an amount
    of 1. Pydantic in strict-ish mode rejects it, and this test makes sure that
    stays true.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    source = await client.post(
        "/accounts", json={"currency": "USD", "label": "S"}, headers=headers
    )
    destination = await client.post(
        "/accounts", json={"currency": "USD", "label": "D"}, headers=headers
    )

    response = await client.post(
        "/transactions",
        json={
            "source_account_id": source.json()["id"],
            "destination_account_id": destination.json()["id"],
            "amount_minor": amount,
            "currency": "USD",
        },
        headers={**headers, "Idempotency-Key": "injection-probe-key-0002"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


@pytest.mark.parametrize(
    "currency", [{"$ne": None}, "$where", "usd", "US", "USDD", "", None, 840]
)
async def test_currency_must_match_a_strict_pattern(
    client: AsyncClient, currency: object
) -> None:
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    response = await client.post(
        "/accounts", json={"currency": currency, "label": "X"}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] in {
        "MALFORMED_REQUEST",
        "UNSUPPORTED_CURRENCY",
    }


# ---------------------------------------------------------------------
# Headers and query parameters
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        '{"$ne": null}',
        "$where",
        "key with spaces",
        "key/with/slashes",
        "key;with;semicolons",
        "{$gt: ''}",
    ],
)
async def test_idempotency_key_is_restricted_to_url_safe_characters(
    client: AsyncClient, key: str
) -> None:
    """The idempotency key becomes a query value and a unique index key.

    It is constrained by pattern to `[A-Za-z0-9_.:-]`, so nothing that could be
    mistaken for an operator, or that would be awkward in a log or a URL, gets
    through.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    source = await client.post(
        "/accounts", json={"currency": "USD", "label": "S"}, headers=headers
    )
    destination = await client.post(
        "/accounts", json={"currency": "USD", "label": "D"}, headers=headers
    )

    response = await client.post(
        "/transactions",
        json={
            "source_account_id": source.json()["id"],
            "destination_account_id": destination.json()["id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={**headers, "Idempotency-Key": key},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_IDEMPOTENCY_KEY"


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("limit", '{"$gt": 0}'),
        ("limit", "-1"),
        ("limit", "99999"),
        ("limit", "abc"),
        ("offset", "-5"),
        ("offset", '{"$ne": null}'),
    ],
)
async def test_pagination_parameters_are_bounded_integers(
    client: AsyncClient, param: str, value: str
) -> None:
    """Unbounded or non-numeric pagination is rejected.

    A negative or enormous `limit` is a denial-of-service lever as much as an
    injection one, so both are constrained rather than clamped silently.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    response = await client.get(f"/accounts?{param}={value}", headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Nothing leaked, and no extra fields accepted
# ---------------------------------------------------------------------


async def test_no_request_can_add_an_unexpected_field(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Every request model sets `extra="forbid"`.

    Silently ignoring unknown fields is how a client ends up believing it set
    something it did not. Rejecting them also stops an attacker probing for a
    field the server happens to honour.
    """
    body = await register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    probes = [
        ("POST", "/accounts", {"currency": "USD", "account_type": "SYSTEM"}),
        ("POST", "/accounts", {"currency": "USD", "owner_id": str(ObjectId())}),
        ("POST", "/accounts", {"currency": "USD", "balance_minor": 5_000}),
        (
            "POST",
            "/auth/register",
            {
                "email": "extra@example.com",
                "password": VALID_PASSWORD,
                "is_admin": True,
            },
        ),
    ]

    for method, path, payload in probes:
        response = await client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 422, f"{path} accepted {payload}"
        assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_validation_errors_do_not_echo_the_submitted_password(
    client: AsyncClient,
) -> None:
    """A 422 must not reflect the password back in its message.

    Validation errors are the easiest place for a secret to end up in a log or
    an error-tracking service, because the natural implementation includes the
    offending input.
    """
    secret = "this-particular-password-must-not-be-echoed"

    response = await client.post(
        "/auth/register", json={"email": "not-an-email", "password": secret}
    )

    assert response.status_code == 422
    assert secret not in response.text, "the submitted password was echoed back"


async def test_an_operator_payload_never_reaches_the_database(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """End-to-end check: after a barrage of injection attempts, nothing moved.

    The individual tests above assert the status codes. This one asserts the
    consequence that actually matters: no user, account, transaction or ledger
    entry was created or altered by any of it.
    """
    body = await register(client, "baseline@example.com")
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    await client.post(
        "/accounts", json={"currency": "USD", "label": "Baseline"}, headers=headers
    )

    counts_before = {
        name: await app_database[name].count_documents({})
        for name in ("users", "accounts", "transactions", "ledger_entries")
    }

    attacks = [
        ("POST", "/auth/login", {"email": {"$ne": None}, "password": {"$ne": None}}),
        ("POST", "/auth/register", {"email": {"$ne": None}, "password": {"$ne": None}}),
        ("POST", "/accounts", {"currency": {"$ne": None}}),
        (
            "POST",
            "/transactions",
            {
                "source_account_id": {"$ne": None},
                "destination_account_id": {"$ne": None},
                "amount_minor": {"$gt": 0},
                "currency": {"$ne": None},
            },
        ),
    ]
    for method, path, payload in attacks:
        await client.request(
            method,
            path,
            json=payload,
            headers={**headers, "Idempotency-Key": "injection-final-probe-key"},
        )

    counts_after = {
        name: await app_database[name].count_documents({})
        for name in ("users", "accounts", "transactions", "ledger_entries")
    }

    print(f"\nINJECTION TEST: counts before = {counts_before}")
    print(f"INJECTION TEST: counts after  = {counts_after}")

    assert counts_after == counts_before, "an injection attempt changed the database"
