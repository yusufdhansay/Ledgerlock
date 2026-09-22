"""Helpers for the concurrency suite.

A note on what "concurrent" means here, because it determines whether
these tests prove anything.

Requests are fired with `asyncio.gather` against the real ASGI application
through an in-process transport. That is genuine concurrency for the
property under test: every request awaits real MongoDB round trips, so
many requests are in flight at the database at once and their reads and
writes interleave exactly as they would across separate processes. The
proof that this is sufficient is in `test_overdraft.py`: the control test,
which removes only the serialisation write, reliably overdraws the account
under this same harness. A harness that could not produce the race could
not produce that result.

What this harness does not cover is multi-process contention, where
requests are spread across OS processes and machines. That is Phase 6's
job, and the reconciliation check is re-run after the load test for
exactly that reason.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

VALID_PASSWORD = "a-perfectly-fine-password"


def fresh_key(label: str = "concurrency") -> str:
    """A unique idempotency key."""
    return f"{label}-{uuid.uuid4()}"


async def register_and_authenticate(
    client: AsyncClient, email: str | None = None
) -> dict[str, str]:
    """Register a user and return an Authorization header for them."""
    address = email or f"user-{uuid.uuid4().hex[:12]}@example.com"
    response = await client.post(
        "/auth/register", json={"email": address, "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def open_account(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    currency: str = "USD",
    label: str = "Account",
) -> str:
    """Open an account and return its id."""
    response = await client.post(
        "/accounts",
        json={"currency": currency, "label": label},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def fund(
    client: AsyncClient,
    headers: dict[str, str],
    account_id: str,
    amount_minor: int,
    *,
    currency: str = "USD",
) -> None:
    """Bring a known amount of value into an account."""
    response = await client.post(
        f"/accounts/{account_id}/funding",
        json={"amount_minor": amount_minor, "currency": currency},
        headers={**headers, "Idempotency-Key": fresh_key("fund")},
    )
    assert response.status_code == 201, response.text


async def read_balance(
    client: AsyncClient, headers: dict[str, str], account_id: str
) -> int:
    """Read an account's computed balance in minor units."""
    response = await client.get(f"/accounts/{account_id}/balance", headers=headers)
    assert response.status_code == 200, response.text
    return int(response.json()["balance_minor"])


async def submit_transfer(
    client: AsyncClient,
    headers: dict[str, str],
    source_id: str,
    destination_id: str,
    amount_minor: int,
    *,
    currency: str = "USD",
    idempotency_key: str | None = None,
) -> Any:
    """Submit one transfer. Returns the raw response for status inspection."""
    return await client.post(
        "/transactions",
        json={
            "source_account_id": source_id,
            "destination_account_id": destination_id,
            "amount_minor": amount_minor,
            "currency": currency,
        },
        headers={
            **headers,
            "Idempotency-Key": idempotency_key or fresh_key("transfer"),
        },
    )


def error_code_of(response: Any) -> str | None:
    """Pull the machine-readable error code out of a response, if any."""
    try:
        return str(response.json()["error"]["code"])
    except Exception:  # noqa: BLE001 - response was a success body
        return None


def summarise(responses: list[Any]) -> dict[str, int]:
    """Count outcomes by status code and error code.

    Used so the tests report *why* each request failed, not just how many
    did. RULES.md requires distinguishable error codes precisely so that a
    result like "10 succeeded, 40 rejected for insufficient funds" can be
    stated rather than "10 succeeded, 40 failed somehow".
    """
    tally: dict[str, int] = {}
    for response in responses:
        if response.status_code in (200, 201):
            key = f"{response.status_code}"
        else:
            key = f"{response.status_code} {error_code_of(response)}"
        tally[key] = tally.get(key, 0) + 1
    return dict(sorted(tally.items()))


@pytest.fixture
async def funded_account(client: AsyncClient) -> dict[str, Any]:
    """One user, a source account funded with 10_000, and a destination."""
    headers = await register_and_authenticate(client)
    source_id = await open_account(client, headers, label="Source")
    destination_id = await open_account(client, headers, label="Destination")
    await fund(client, headers, source_id, 10_000)
    return {
        "headers": headers,
        "source_id": source_id,
        "destination_id": destination_id,
        "opening_balance": 10_000,
    }
