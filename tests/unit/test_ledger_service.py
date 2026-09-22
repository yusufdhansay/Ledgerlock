"""Phase 3 tests: atomic transaction creation.

These are the single-threaded correctness tests for `ledger_service`: a
valid transfer produces exactly one debit and one credit sharing a
transaction id, and every rejection path leaves the database completely
untouched. Concurrency is Phase 4's job; what matters here is that the
sequential semantics and the all-or-nothing property are right, because
the concurrency tests build directly on them.
"""

from __future__ import annotations

import uuid

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

VALID_PASSWORD = "a-perfectly-fine-password"


def fresh_key(label: str = "test") -> str:
    """A unique idempotency key per logical transfer."""
    return f"{label}-{uuid.uuid4()}"


async def register_and_authenticate(
    client: AsyncClient, email: str = "ada@example.com"
) -> dict[str, str]:
    response = await client.post(
        "/auth/register", json={"email": email, "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def open_account(
    client: AsyncClient,
    headers: dict[str, str],
    currency: str = "USD",
    label: str = "Account",
) -> dict:
    response = await client.post(
        "/accounts", json={"currency": currency, "label": label}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def fund(
    client: AsyncClient,
    headers: dict[str, str],
    account_id: str,
    amount_minor: int,
    currency: str = "USD",
) -> dict:
    """Bring value into an account through the funding endpoint."""
    response = await client.post(
        f"/accounts/{account_id}/funding",
        json={"amount_minor": amount_minor, "currency": currency},
        headers={**headers, "Idempotency-Key": fresh_key("fund")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def read_balance(
    client: AsyncClient, headers: dict[str, str], account_id: str
) -> int:
    response = await client.get(f"/accounts/{account_id}/balance", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["balance_minor"]


async def transfer(
    client: AsyncClient,
    headers: dict[str, str],
    source_id: str,
    destination_id: str,
    amount_minor: int,
    *,
    currency: str = "USD",
    idempotency_key: str | None = None,
):
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


@pytest.fixture
async def funded_pair(client: AsyncClient) -> dict:
    """Two accounts owned by one user, the first funded with 100_00."""
    headers = await register_and_authenticate(client)
    source = await open_account(client, headers, label="Source")
    destination = await open_account(client, headers, label="Destination")
    await fund(client, headers, source["id"], 10_000)
    return {
        "headers": headers,
        "source_id": source["id"],
        "destination_id": destination["id"],
    }


# ---------------------------------------------------------------------
# The happy path, in detail
# ---------------------------------------------------------------------


async def test_transfer_creates_exactly_one_debit_and_one_credit(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """The core double-entry assertion.

    One transaction, two entries, equal and opposite, both pointing at the
    same transaction id.
    """
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        2_500,
    )
    assert response.status_code == 201, response.text
    transaction_id = ObjectId(response.json()["id"])

    entries = (
        await app_database["ledger_entries"]
        .find({"transaction_id": transaction_id})
        .to_list(length=10)
    )

    assert len(entries) == 2, f"expected exactly 2 entries, found {len(entries)}"

    debits = [e for e in entries if e["direction"] == "DEBIT"]
    credits = [e for e in entries if e["direction"] == "CREDIT"]
    assert len(debits) == 1
    assert len(credits) == 1

    debit, credit = debits[0], credits[0]
    assert debit["account_id"] == ObjectId(funded_pair["source_id"])
    assert credit["account_id"] == ObjectId(funded_pair["destination_id"])
    assert debit["amount_minor"] == credit["amount_minor"] == 2_500
    assert debit["signed_amount_minor"] == -2_500
    assert credit["signed_amount_minor"] == 2_500
    assert debit["transaction_id"] == credit["transaction_id"] == transaction_id
    assert debit["currency"] == credit["currency"] == "USD"


async def test_transfer_nets_to_zero_across_the_pair(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """The pair of entries sums to zero. This is reconciliation in miniature."""
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        1_234,
    )
    transaction_id = ObjectId(response.json()["id"])

    entries = (
        await app_database["ledger_entries"]
        .find({"transaction_id": transaction_id})
        .to_list(length=10)
    )

    assert sum(e["signed_amount_minor"] for e in entries) == 0


async def test_transfer_moves_the_balance(
    client: AsyncClient, funded_pair: dict
) -> None:
    headers = funded_pair["headers"]

    await transfer(
        client, headers, funded_pair["source_id"], funded_pair["destination_id"], 3_000
    )

    assert await read_balance(client, headers, funded_pair["source_id"]) == 7_000
    assert await read_balance(client, headers, funded_pair["destination_id"]) == 3_000


async def test_transfer_response_reports_status_explicitly(
    client: AsyncClient, funded_pair: dict
) -> None:
    """DESIGN.md: a client never infers status from the HTTP code alone."""
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        100,
    )

    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["kind"] == "TRANSFER"
    assert body["amount_minor"] == 100
    assert body["currency"] == "USD"
    assert body["idempotency_key"]


async def test_transfer_of_entire_balance_is_allowed(
    client: AsyncClient, funded_pair: dict
) -> None:
    """Spending down to exactly zero is fine. The rule is never *below* zero."""
    headers = funded_pair["headers"]

    response = await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        10_000,
    )

    assert response.status_code == 201, response.text
    assert await read_balance(client, headers, funded_pair["source_id"]) == 0


# ---------------------------------------------------------------------
# Funding: how value enters the ledger
# ---------------------------------------------------------------------


async def test_funding_debits_the_system_boundary_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Funding is an ordinary double entry, with SYSTEM on the other side.

    The boundary account's balance goes negative by the funded amount. That
    is intended: its negative balance is the total value held across user
    accounts in that currency.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    await fund(client, headers, account["id"], 50_000)

    system = await app_database["accounts"].find_one(
        {"account_type": "SYSTEM", "currency": "USD"}
    )
    assert system is not None

    system_entries = (
        await app_database["ledger_entries"]
        .find({"account_id": system["_id"]})
        .to_list(length=10)
    )
    assert len(system_entries) == 1
    assert system_entries[0]["direction"] == "DEBIT"
    assert system_entries[0]["signed_amount_minor"] == -50_000

    assert await read_balance(client, headers, account["id"]) == 50_000


async def test_funding_keeps_the_whole_ledger_at_zero(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Every entry in the system still nets to zero after funding."""
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)
    await fund(client, headers, account["id"], 12_345)

    rows = (
        await app_database["ledger_entries"]
        .aggregate([{"$group": {"_id": None, "net": {"$sum": "$signed_amount_minor"}}}])
        .to_list(length=1)
    )

    assert rows[0]["net"] == 0


async def test_funding_requires_ownership(client: AsyncClient) -> None:
    owner_headers = await register_and_authenticate(client, "owner@example.com")
    account = await open_account(client, owner_headers)
    intruder_headers = await register_and_authenticate(client, "intruder@example.com")

    response = await client.post(
        f"/accounts/{account['id']}/funding",
        json={"amount_minor": 1_000, "currency": "USD"},
        headers={**intruder_headers, "Idempotency-Key": fresh_key()},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


async def test_funding_rejects_currency_mismatch(client: AsyncClient) -> None:
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers, currency="USD")

    response = await client.post(
        f"/accounts/{account['id']}/funding",
        json={"amount_minor": 1_000, "currency": "EUR"},
        headers={**headers, "Idempotency-Key": fresh_key()},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CURRENCY_MISMATCH"


async def test_a_user_cannot_spend_directly_from_the_system_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The boundary account must not be usable as a transfer source.

    It is exempt from the overdraft check, so being able to name it as a
    source would be being able to mint money. It has no owner, and the
    route requires the caller to own the source.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)
    system = await app_database["accounts"].find_one(
        {"account_type": "SYSTEM", "currency": "USD"}
    )
    assert system is not None

    response = await transfer(
        client, headers, str(system["_id"]), account["id"], 1_000_000
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert await app_database["ledger_entries"].count_documents({}) == 0


# ---------------------------------------------------------------------
# Insufficient funds: rejected, and writes nothing
# ---------------------------------------------------------------------


async def test_insufficient_funds_is_rejected(
    client: AsyncClient, funded_pair: dict
) -> None:
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        10_001,  # one minor unit more than the balance
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSUFFICIENT_FUNDS"


async def test_insufficient_funds_writes_absolutely_nothing(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """A rejected transfer leaves no transaction and no entries.

    This is the rollback guarantee. If the sufficiency check happened after
    the inserts, or outside the transaction, there would be orphaned entries
    here.
    """
    entries_before = await app_database["ledger_entries"].count_documents({})
    transactions_before = await app_database["transactions"].count_documents({})

    await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        999_999,
    )

    assert await app_database["ledger_entries"].count_documents({}) == entries_before
    assert await app_database["transactions"].count_documents({}) == transactions_before


async def test_rejected_transfer_leaves_both_balances_unchanged(
    client: AsyncClient, funded_pair: dict
) -> None:
    headers = funded_pair["headers"]
    source_before = await read_balance(client, headers, funded_pair["source_id"])
    destination_before = await read_balance(
        client, headers, funded_pair["destination_id"]
    )

    await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        500_000,
    )

    assert (
        await read_balance(client, headers, funded_pair["source_id"]) == source_before
    )
    assert (
        await read_balance(client, headers, funded_pair["destination_id"])
        == destination_before
    )


async def test_an_account_with_no_funds_cannot_transfer_anything(
    client: AsyncClient,
) -> None:
    """A brand-new account has a balance of zero and can move nothing."""
    headers = await register_and_authenticate(client)
    source = await open_account(client, headers, label="Empty")
    destination = await open_account(client, headers, label="Destination")

    response = await transfer(client, headers, source["id"], destination["id"], 1)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INSUFFICIENT_FUNDS"


async def test_a_failed_transfer_does_not_burn_its_idempotency_key(
    client: AsyncClient, funded_pair: dict
) -> None:
    """A rejection rolls back, so the key was never recorded as used.

    The client can fix the amount and retry with the same key. If the key
    had been consumed by the failed attempt, the corrected retry would be
    wrongly rejected as a duplicate.
    """
    headers = funded_pair["headers"]
    key = fresh_key("retry-after-failure")

    rejected = await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        999_999,
        idempotency_key=key,
    )
    assert rejected.status_code == 422

    accepted = await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        1_000,
        idempotency_key=key,
    )

    assert accepted.status_code == 201, accepted.text


# ---------------------------------------------------------------------
# Idempotency, sequential case
# ---------------------------------------------------------------------


async def test_reusing_an_idempotency_key_is_rejected(
    client: AsyncClient, funded_pair: dict
) -> None:
    headers = funded_pair["headers"]
    key = fresh_key("reuse")

    first = await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        1_000,
        idempotency_key=key,
    )
    second = await transfer(
        client,
        headers,
        funded_pair["source_id"],
        funded_pair["destination_id"],
        1_000,
        idempotency_key=key,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_SUBMISSION"


async def test_a_replayed_request_is_applied_exactly_once(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """Five identical submissions move the money once, not five times."""
    headers = funded_pair["headers"]
    key = fresh_key("replay")

    statuses = []
    for _ in range(5):
        response = await transfer(
            client,
            headers,
            funded_pair["source_id"],
            funded_pair["destination_id"],
            2_000,
            idempotency_key=key,
        )
        statuses.append(response.status_code)

    assert statuses == [201, 409, 409, 409, 409]
    assert (
        await app_database["transactions"].count_documents({"idempotency_key": key})
        == 1
    )
    assert await read_balance(client, headers, funded_pair["destination_id"]) == 2_000


async def test_a_different_key_for_the_same_amounts_is_a_separate_transfer(
    client: AsyncClient, funded_pair: dict
) -> None:
    """Idempotency is keyed on the key, not on the payload.

    Two genuinely separate payments of the same amount to the same person
    must both go through.
    """
    headers = funded_pair["headers"]

    first = await transfer(
        client, headers, funded_pair["source_id"], funded_pair["destination_id"], 1_500
    )
    second = await transfer(
        client, headers, funded_pair["source_id"], funded_pair["destination_id"], 1_500
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert await read_balance(client, headers, funded_pair["destination_id"]) == 3_000


async def test_missing_idempotency_key_is_rejected(
    client: AsyncClient, funded_pair: dict
) -> None:
    response = await client.post(
        "/transactions",
        json={
            "source_account_id": funded_pair["source_id"],
            "destination_account_id": funded_pair["destination_id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers=funded_pair["headers"],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.parametrize(
    "bad_key",
    [
        "short",  # under 8 characters
        "has spaces in it",
        "has/slashes/in/it",
        "x" * 129,  # over 128 characters
        "semi;colon;injection",
    ],
)
async def test_malformed_idempotency_keys_are_rejected(
    client: AsyncClient, funded_pair: dict, bad_key: str
) -> None:
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        100,
        idempotency_key=bad_key,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_IDEMPOTENCY_KEY"


# ---------------------------------------------------------------------
# Other rejection paths
# ---------------------------------------------------------------------


async def test_transfer_to_self_is_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    entries_before = await app_database["ledger_entries"].count_documents({})

    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["source_id"],
        100,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SAME_ACCOUNT_TRANSFER"
    assert await app_database["ledger_entries"].count_documents({}) == entries_before


async def test_transfer_from_an_account_you_do_not_own_is_forbidden(
    client: AsyncClient,
) -> None:
    owner_headers = await register_and_authenticate(client, "owner@example.com")
    victim = await open_account(client, owner_headers)
    await fund(client, owner_headers, victim["id"], 100_000)

    thief_headers = await register_and_authenticate(client, "thief@example.com")
    thief_account = await open_account(client, thief_headers)

    response = await transfer(
        client, thief_headers, victim["id"], thief_account["id"], 100_000
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert await read_balance(client, owner_headers, victim["id"]) == 100_000


async def test_transfer_to_another_users_account_is_allowed(
    client: AsyncClient,
) -> None:
    """Paying someone else is the normal case; only the source must be owned."""
    sender_headers = await register_and_authenticate(client, "sender@example.com")
    sender_account = await open_account(client, sender_headers)
    await fund(client, sender_headers, sender_account["id"], 5_000)

    recipient_headers = await register_and_authenticate(client, "recipient@example.com")
    recipient_account = await open_account(client, recipient_headers)

    response = await transfer(
        client, sender_headers, sender_account["id"], recipient_account["id"], 2_000
    )

    assert response.status_code == 201, response.text
    assert (
        await read_balance(client, recipient_headers, recipient_account["id"]) == 2_000
    )


async def test_transfer_to_unknown_account_is_not_found(
    client: AsyncClient, funded_pair: dict
) -> None:
    response = await transfer(
        client, funded_pair["headers"], funded_pair["source_id"], str(ObjectId()), 100
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


async def test_transfer_between_different_currencies_is_rejected(
    client: AsyncClient,
) -> None:
    """Cross-currency conversion is out of scope (PRD.md), so it must fail
    loudly rather than move a nominally equal number of minor units."""
    headers = await register_and_authenticate(client)
    usd_account = await open_account(client, headers, currency="USD", label="USD")
    eur_account = await open_account(client, headers, currency="EUR", label="EUR")
    await fund(client, headers, usd_account["id"], 10_000, currency="USD")

    response = await transfer(
        client, headers, usd_account["id"], eur_account["id"], 1_000, currency="USD"
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CURRENCY_MISMATCH"


async def test_transfer_from_a_frozen_account_is_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """FROZEN accounts cannot take part in a transaction on either side.

    There is no route to change status, so the status is set directly here.
    The check itself runs inside the ledger transaction, against the same
    state the write would apply to.
    """
    headers = funded_pair["headers"]
    await app_database["accounts"].update_one(
        {"_id": ObjectId(funded_pair["source_id"])}, {"$set": {"status": "FROZEN"}}
    )

    response = await transfer(
        client, headers, funded_pair["source_id"], funded_pair["destination_id"], 100
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVE"


async def test_transfer_to_a_closed_account_is_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    await app_database["accounts"].update_one(
        {"_id": ObjectId(funded_pair["destination_id"])},
        {"$set": {"status": "CLOSED"}},
    )

    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        100,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_ACTIVE"


@pytest.mark.parametrize(
    ("amount", "reason"),
    [
        (0, "zero is not a movement of money"),
        (-100, "direction comes from which account is debited, not the sign"),
    ],
)
async def test_non_positive_amounts_are_rejected(
    client: AsyncClient, funded_pair: dict, amount: int, reason: str
) -> None:
    response = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        amount,
    )

    assert response.status_code == 422, reason
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_fractional_amounts_are_rejected(
    client: AsyncClient, funded_pair: dict
) -> None:
    """Amounts are integer minor units. A float is a client bug, not a value
    to round."""
    response = await client.post(
        "/transactions",
        json={
            "source_account_id": funded_pair["source_id"],
            "destination_account_id": funded_pair["destination_id"],
            "amount_minor": 10.5,
            "currency": "USD",
        },
        headers={**funded_pair["headers"], "Idempotency-Key": fresh_key()},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_transfer_requires_authentication(
    client: AsyncClient, funded_pair: dict
) -> None:
    response = await client.post(
        "/transactions",
        json={
            "source_account_id": funded_pair["source_id"],
            "destination_account_id": funded_pair["destination_id"],
            "amount_minor": 100,
            "currency": "USD",
        },
        headers={"Idempotency-Key": fresh_key()},
    )

    assert response.status_code == 401


async def test_amount_is_not_read_from_an_unexpected_field(
    client: AsyncClient, funded_pair: dict
) -> None:
    """Unknown body fields are rejected, not ignored.

    A silently-ignored `amount` alongside a missing `amount_minor` is the
    kind of thing that moves the wrong sum.
    """
    response = await client.post(
        "/transactions",
        json={
            "source_account_id": funded_pair["source_id"],
            "destination_account_id": funded_pair["destination_id"],
            "amount_minor": 100,
            "currency": "USD",
            "amount": 999_999,
        },
        headers={**funded_pair["headers"], "Idempotency-Key": fresh_key()},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Reading a transaction back
# ---------------------------------------------------------------------


async def test_a_participant_can_read_the_transaction(
    client: AsyncClient, funded_pair: dict
) -> None:
    created = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        750,
    )
    transaction_id = created.json()["id"]

    response = await client.get(
        f"/transactions/{transaction_id}", headers=funded_pair["headers"]
    )

    assert response.status_code == 200, response.text
    assert response.json()["amount_minor"] == 750
    assert response.json()["status"] == "COMPLETED"


async def test_a_stranger_cannot_read_the_transaction(
    client: AsyncClient, funded_pair: dict
) -> None:
    created = await transfer(
        client,
        funded_pair["headers"],
        funded_pair["source_id"],
        funded_pair["destination_id"],
        750,
    )
    stranger_headers = await register_and_authenticate(client, "stranger@example.com")

    response = await client.get(
        f"/transactions/{created.json()['id']}", headers=stranger_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRANSACTION_NOT_FOUND"


# ---------------------------------------------------------------------
# The serialisation mechanism itself
# ---------------------------------------------------------------------


async def test_debiting_increments_the_serialisation_counter(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """The counter exists to create a write-conflict point on the source.

    Asserted directly so that if someone later removes the `$inc` as a
    pointless write, this test fails and points at the module docstring
    explaining why it is load-bearing: without a shared document to
    conflict on, two concurrent debits both read the same pre-debit balance
    and both commit.
    """
    from app.services.ledger_service import DEBIT_SERIALISATION_FIELD

    headers = funded_pair["headers"]
    source_id = ObjectId(funded_pair["source_id"])

    for _ in range(3):
        response = await transfer(
            client,
            headers,
            funded_pair["source_id"],
            funded_pair["destination_id"],
            100,
        )
        assert response.status_code == 201, response.text

    source = await app_database["accounts"].find_one({"_id": source_id})
    assert source is not None
    assert source[DEBIT_SERIALISATION_FIELD] == 3


async def test_the_counter_is_not_used_as_a_balance(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_pair: dict
) -> None:
    """The counter counts attempts; it must bear no relation to the balance.

    Stated as a test so the distinction is enforced rather than just
    asserted in a comment. RULES.md forbids a stored balance, and this field
    is not one.
    """
    from app.services.ledger_service import DEBIT_SERIALISATION_FIELD

    headers = funded_pair["headers"]
    await transfer(
        client, headers, funded_pair["source_id"], funded_pair["destination_id"], 4_000
    )

    source = await app_database["accounts"].find_one(
        {"_id": ObjectId(funded_pair["source_id"])}
    )
    assert source is not None
    assert source[DEBIT_SERIALISATION_FIELD] == 1
    assert await read_balance(client, headers, funded_pair["source_id"]) == 6_000


async def test_funding_does_not_lock_the_system_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Funding must not take the serialisation lock on the SYSTEM account.

    The lock exists only to serialise the overdraft check, and a SYSTEM
    source has no overdraft check. If funding took it, every funding request
    for a currency would contend on a single document and serialise the
    whole system.
    """
    from app.services.ledger_service import DEBIT_SERIALISATION_FIELD

    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)
    await fund(client, headers, account["id"], 1_000)
    await fund(client, headers, account["id"], 2_000)

    system = await app_database["accounts"].find_one(
        {"account_type": "SYSTEM", "currency": "USD"}
    )
    assert system is not None
    assert DEBIT_SERIALISATION_FIELD not in system
