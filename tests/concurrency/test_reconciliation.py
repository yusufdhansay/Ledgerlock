"""System-wide reconciliation: the ledger nets to exactly zero.

PRD.md goal 3: the sum of every ledger entry in the entire system always
nets to zero, checked automatically after load, not just claimed.

This is the most valuable check in the project, because it needs no
knowledge of what happened. Whatever sequence of transfers ran, however
they interleaved, whatever retried, the total must be 0. Not approximately
0: the amounts are integers, so there is no rounding tolerance to hide
behind.

The tests run randomised batches of concurrent transfers and then assert
the invariant. They also verify the check is not vacuous, by corrupting the
ledger deliberately and confirming each corruption is caught.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.common import utc_now
from app.services import reconciliation as reconciliation_service
from tests.concurrency.conftest import (
    fund,
    open_account,
    read_balance,
    register_and_authenticate,
    submit_transfer,
    summarise,
)

pytestmark = pytest.mark.concurrency

# Fixed seed so a failure is reproducible. Randomised in shape, not in
# whether it can be re-run.
RANDOM_SEED = 20260922


async def test_an_empty_ledger_reconciles(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Zero entries sum to zero. The degenerate case must not error."""
    report = await reconciliation_service.reconcile(app_database)

    assert report.balanced is True
    assert report.healthy is True
    assert report.net_signed_minor == 0
    assert report.total_entries == 0


async def test_a_randomised_batch_of_concurrent_transfers_reconciles(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The headline reconciliation test.

    Eight accounts across two currencies, funded to random amounts, then 120
    concurrent transfers of random amounts between random pairs. Many will be
    rejected for insufficient funds or currency mismatch, and that is the
    point: a messy mixture of commits and rollbacks is exactly the condition
    under which a half-written pair would appear.

    Whatever the outcome of each individual request, the ledger must net to
    exactly zero, no entry may belong to a missing transaction, every
    transaction must have exactly one debit and one credit, and no USER
    account may be negative.
    """
    random.seed(RANDOM_SEED)

    headers = await register_and_authenticate(client)
    accounts: list[tuple[str, str]] = []

    for index in range(6):
        account_id = await open_account(
            client, headers, currency="USD", label=f"USD {index}"
        )
        await fund(client, headers, account_id, random.randrange(5_000, 50_000))
        accounts.append((account_id, "USD"))

    for index in range(2):
        account_id = await open_account(
            client, headers, currency="EUR", label=f"EUR {index}"
        )
        await fund(
            client,
            headers,
            account_id,
            random.randrange(5_000, 50_000),
            currency="EUR",
        )
        accounts.append((account_id, "EUR"))

    transfers = []
    for _ in range(120):
        source_id, source_currency = random.choice(accounts)
        destination_id, _ = random.choice(accounts)
        if destination_id == source_id:
            continue
        transfers.append(
            submit_transfer(
                client,
                headers,
                source_id,
                destination_id,
                random.randrange(1, 9_000),
                currency=source_currency,
            )
        )

    responses = await asyncio.gather(*transfers)
    report = await reconciliation_service.reconcile(app_database)

    print(
        f"\nRECONCILIATION TEST: {len(transfers)} concurrent transfers across "
        f"{len(accounts)} accounts in 2 currencies (seed {RANDOM_SEED})"
    )
    print(f"RECONCILIATION TEST: outcomes = {summarise(responses)}")
    print(f"RECONCILIATION TEST: total ledger entries = {report.total_entries}")
    print(f"RECONCILIATION TEST: total transactions   = {report.total_transactions}")
    print(
        f"RECONCILIATION TEST: net_signed_minor     = "
        f"{report.net_signed_minor} (must be exactly 0)"
    )
    print(
        f"RECONCILIATION TEST: per currency         = "
        f"{report.per_currency_net_minor}"
    )
    print(
        f"RECONCILIATION TEST: entries without a transaction = "
        f"{report.entries_without_transaction}"
    )
    print(
        f"RECONCILIATION TEST: unbalanced transaction groups = "
        f"{report.unbalanced_transaction_groups}"
    )
    print(
        f"RECONCILIATION TEST: negative USER accounts        = "
        f"{report.negative_user_accounts}"
    )
    print(f"RECONCILIATION TEST: healthy = {report.healthy}")

    assert report.net_signed_minor == 0, (
        f"the ledger does not net to zero: {report.net_signed_minor}. Some "
        "transaction wrote one side of its pair without the other."
    )
    assert all(net == 0 for net in report.per_currency_net_minor.values())
    assert report.entries_without_transaction == 0
    assert report.unbalanced_transaction_groups == 0
    assert report.negative_user_accounts == []
    assert report.healthy is True
    # Two entries per committed transaction, no more and no fewer.
    assert report.total_entries == report.total_transactions * 2

    # Every USER account balance is non-negative, checked independently of
    # the report.
    for account_id, _ in accounts:
        assert await read_balance(client, headers, account_id) >= 0


async def test_reconciliation_holds_after_heavy_contention_on_one_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The retry path is where a half-written pair would be most likely.

    Drives 80 concurrent transfers out of a single account, so nearly every
    transaction hits a write conflict and is replayed. If a retry could
    commit one entry from an earlier attempt alongside one from a later
    attempt, this is where it would show.
    """
    headers = await register_and_authenticate(client)
    source_id = await open_account(client, headers, label="Hot account")
    destination_id = await open_account(client, headers, label="Sink")
    await fund(client, headers, source_id, 40_000)

    responses = await asyncio.gather(
        *(
            submit_transfer(client, headers, source_id, destination_id, 1_000)
            for _ in range(80)
        )
    )

    report = await reconciliation_service.reconcile(app_database)
    source_balance = await read_balance(client, headers, source_id)
    destination_balance = await read_balance(client, headers, destination_id)

    print(f"\nCONTENTION RECONCILIATION: outcomes = {summarise(responses)}")
    print(
        f"CONTENTION RECONCILIATION: source = {source_balance}, "
        f"destination = {destination_balance}"
    )
    print(
        f"CONTENTION RECONCILIATION: net_signed_minor = "
        f"{report.net_signed_minor}, healthy = {report.healthy}"
    )

    assert sum(1 for r in responses if r.status_code == 201) == 40
    assert source_balance == 0
    assert destination_balance == 40_000
    assert report.net_signed_minor == 0
    assert report.healthy is True
    assert report.unbalanced_transaction_groups == 0


async def test_the_system_account_balance_equals_total_user_holdings(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """A second, independent statement of the same invariant.

    The SYSTEM boundary account's negative balance must equal, exactly, the
    total held across every USER account in that currency. That is what
    "nets to zero" means in bookkeeping terms rather than arithmetic terms,
    and it is a useful cross-check because it would catch a sign error that
    happened to cancel out globally.
    """
    from app.services import account_service, balance_service

    headers = await register_and_authenticate(client)
    user_accounts = []
    funded_amounts = [12_000, 7_500, 33_333]

    for index, amount in enumerate(funded_amounts):
        account_id = await open_account(client, headers, label=f"Holder {index}")
        await fund(client, headers, account_id, amount)
        user_accounts.append(account_id)

    # Move money around; it must not change the total held.
    await submit_transfer(client, headers, user_accounts[0], user_accounts[1], 5_000)
    await submit_transfer(client, headers, user_accounts[1], user_accounts[2], 1_250)

    system_account = await account_service.get_system_account("USD")
    assert system_account is not None
    system_balance = (
        await balance_service.compute_balance(system_account["_id"])
    ).balance_minor

    total_user_holdings = 0
    for account_id in user_accounts:
        total_user_holdings += await read_balance(client, headers, account_id)

    print(f"\nBOUNDARY TEST: total funded = {sum(funded_amounts)}")
    print(f"BOUNDARY TEST: SYSTEM account balance = {system_balance}")
    print(f"BOUNDARY TEST: total USER holdings    = {total_user_holdings}")

    assert total_user_holdings == sum(funded_amounts)
    assert system_balance == -total_user_holdings, (
        "the boundary account's negative balance must equal total user "
        "holdings exactly"
    )

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy is True


async def test_reconciliation_is_self_consistent_while_writes_are_committing(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Regression test for a bug the Phase 6 load test found.

    The check is several aggregations plus a count. Originally each ran as an
    independent read, so under concurrent writes they observed the database
    at slightly different instants and disagreed with one another: the entry
    aggregation returned 3086 entries, two more transactions committed, and
    the transaction count then returned 1545, so the report claimed
    `total_entries != total_transactions * 2` while the ledger was in fact
    perfectly sound.

    A checker that raises false alarms under load is worse than no checker,
    because it teaches you to ignore it. The fix is to run every read inside
    one session with `readConcern: "snapshot"`.

    This test reconciles repeatedly *while* transfers are committing and
    requires every single report to be internally consistent. Before the fix
    it fails; after it, the invariant holds throughout.
    """
    headers = await register_and_authenticate(client)
    source_id = await open_account(client, headers, label="Writer source")
    destination_id = await open_account(client, headers, label="Writer sink")
    await fund(client, headers, source_id, 1_000_000)

    stop = asyncio.Event()

    async def keep_transferring() -> int:
        committed = 0
        while not stop.is_set():
            response = await submit_transfer(
                client, headers, source_id, destination_id, 10
            )
            if response.status_code == 201:
                committed += 1
        return committed

    async def keep_reconciling() -> list[dict]:
        reports = []
        for _ in range(12):
            report = await reconciliation_service.reconcile(app_database)
            reports.append(report.model_dump())
            await asyncio.sleep(0)
        stop.set()
        return reports

    writers = [asyncio.create_task(keep_transferring()) for _ in range(4)]
    reports = await keep_reconciling()
    committed_counts = await asyncio.gather(*writers)

    inconsistent = [
        r
        for r in reports
        if r["total_entries"] != r["total_transactions"] * 2
        or r["net_signed_minor"] != 0
        or not r["healthy"]
    ]

    print(
        f"\nSNAPSHOT TEST: {sum(committed_counts)} transfers committed during "
        f"{len(reports)} reconciliation runs"
    )
    print(
        "SNAPSHOT TEST: entries/transactions per report = "
        + ", ".join(f"{r['total_entries']}/{r['total_transactions']}" for r in reports)
    )
    print(f"SNAPSHOT TEST: internally inconsistent reports = {len(inconsistent)}")

    assert sum(committed_counts) > 0, "no writes happened, so nothing was tested"
    assert inconsistent == [], (
        "reconciliation produced an internally inconsistent report while "
        f"writes were committing: {inconsistent[:2]}"
    )


# ---------------------------------------------------------------------
# Is the check actually capable of failing?
# ---------------------------------------------------------------------


async def test_reconciliation_detects_a_missing_credit(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Deleting one side of a pair must be caught.

    A reconciliation check that cannot fail proves nothing, so each failure
    mode is induced deliberately. This one simulates the exact corruption the
    atomic write exists to prevent: a debit with no matching credit.
    """
    headers = funded_account["headers"]
    response = await submit_transfer(
        client,
        headers,
        funded_account["source_id"],
        funded_account["destination_id"],
        1_500,
    )
    transaction_id = ObjectId(response.json()["id"])

    await app_database["ledger_entries"].delete_one(
        {"transaction_id": transaction_id, "direction": "CREDIT"}
    )

    report = await reconciliation_service.reconcile(app_database)

    print(
        f"\nDETECTION: missing credit -> net_signed_minor="
        f"{report.net_signed_minor}, unbalanced_groups="
        f"{report.unbalanced_transaction_groups}, healthy={report.healthy}"
    )

    assert report.net_signed_minor == -1_500
    assert report.balanced is False
    assert report.healthy is False
    assert report.unbalanced_transaction_groups == 1


async def test_reconciliation_detects_an_entry_with_no_transaction(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """An orphaned pair that nets to zero must still be caught.

    Two entries that cancel out, belonging to a transaction that does not
    exist. The global net stays 0, so this is precisely the corruption a
    sum-to-zero check alone would miss.
    """
    orphan_transaction_id = ObjectId()
    await app_database["ledger_entries"].insert_many(
        [
            {
                "transaction_id": orphan_transaction_id,
                "account_id": ObjectId(funded_account["source_id"]),
                "direction": "DEBIT",
                "amount_minor": 500,
                "signed_amount_minor": -500,
                "currency": "USD",
                "created_at": utc_now(),
            },
            {
                "transaction_id": orphan_transaction_id,
                "account_id": ObjectId(funded_account["destination_id"]),
                "direction": "CREDIT",
                "amount_minor": 500,
                "signed_amount_minor": 500,
                "currency": "USD",
                "created_at": utc_now(),
            },
        ]
    )

    report = await reconciliation_service.reconcile(app_database)

    print(
        f"\nDETECTION: orphaned pair -> net_signed_minor="
        f"{report.net_signed_minor} (still zero), "
        f"entries_without_transaction={report.entries_without_transaction}, "
        f"healthy={report.healthy}"
    )

    assert report.net_signed_minor == 0
    assert report.balanced is True, "the net alone does not reveal this"
    assert report.entries_without_transaction == 2
    assert (
        report.healthy is False
    ), "healthy must fail even though the ledger nets to zero"


async def test_reconciliation_detects_a_negative_user_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """An overdrawn account must be caught even if the books balance.

    A debit and its matching credit, written correctly, that happen to take
    an account below zero. Perfectly balanced, and completely wrong. This is
    the overdraft guarantee being checked globally rather than per request.
    """
    overdraw_transaction_id = ObjectId()
    await app_database["ledger_entries"].insert_many(
        [
            {
                "transaction_id": overdraw_transaction_id,
                "account_id": ObjectId(funded_account["destination_id"]),
                "direction": "DEBIT",
                "amount_minor": 4_000,
                "signed_amount_minor": -4_000,
                "currency": "USD",
                "created_at": utc_now(),
            },
            {
                "transaction_id": overdraw_transaction_id,
                "account_id": ObjectId(funded_account["source_id"]),
                "direction": "CREDIT",
                "amount_minor": 4_000,
                "signed_amount_minor": 4_000,
                "currency": "USD",
                "created_at": utc_now(),
            },
        ]
    )
    await app_database["transactions"].insert_one(
        {
            "_id": overdraw_transaction_id,
            "idempotency_key": "manually-injected-overdraw",
            "kind": "TRANSFER",
            "status": "COMPLETED",
            "source_account_id": ObjectId(funded_account["destination_id"]),
            "destination_account_id": ObjectId(funded_account["source_id"]),
            "amount_minor": 4_000,
            "currency": "USD",
            "created_at": utc_now(),
        }
    )

    report = await reconciliation_service.reconcile(app_database)

    print(
        f"\nDETECTION: overdrawn account -> net_signed_minor="
        f"{report.net_signed_minor} (zero), balanced={report.balanced}, "
        f"negative_user_accounts={report.negative_user_accounts}, "
        f"healthy={report.healthy}"
    )

    assert report.net_signed_minor == 0
    assert report.balanced is True
    assert report.unbalanced_transaction_groups == 0
    assert funded_account["destination_id"] in report.negative_user_accounts
    assert report.healthy is False


async def test_reconciliation_detects_a_per_currency_imbalance_that_nets_out(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Two errors in different currencies that cancel globally.

    +700 EUR and -700 USD sum to zero across the whole ledger while both
    currencies are individually wrong. This is why the net is also computed
    per currency.
    """
    await app_database["ledger_entries"].insert_many(
        [
            {
                "transaction_id": ObjectId(),
                "account_id": ObjectId(funded_account["source_id"]),
                "direction": "DEBIT",
                "amount_minor": 700,
                "signed_amount_minor": -700,
                "currency": "USD",
                "created_at": utc_now(),
            },
            {
                "transaction_id": ObjectId(),
                "account_id": ObjectId(funded_account["destination_id"]),
                "direction": "CREDIT",
                "amount_minor": 700,
                "signed_amount_minor": 700,
                "currency": "EUR",
                "created_at": utc_now(),
            },
        ]
    )

    report = await reconciliation_service.reconcile(app_database)

    print(
        f"\nDETECTION: cross-currency cancellation -> net_signed_minor="
        f"{report.net_signed_minor} (zero), per currency="
        f"{report.per_currency_net_minor}, balanced={report.balanced}"
    )

    assert report.net_signed_minor == 0
    assert report.per_currency_net_minor["USD"] == -700
    assert report.per_currency_net_minor["EUR"] == 700
    assert report.balanced is False, (
        "a global net of zero must not be reported as balanced when a "
        "currency is individually out"
    )
    assert report.healthy is False


# ---------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------


async def test_the_reconciliation_endpoint_reports_a_healthy_ledger(
    client: AsyncClient, funded_account: dict
) -> None:
    """PRD.md asks for an endpoint as well as a test.

    The endpoint is what makes the invariant checkable against a running
    deployment, which is how it gets used after the Phase 6 load test.
    """
    headers = funded_account["headers"]
    await submit_transfer(
        client,
        headers,
        funded_account["source_id"],
        funded_account["destination_id"],
        2_000,
    )

    response = await client.get("/reconciliation", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()

    print(f"\nENDPOINT TEST: GET /reconciliation -> {body}")

    assert body["healthy"] is True
    assert body["balanced"] is True
    assert body["net_signed_minor"] == 0
    assert body["total_entries"] == 4
    assert body["total_transactions"] == 2
    assert body["negative_user_accounts"] == []
    assert body["checked_at"]


async def test_the_reconciliation_endpoint_requires_authentication(
    client: AsyncClient,
) -> None:
    response = await client.get("/reconciliation")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
