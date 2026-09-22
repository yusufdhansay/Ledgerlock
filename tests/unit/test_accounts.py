"""Phase 2 tests: account creation and aggregation-based balance.

The balance tests deliberately insert ledger entries directly, with known
values, and then assert what the endpoint computes. That is the only way
to prove the balance is *derived* rather than tracked: nothing wrote a
balance field, yet the correct figure comes back.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.common import utc_now

VALID_PASSWORD = "a-perfectly-fine-password"


async def register_and_authenticate(
    client: AsyncClient, email: str = "ada@example.com"
) -> dict[str, str]:
    """Register a user and return an Authorization header for them."""
    response = await client.post(
        "/auth/register", json={"email": email, "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def open_account(
    client: AsyncClient,
    headers: dict[str, str],
    currency: str = "USD",
    label: str = "Everyday spending",
) -> dict:
    response = await client.post(
        "/accounts",
        json={"currency": currency, "label": label},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def insert_entry(
    database: AsyncIOMotorDatabase,
    account_id: str,
    direction: str,
    amount_minor: int,
    *,
    currency: str = "USD",
    transaction_id: ObjectId | None = None,
    created_at_offset_seconds: int = 0,
) -> ObjectId:
    """Insert one ledger entry directly, bypassing the service layer.

    Used only to set up a known ledger state for balance assertions. The
    collection's `$jsonSchema` + `$expr` validator still applies, so these
    inserts cannot create an entry the application itself could not.
    """
    signed = amount_minor if direction == "CREDIT" else -amount_minor
    result = await database["ledger_entries"].insert_one(
        {
            "transaction_id": transaction_id or ObjectId(),
            "account_id": ObjectId(account_id),
            "direction": direction,
            "amount_minor": amount_minor,
            "signed_amount_minor": signed,
            "currency": currency,
            "created_at": utc_now() + timedelta(seconds=created_at_offset_seconds),
        }
    )
    return result.inserted_id


# ---------------------------------------------------------------------
# Account creation
# ---------------------------------------------------------------------


async def test_create_account_returns_the_account(client: AsyncClient) -> None:
    headers = await register_and_authenticate(client)

    account = await open_account(client, headers, currency="USD", label="Main")

    assert account["currency"] == "USD"
    assert account["label"] == "Main"
    assert account["status"] == "ACTIVE"
    assert account["account_type"] == "USER"
    assert account["id"]


async def test_created_account_has_no_stored_balance_field(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The stored document must not contain a balance field.

    This is the structural guarantee in RULES.md. If a `balance` field ever
    appears on an account document, the bug class this project exists to
    prevent is back.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    document = await app_database["accounts"].find_one({"_id": ObjectId(account["id"])})
    assert document is not None
    for forbidden in ("balance", "balance_minor", "available_balance"):
        assert forbidden not in document, (
            f"account document stores a {forbidden!r} field; balance must "
            "always be derived from ledger entries"
        )


async def test_account_response_does_not_include_a_balance(
    client: AsyncClient,
) -> None:
    """Balance is a separate resource, not an attribute of the account."""
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    assert "balance" not in account
    assert "balance_minor" not in account


async def test_create_account_requires_authentication(client: AsyncClient) -> None:
    response = await client.post("/accounts", json={"currency": "USD"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_create_account_rejects_unsupported_currency(
    client: AsyncClient,
) -> None:
    headers = await register_and_authenticate(client)

    response = await client.post("/accounts", json={"currency": "ZZZ"}, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CURRENCY"


@pytest.mark.parametrize(
    "payload",
    [
        {"currency": "usd"},  # lowercase fails the pattern
        {"currency": "US"},  # too short
        {"currency": "USDD"},  # too long
        {},  # missing currency
        {"currency": "USD", "account_type": "SYSTEM"},  # not client-settable
        {"currency": "USD", "balance_minor": 100_000},  # no opening balance
    ],
)
async def test_create_account_rejects_malformed_payloads(
    client: AsyncClient, payload: dict
) -> None:
    """Note the last two cases.

    `account_type` is rejected rather than ignored, because a SYSTEM
    account is exempt from the overdraft check and letting a caller ask for
    one would let them mint money. `balance_minor` is rejected because
    there is no such thing as an opening balance here: value can only enter
    through a transaction, which is what keeps reconciliation at zero.
    """
    headers = await register_and_authenticate(client)

    response = await client.post("/accounts", json=payload, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


async def test_new_account_balance_is_exactly_zero(client: AsyncClient) -> None:
    """An account with no entries has a balance of 0, not a missing balance."""
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    response = await client.get(f"/accounts/{account['id']}/balance", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["balance_minor"] == 0
    assert body["total_credited_minor"] == 0
    assert body["total_debited_minor"] == 0
    assert body["entry_count"] == 0
    assert body["currency"] == "USD"


# ---------------------------------------------------------------------
# SYSTEM accounts
# ---------------------------------------------------------------------


async def test_system_accounts_are_provisioned_one_per_currency(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Startup creates exactly one SYSTEM boundary account per currency."""
    from app.core.config import get_settings

    settings = get_settings()

    for currency in settings.supported_currencies:
        count = await app_database["accounts"].count_documents(
            {"account_type": "SYSTEM", "currency": currency}
        )
        assert count == 1, f"expected 1 SYSTEM account for {currency}, found {count}"


async def test_a_second_system_account_for_a_currency_is_impossible(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The unique partial index must reject a duplicate boundary account.

    A second SYSTEM account for a currency would be a second untracked
    source of money entering the ledger, since SYSTEM accounts skip the
    overdraft check.
    """
    from pymongo.errors import DuplicateKeyError

    with pytest.raises(DuplicateKeyError):
        await app_database["accounts"].insert_one(
            {
                "owner_id": None,
                "currency": "USD",
                "status": "ACTIVE",
                "account_type": "SYSTEM",
                "label": "A second boundary account",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )


async def test_system_accounts_are_not_reachable_as_owned_accounts(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """A user must not be able to read or inspect the boundary account.

    SYSTEM accounts have a null owner, and every owner-scoped query filters
    on the caller's id, so no caller's id can ever match.
    """
    headers = await register_and_authenticate(client)
    system = await app_database["accounts"].find_one(
        {"account_type": "SYSTEM", "currency": "USD"}
    )
    assert system is not None

    read = await client.get(f"/accounts/{system['_id']}", headers=headers)
    balance = await client.get(f"/accounts/{system['_id']}/balance", headers=headers)

    assert read.status_code == 404
    assert read.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"
    assert balance.status_code == 404


async def test_listing_accounts_excludes_system_accounts(
    client: AsyncClient,
) -> None:
    headers = await register_and_authenticate(client)
    await open_account(client, headers)

    response = await client.get("/accounts", headers=headers)

    assert response.status_code == 200
    account_types = {a["account_type"] for a in response.json()["accounts"]}
    assert account_types == {"USER"}


# ---------------------------------------------------------------------
# Balance computation over known ledger entries
# ---------------------------------------------------------------------


async def test_balance_is_credits_minus_debits(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Insert known entries, assert the computed figure.

    10000 + 2500 credited, 4000 + 1 debited, so the balance is 8499. Note
    the deliberately awkward 1-minor-unit debit: integer arithmetic must be
    exact, with no rounding anywhere.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)
    account_id = account["id"]

    await insert_entry(app_database, account_id, "CREDIT", 10_000)
    await insert_entry(app_database, account_id, "CREDIT", 2_500)
    await insert_entry(app_database, account_id, "DEBIT", 4_000)
    await insert_entry(app_database, account_id, "DEBIT", 1)

    response = await client.get(f"/accounts/{account_id}/balance", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["balance_minor"] == 8_499
    assert body["total_credited_minor"] == 12_500
    assert body["total_debited_minor"] == 4_001
    assert body["entry_count"] == 4


async def test_balance_only_counts_this_accounts_entries(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Entries belonging to another account must not leak into the sum."""
    headers = await register_and_authenticate(client)
    mine = await open_account(client, headers, label="Mine")
    theirs = await open_account(client, headers, label="Theirs")

    await insert_entry(app_database, mine["id"], "CREDIT", 5_000)
    await insert_entry(app_database, theirs["id"], "CREDIT", 999_999)

    response = await client.get(f"/accounts/{mine['id']}/balance", headers=headers)

    assert response.json()["balance_minor"] == 5_000
    assert response.json()["entry_count"] == 1


async def test_balance_can_be_computed_as_negative_for_a_raw_ledger_state(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The aggregation reports what the ledger says, including a negative.

    This is not a hole in the overdraft guarantee. The guarantee is that no
    *transaction* can drive a USER account below zero, and it is enforced
    in the write path (Phase 3) inside the same MongoDB transaction as the
    write. This test reaches around that write path and inserts a debit
    directly, to show that the balance function is an honest reading of the
    entries rather than something that clamps or hides a negative value. A
    reader function that silently floored at zero would mask exactly the
    corruption the reconciliation check exists to find.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    await insert_entry(app_database, account["id"], "DEBIT", 750)

    response = await client.get(f"/accounts/{account['id']}/balance", headers=headers)

    assert response.json()["balance_minor"] == -750


async def test_balance_handles_large_amounts_without_precision_loss(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Money is integer minor units, so large sums stay exact.

    These values are past 2^53, where a float64 can no longer represent
    every integer. If any part of the path used a float, this assertion
    would be off by one or more.
    """
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    big = 9_007_199_254_740_993  # 2**53 + 1
    await insert_entry(app_database, account["id"], "CREDIT", big)
    await insert_entry(app_database, account["id"], "DEBIT", 1)

    response = await client.get(f"/accounts/{account['id']}/balance", headers=headers)

    assert response.json()["balance_minor"] == big - 1


async def test_balance_of_many_entries_is_exact(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """500 entries of 1 minor unit each sum to exactly 500."""
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    await app_database["ledger_entries"].insert_many(
        [
            {
                "transaction_id": ObjectId(),
                "account_id": ObjectId(account["id"]),
                "direction": "CREDIT",
                "amount_minor": 1,
                "signed_amount_minor": 1,
                "currency": "USD",
                "created_at": utc_now(),
            }
            for _ in range(500)
        ]
    )

    response = await client.get(f"/accounts/{account['id']}/balance", headers=headers)

    assert response.json()["balance_minor"] == 500
    assert response.json()["entry_count"] == 500


async def test_balance_requires_authentication(
    client: AsyncClient,
) -> None:
    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    response = await client.get(f"/accounts/{account['id']}/balance")

    assert response.status_code == 401


async def test_cannot_read_another_users_balance(client: AsyncClient) -> None:
    owner_headers = await register_and_authenticate(client, "owner@example.com")
    account = await open_account(client, owner_headers)
    intruder_headers = await register_and_authenticate(client, "intruder@example.com")

    response = await client.get(
        f"/accounts/{account['id']}/balance", headers=intruder_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


async def test_balance_for_unknown_account_is_not_found(client: AsyncClient) -> None:
    headers = await register_and_authenticate(client)

    response = await client.get(f"/accounts/{ObjectId()}/balance", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-an-object-id",
        "12345",
        "000000000000000000000000000000",
        '{"$ne": null}',
        "$where",
    ],
)
async def test_malformed_account_ids_are_rejected_before_reaching_a_query(
    client: AsyncClient, bad_id: str
) -> None:
    """Account ids are constrained to validated ObjectId strings.

    The last two cases are the point: a client must not be able to place a
    query operator where an identifier is expected. Because the path
    parameter is typed as a validated 24-hex string, the value is rejected
    at the edge and never reaches a filter document (RULES.md, MongoDB's
    analogue of SQL injection).
    """
    headers = await register_and_authenticate(client)

    response = await client.get(f"/accounts/{bad_id}/balance", headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MALFORMED_REQUEST"


# ---------------------------------------------------------------------
# Validator enforcement on ledger entries
# ---------------------------------------------------------------------


async def test_ledger_entry_with_mismatched_sign_is_rejected_by_the_database(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The sign convention is enforced by MongoDB, not just by Python.

    A DEBIT whose `signed_amount_minor` is positive would inflate a balance
    and break reconciliation. The collection validator's `$expr` clause
    rejects it even on a direct driver insert that bypasses every
    application model.
    """
    from pymongo.errors import WriteError

    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].insert_one(
            {
                "transaction_id": ObjectId(),
                "account_id": ObjectId(account["id"]),
                "direction": "DEBIT",
                "amount_minor": 500,
                "signed_amount_minor": 500,  # wrong: a debit must be negative
                "currency": "USD",
                "created_at": utc_now(),
            }
        )


async def test_ledger_entry_with_zero_or_negative_magnitude_is_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    from pymongo.errors import WriteError

    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)

    for amount in (0, -100):
        with pytest.raises(WriteError):
            await app_database["ledger_entries"].insert_one(
                {
                    "transaction_id": ObjectId(),
                    "account_id": ObjectId(account["id"]),
                    "direction": "CREDIT",
                    "amount_minor": amount,
                    "signed_amount_minor": amount,
                    "currency": "USD",
                    "created_at": utc_now(),
                }
            )


async def test_two_entries_of_the_same_direction_for_one_transaction_are_rejected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """One transaction gets at most one DEBIT and one CREDIT.

    Enforced by a unique index on `(transaction_id, direction)`. A bug that
    replayed one side of a pair would violate it and fail, rather than
    quietly unbalancing the ledger.
    """
    from pymongo.errors import DuplicateKeyError

    headers = await register_and_authenticate(client)
    account = await open_account(client, headers)
    transaction_id = ObjectId()

    await insert_entry(
        app_database, account["id"], "DEBIT", 100, transaction_id=transaction_id
    )

    with pytest.raises(DuplicateKeyError):
        await insert_entry(
            app_database, account["id"], "DEBIT", 100, transaction_id=transaction_id
        )
