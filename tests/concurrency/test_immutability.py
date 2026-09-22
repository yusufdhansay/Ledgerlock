"""Ledger entry immutability.

PRD.md goal 4: once written, an entry can never be altered or deleted
through the application.

TASK.md asks for every available path to be attempted. This file does that
and is explicit about how strong each result is, because "the API has no
such route" and "the database refuses the write" are very different claims
and conflating them would overstate the guarantee.

What is proven here:

1. **No mutating route exists.** Checked against the generated OpenAPI
   schema rather than by trying a list of guessed URLs, so a route added
   later is caught automatically. Every plausible verb and path is also
   attempted over HTTP and must fail.
2. **The database rejects any write that breaks an entry's shape or its
   sign convention**, including on update, and including from a direct
   driver call that bypasses all application code. That is the
   `$jsonSchema` + `$expr` collection validator.
3. **A transaction cannot gain a third entry or a second entry on the same
   side**, because of the unique index on `(transaction_id, direction)`.

What is NOT proven *here*, and where it is proven instead:

A client holding unrestricted database credentials can still issue an update
that keeps the document shape and sign convention valid, for example changing
`amount_minor` and `signed_amount_minor` together. A `$jsonSchema` validator
constrains the resulting document; it cannot compare it against the previous
version. This file runs against the default local profile, whose MongoDB has
no authentication, so its own connection is effectively a superuser and that
update succeeds. There is a test asserting exactly that, so the boundary is
explicit rather than glossed over.

That gap is closed by privilege separation in the hardened profile: a role
granting `find` and `insert` on `ledger_entries` and `transactions` but not
`update` or `remove`. Verified by `scripts/verify_append_only.sh` against
`docker-compose.hardened.yml`, where all nine mutation attempts (update,
replace, delete, drop collection, drop database, and the same against
`transactions`) are refused with Unauthorized while insert and find still
work and the full end-to-end flow still passes. Captured output:
`tests/concurrency/results/phase8-append-only-privileges-*.txt`.
"""

from __future__ import annotations

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError, WriteError

from app.main import create_app
from tests.concurrency.conftest import (
    fresh_key,
    read_balance,
    submit_transfer,
)

pytestmark = pytest.mark.concurrency


@pytest.fixture
async def committed_entry(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> dict:
    """A real, committed ledger entry to attempt to tamper with."""
    response = await submit_transfer(
        client,
        funded_account["headers"],
        funded_account["source_id"],
        funded_account["destination_id"],
        2_000,
        idempotency_key=fresh_key("immutability"),
    )
    assert response.status_code == 201, response.text
    transaction_id = ObjectId(response.json()["id"])

    entry = await app_database["ledger_entries"].find_one(
        {"transaction_id": transaction_id, "direction": "DEBIT"}
    )
    assert entry is not None
    return {
        "entry": entry,
        "transaction_id": transaction_id,
        **funded_account,
    }


# ---------------------------------------------------------------------
# 1. The API exposes no way to mutate ledger data
# ---------------------------------------------------------------------


def test_no_route_can_mutate_ledger_entries_or_transactions() -> None:
    """Checked against the OpenAPI schema, so new routes are caught too.

    Asserting against the schema rather than a hand-written list of URLs
    means that if someone later adds `PATCH /ledger-entries/{id}`, this test
    fails without anyone remembering to update it.
    """
    import os

    os.environ.setdefault(
        "JWT_SECRET_KEY", "test-only-signing-key-for-schema-inspection-0123456789"
    )
    schema = create_app().openapi()

    mutating_methods = {"put", "patch", "delete"}
    offenders: list[str] = []

    for path, operations in schema["paths"].items():
        for method in operations:
            if method.lower() in mutating_methods:
                offenders.append(f"{method.upper()} {path}")
            # A POST to a ledger-entry path would also be a way in.
            if method.lower() == "post" and "ledger" in path.lower():
                offenders.append(f"{method.upper()} {path}")

    print(f"\nROUTE AUDIT: paths = {sorted(schema['paths'])}")
    print(f"ROUTE AUDIT: mutating routes found = {offenders or 'none'}")

    assert offenders == [], (
        f"the API exposes mutating routes: {offenders}. Ledger entries are "
        "append-only; corrections are made by a compensating transaction, "
        "never by editing history."
    )


@pytest.mark.parametrize(
    ("method", "path_template"),
    [
        ("PUT", "/ledger-entries/{entry_id}"),
        ("PATCH", "/ledger-entries/{entry_id}"),
        ("DELETE", "/ledger-entries/{entry_id}"),
        ("PUT", "/ledger_entries/{entry_id}"),
        ("DELETE", "/transactions/{transaction_id}"),
        ("PATCH", "/transactions/{transaction_id}"),
        ("PUT", "/transactions/{transaction_id}"),
        ("DELETE", "/accounts/{account_id}/balance"),
        ("PUT", "/accounts/{account_id}/balance"),
        ("PATCH", "/accounts/{account_id}/balance"),
    ],
)
async def test_mutating_requests_over_http_are_refused(
    client: AsyncClient, committed_entry: dict, method: str, path_template: str
) -> None:
    """Every plausible mutating request must be refused.

    Belt and braces alongside the schema audit: a route could in principle
    exist without appearing in the schema.
    """
    path = path_template.format(
        entry_id=str(committed_entry["entry"]["_id"]),
        transaction_id=str(committed_entry["transaction_id"]),
        account_id=committed_entry["source_id"],
    )

    response = await client.request(
        method,
        path,
        headers=committed_entry["headers"],
        json={"amount_minor": 1},
    )

    assert response.status_code in (404, 405), (
        f"{method} {path} returned {response.status_code}; a mutating "
        "endpoint on ledger data must not exist"
    )


async def test_the_balance_endpoint_is_read_only_and_cannot_be_set(
    client: AsyncClient, committed_entry: dict
) -> None:
    """There is no way to write a balance, because balance is not stored.

    Included because "set the balance directly" is the most natural thing an
    attacker or a careless client would try, and in a system with a stored
    balance field it is the thing that works.
    """
    before = await read_balance(
        client, committed_entry["headers"], committed_entry["source_id"]
    )

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await client.request(
            method,
            f"/accounts/{committed_entry['source_id']}/balance",
            headers=committed_entry["headers"],
            json={"balance_minor": 999_999_999},
        )
        assert response.status_code in (404, 405), method

    after = await read_balance(
        client, committed_entry["headers"], committed_entry["source_id"]
    )
    assert after == before


# ---------------------------------------------------------------------
# 2. The database rejects writes that break an entry's invariants
# ---------------------------------------------------------------------


async def test_direct_update_breaking_the_sign_convention_is_rejected(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """Tampering with the amount alone is refused by the database.

    This bypasses every line of application code and still fails, because
    the collection validator's `$expr` clause requires
    `signed_amount_minor` to equal `±amount_minor` according to
    `direction`. Changing one without the other cannot be written.
    """
    entry_id = committed_entry["entry"]["_id"]

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$set": {"amount_minor": 999_999}}
        )

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$set": {"signed_amount_minor": 999_999}}
        )

    unchanged = await app_database["ledger_entries"].find_one({"_id": entry_id})
    assert unchanged is not None
    assert unchanged["amount_minor"] == committed_entry["entry"]["amount_minor"]
    assert (
        unchanged["signed_amount_minor"]
        == committed_entry["entry"]["signed_amount_minor"]
    )

    print(
        "\nVALIDATOR TEST: direct driver updates to amount_minor and to "
        "signed_amount_minor were both rejected by the collection validator"
    )


async def test_direct_update_flipping_the_direction_is_rejected(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """Turning a debit into a credit is refused.

    The most valuable single tamper available: flipping a debit to a credit
    would double an account's balance relative to the truth. The sign would
    no longer match the direction, so the validator rejects it.
    """
    entry_id = committed_entry["entry"]["_id"]

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$set": {"direction": "CREDIT"}}
        )

    unchanged = await app_database["ledger_entries"].find_one({"_id": entry_id})
    assert unchanged is not None
    assert unchanged["direction"] == "DEBIT"


async def test_direct_update_adding_an_unexpected_field_is_rejected(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """`additionalProperties: false` blocks smuggling data into an entry."""
    entry_id = committed_entry["entry"]["_id"]

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$set": {"reversed_by": "someone"}}
        )


async def test_direct_update_removing_a_required_field_is_rejected(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """An entry cannot be hollowed out into a shape that sums differently."""
    entry_id = committed_entry["entry"]["_id"]

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$unset": {"signed_amount_minor": ""}}
        )

    with pytest.raises(WriteError):
        await app_database["ledger_entries"].update_one(
            {"_id": entry_id}, {"$unset": {"transaction_id": ""}}
        )


async def test_a_transaction_cannot_gain_a_second_entry_on_the_same_side(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """The unique index blocks injecting an extra debit or credit.

    Without this, an attacker with database access could add a second CREDIT
    to an existing transaction and inflate an account while leaving each
    individual entry perfectly valid.
    """
    entry = committed_entry["entry"]

    extra_debit = {
        "transaction_id": entry["transaction_id"],
        "account_id": entry["account_id"],
        "direction": "DEBIT",
        "amount_minor": 1,
        "signed_amount_minor": -1,
        "currency": entry["currency"],
        "created_at": entry["created_at"],
    }

    with pytest.raises(DuplicateKeyError):
        await app_database["ledger_entries"].insert_one(extra_debit)

    print(
        "\nINDEX TEST: injecting a second DEBIT into an existing transaction "
        "was rejected by uq_one_entry_per_direction_per_transaction"
    )


async def test_a_self_consistent_update_succeeds_without_privilege_separation(
    app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """Scopes exactly what the validators do and do not prevent.

    A caller with *unrestricted* database credentials can rewrite an entry if
    the result stays self-consistent, because a `$jsonSchema` validator judges
    only the resulting document and cannot compare it with the previous one.
    This test runs under the default local profile, whose MongoDB has no
    authentication, so the test's own connection is effectively a superuser.

    That gap is closed in the hardened profile, and this test's continued
    passing is what makes the distinction precise rather than hand-waved:

    * validators alone: a self-consistent update succeeds (asserted here)
    * plus privilege separation: the same update fails with Unauthorized,
      because the application's role holds `insert` but not `update` on
      `ledger_entries`. Verified by `scripts/verify_append_only.sh` against
      `docker-compose.hardened.yml`; captured output is in
      `tests/concurrency/results/phase8-append-only-privileges-*.txt`.

    Note what the tamper does not escape even here: it unbalances the ledger,
    and reconciliation finds it. Detection is not prevention, but it is the
    difference between silent corruption and loud corruption.
    """
    from app.services import reconciliation as reconciliation_service

    entry_id = committed_entry["entry"]["_id"]

    result = await app_database["ledger_entries"].update_one(
        {"_id": entry_id},
        {"$set": {"amount_minor": 9_999, "signed_amount_minor": -9_999}},
    )
    assert result.modified_count == 1, (
        "the update was refused. If this suite is now being run against the "
        "hardened profile, that is the correct outcome and this test belongs "
        "with the append-only verification instead."
    )

    report = await reconciliation_service.reconcile(app_database)

    print(
        "\nVALIDATOR SCOPE TEST: with unrestricted credentials, a "
        "self-consistent update DID succeed (a validator judges only the "
        "resulting document). The hardened profile refuses it outright."
    )
    print(
        f"VALIDATOR SCOPE TEST: reconciliation caught it -> healthy="
        f"{report.healthy}, net_signed_minor={report.net_signed_minor}, "
        f"unbalanced_transaction_groups={report.unbalanced_transaction_groups}"
    )

    assert not report.healthy, (
        "the tamper must at least be detectable; reconciliation reported the "
        "ledger as sound, which would make the corruption silent"
    )
    assert report.net_signed_minor != 0
    assert report.unbalanced_transaction_groups >= 1


# ---------------------------------------------------------------------
# 3. The application's own path never mutates
# ---------------------------------------------------------------------


async def test_a_committed_entry_is_untouched_by_later_activity(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, committed_entry: dict
) -> None:
    """Normal operation only ever appends.

    Runs further transfers and re-reads the original entry byte for byte.
    Any in-place update by the application, for instance a running balance
    written onto the entry, would show up as a difference here.
    """
    original = dict(committed_entry["entry"])
    headers = committed_entry["headers"]

    for _ in range(5):
        response = await submit_transfer(
            client,
            headers,
            committed_entry["source_id"],
            committed_entry["destination_id"],
            100,
        )
        assert response.status_code == 201, response.text

    reread = await app_database["ledger_entries"].find_one({"_id": original["_id"]})

    assert reread == original, (
        "a committed ledger entry changed while later transfers were "
        "processed; the ledger is not append-only"
    )

    print(
        "\nAPPEND-ONLY TEST: the original entry is byte-identical after 5 "
        "further transfers on the same accounts"
    )


async def test_corrections_are_made_by_compensating_transaction(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """The supported way to undo a transfer: send it back.

    Demonstrates the actual remedy for a mistaken payment. The original
    entries stay exactly where they are, two new ones are appended, and both
    accounts end up where they started. History is added to, never rewritten,
    which is the property that makes a ledger auditable.
    """
    headers = funded_account["headers"]
    source_id = funded_account["source_id"]
    destination_id = funded_account["destination_id"]

    mistake = await submit_transfer(client, headers, source_id, destination_id, 3_000)
    assert mistake.status_code == 201
    mistake_id = ObjectId(mistake.json()["id"])

    correction = await submit_transfer(
        client, headers, destination_id, source_id, 3_000
    )
    assert correction.status_code == 201, correction.text

    original_entries = (
        await app_database["ledger_entries"]
        .find({"transaction_id": mistake_id})
        .to_list(length=5)
    )
    source_balance = await read_balance(client, headers, source_id)
    destination_balance = await read_balance(client, headers, destination_id)
    total_entries = await app_database["ledger_entries"].count_documents({})

    print(
        f"\nCOMPENSATION TEST: entries for the original transfer still "
        f"present = {len(original_entries)}"
    )
    print(
        f"COMPENSATION TEST: total entries = {total_entries} "
        f"(2 funding + 2 mistake + 2 correction)"
    )
    print(
        f"COMPENSATION TEST: source = {source_balance}, destination = "
        f"{destination_balance}"
    )

    assert len(original_entries) == 2, "the mistaken entries were altered"
    assert total_entries == 6
    assert source_balance == funded_account["opening_balance"]
    assert destination_balance == 0
