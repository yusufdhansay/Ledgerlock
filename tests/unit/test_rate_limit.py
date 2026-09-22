"""Rate limiting.

The rest of the suite runs with `RATE_LIMIT_ENABLED=false`, because dozens of
tests fire fifty to a hundred requests at once to exercise concurrency and a
limiter would fail them for a reason unrelated to what they test. So limiting
is verified here, with it deliberately switched back on and set to values
small enough to trip inside a test.

What is asserted: that the limit actually blocks, that a blocked request gets
the project's standard error envelope rather than slowapi's own shape, that
credential endpoints are limited more tightly than the rest, that limiting is
keyed per caller rather than globally, and that the health probes are exempt
(a rate-limited liveness probe would get a pod restarted).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.main import create_app

VALID_PASSWORD = "a-perfectly-fine-password"


@pytest.fixture
async def limited_client(
    app_database: AsyncIOMotorDatabase, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """A client against an app with rate limiting ON and set very low.

    The limiter's in-memory storage is reset for each test, because it is
    module-level state that would otherwise leak counts between tests and make
    them order-dependent.
    """
    from app.core.rate_limit import limiter

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_AUTH", "3/minute")
    monkeypatch.setenv("RATE_LIMIT_TRANSACTIONS", "5/minute")
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "8/minute")
    get_settings.cache_clear()

    limiter.reset()

    application = create_app()
    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport, base_url="http://ledgerlock.test"
        ) as client:
            yield client

    limiter.reset()
    get_settings.cache_clear()


async def test_auth_endpoint_blocks_past_its_limit(
    limited_client: AsyncClient,
) -> None:
    """The fourth registration attempt inside the window is refused.

    Credential endpoints get the tightest limit, because they are the ones an
    attacker can make progress against by sheer repetition.
    """
    statuses = []
    for index in range(5):
        response = await limited_client.post(
            "/auth/register",
            json={"email": f"user{index}@example.com", "password": VALID_PASSWORD},
        )
        statuses.append(response.status_code)

    print(f"\nAUTH LIMIT TEST: statuses = {statuses} (limit is 3/minute)")

    assert statuses[:3] == [201, 201, 201]
    assert statuses[3:] == [429, 429]


async def test_a_rate_limited_response_uses_the_standard_error_envelope(
    limited_client: AsyncClient,
) -> None:
    """A 429 must look like every other error from this API.

    slowapi's built-in handler returns its own shape, which would break
    DESIGN.md's promise that a client can always read `error.code`.
    """
    for index in range(4):
        response = await limited_client.post(
            "/auth/register",
            json={"email": f"envelope{index}@example.com", "password": VALID_PASSWORD},
        )

    assert response.status_code == 429
    body = response.json()

    print(f"\nENVELOPE TEST: 429 body = {body}")
    print(f"ENVELOPE TEST: Retry-After = {response.headers.get('retry-after')}")

    assert set(body) == {"error"}, "the 429 body is not the standard envelope"
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["message"]
    # Tell the client when to come back rather than making it guess.
    assert "retry-after" in {k.lower() for k in response.headers}


async def test_the_transaction_endpoint_has_its_own_higher_limit(
    limited_client: AsyncClient,
) -> None:
    """Ledger writes are limited separately from credential endpoints.

    One tight limit across everything would either throttle legitimate
    transaction traffic or leave login wide open. Here auth is 3/minute and
    transactions 5/minute.

    Worth recording, because it is easy to assume otherwise: slowapi scopes a
    limit to the individual endpoint, so `POST /transactions` and
    `POST /accounts/{id}/funding` each get their own 5/minute bucket even
    though both are configured from `RATE_LIMIT_TRANSACTIONS`. The configured
    value is therefore a per-endpoint allowance, not a shared ledger-write
    allowance. That is a reasonable default, and it is the observed behaviour
    rather than a claim: the funding call below succeeds and five subsequent
    transfers still succeed before the sixth is refused.
    """
    registration = await limited_client.post(
        "/auth/register",
        json={"email": "payer@example.com", "password": VALID_PASSWORD},
    )
    assert registration.status_code == 201
    headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}

    source = await limited_client.post(
        "/accounts", json={"currency": "USD", "label": "S"}, headers=headers
    )
    destination = await limited_client.post(
        "/accounts", json={"currency": "USD", "label": "D"}, headers=headers
    )
    assert source.status_code == 201, source.text
    assert destination.status_code == 201, destination.text

    # Funding is configured from the same setting but gets its own bucket.
    funding = await limited_client.post(
        f"/accounts/{source.json()['id']}/funding",
        json={"amount_minor": 100_000, "currency": "USD"},
        headers={**headers, "Idempotency-Key": "fund-key-for-rate-limit-test"},
    )
    assert funding.status_code == 201, funding.text

    statuses = []
    for index in range(6):
        response = await limited_client.post(
            "/transactions",
            json={
                "source_account_id": source.json()["id"],
                "destination_account_id": destination.json()["id"],
                "amount_minor": 10,
                "currency": "USD",
            },
            headers={**headers, "Idempotency-Key": f"rate-limit-transfer-{index}"},
        )
        statuses.append(response.status_code)

    print(
        f"\nTRANSACTION LIMIT TEST: statuses = {statuses} "
        f"(5/minute, scoped per endpoint)"
    )

    assert 201 in statuses, "no transfer got through at all"
    assert 429 in statuses, "the transaction limit never engaged"
    # More ledger writes got through than the auth limit of 3 would have
    # allowed, so the two budgets are demonstrably separate.
    assert statuses.count(201) > 3


async def test_limits_are_per_caller_not_global(
    limited_client: AsyncClient,
) -> None:
    """One caller exhausting its budget must not block a different caller.

    Authenticated traffic is keyed on the user id, so several users behind one
    address do not consume each other's allowance, and one user cannot enlarge
    their allowance by changing address.
    """
    first = await limited_client.post(
        "/auth/register",
        json={"email": "first@example.com", "password": VALID_PASSWORD},
    )
    second = await limited_client.post(
        "/auth/register",
        json={"email": "second@example.com", "password": VALID_PASSWORD},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}

    # Burn the first user's default budget (8/minute).
    first_statuses = []
    for _ in range(10):
        response = await limited_client.get("/auth/me", headers=first_headers)
        first_statuses.append(response.status_code)

    # The second user must be unaffected.
    second_status = (
        await limited_client.get("/auth/me", headers=second_headers)
    ).status_code

    print(f"\nPER-CALLER TEST: first user statuses = {first_statuses}")
    print(f"PER-CALLER TEST: second user status after that = {second_status}")

    assert 429 in first_statuses, "the first user was never limited"
    assert second_status == 200, (
        "the second user was blocked by the first user's usage; the limit is "
        "global rather than per caller"
    )


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
async def test_health_probes_are_never_rate_limited(
    limited_client: AsyncClient, path: str
) -> None:
    """Probes must be exempt, or Kubernetes will restart healthy pods.

    A liveness probe that receives a 429 counts as a failure, and enough
    consecutive failures gets the container killed. Rate limiting a health
    endpoint is therefore a way to cause an outage.
    """
    statuses = [(await limited_client.get(path)).status_code for _ in range(25)]

    print(
        f"\nPROBE TEST: {path} -> {len(statuses)} requests, "
        f"429 count = {statuses.count(429)}"
    )

    assert 429 not in statuses
    assert all(status in (200, 503) for status in statuses)


async def test_limiting_is_off_for_the_rest_of_the_suite(
    client: AsyncClient,
) -> None:
    """Guards the arrangement the rest of the suite depends on.

    If `RATE_LIMIT_ENABLED` ever defaulted to true in the test environment,
    dozens of concurrency tests would start failing with 429s, which is a
    confusing symptom to debug. This makes that failure explicit and
    immediate.
    """
    assert get_settings().rate_limit_enabled is False

    statuses = [
        (
            await client.post(
                "/auth/register",
                json={"email": f"unlimited{i}@example.com", "password": VALID_PASSWORD},
            )
        ).status_code
        for i in range(12)
    ]

    assert 429 not in statuses, (
        "rate limiting is active in the default test client; the concurrency "
        "suite will fail spuriously"
    )
    assert statuses.count(201) == 12
