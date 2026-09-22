"""Idempotency under concurrent duplicate submission.

PRD.md goal 2: resubmitting the same transaction (client retry, network
duplicate) never applies it twice.

The sequential case is covered in `tests/unit/test_ledger_service.py`. What
these tests add is the case that actually happens in production: a client
whose request timed out retries while the original is still in flight, or a
proxy replays a request, so several identical submissions are being
processed at the same instant. That is the case an application-level
"have I seen this key?" check cannot handle, because every one of the
duplicates reads "no" before any of them writes.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.models.common import utc_now
from app.services import reconciliation as reconciliation_service
from tests.concurrency.conftest import (
    error_code_of,
    fresh_key,
    fund,
    open_account,
    read_balance,
    register_and_authenticate,
    submit_transfer,
    summarise,
)

pytestmark = pytest.mark.concurrency


async def test_twenty_concurrent_submissions_of_one_key_apply_once(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """20 identical requests, fired together, sharing one idempotency key.

    Exactly one may commit. The assertions are deliberately layered: one
    HTTP success, one transaction document, one pair of ledger entries, and
    the money moved once. Checking only the HTTP statuses would miss a
    system that returned one 201 while writing two sets of entries.
    """
    headers = funded_account["headers"]
    shared_key = fresh_key("concurrent-duplicate")
    attempts = 20

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                2_500,
                idempotency_key=shared_key,
            )
            for _ in range(attempts)
        )
    )

    created = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code != 201]

    transaction_count = await app_database["transactions"].count_documents(
        {"idempotency_key": shared_key}
    )
    entry_count = await app_database["ledger_entries"].count_documents({})
    # Two entries from the funding that set up the account, two from this
    # transfer. Anything more means a duplicate was applied.
    destination_balance = await read_balance(
        client, headers, funded_account["destination_id"]
    )

    print(f"\nIDEMPOTENCY TEST: {attempts} concurrent submissions of one key")
    print(f"IDEMPOTENCY TEST: outcomes = {summarise(responses)}")
    print(
        f"IDEMPOTENCY TEST: transaction documents for that key = "
        f"{transaction_count} (expected 1)"
    )
    print(
        f"IDEMPOTENCY TEST: total ledger entries = {entry_count} "
        f"(expected 4: 2 from funding, 2 from the single transfer)"
    )
    print(
        f"IDEMPOTENCY TEST: destination balance = {destination_balance} "
        f"(expected 2500, i.e. applied once)"
    )

    assert len(created) == 1, (
        f"{len(created)} submissions were accepted; the transfer was applied "
        f"more than once"
    )
    assert len(rejected) == attempts - 1
    assert all(
        error_code_of(r) == "DUPLICATE_SUBMISSION" for r in rejected
    ), f"unexpected rejection reasons: {summarise(rejected)}"
    assert transaction_count == 1
    assert entry_count == 4
    assert destination_balance == 2_500

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


async def test_exactly_one_pair_of_ledger_entries_exists_afterwards(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """The entry pair is inspected directly, not inferred from the balance.

    A balance of 2500 could also be produced by two 1250 pairs. This checks
    the shape of the ledger: one transaction id, one DEBIT, one CREDIT.
    """
    headers = funded_account["headers"]
    shared_key = fresh_key("entry-shape")

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                3_333,
                idempotency_key=shared_key,
            )
            for _ in range(15)
        )
    )

    transaction = await app_database["transactions"].find_one(
        {"idempotency_key": shared_key}
    )
    assert transaction is not None

    entries = (
        await app_database["ledger_entries"]
        .find({"transaction_id": transaction["_id"]})
        .to_list(length=20)
    )

    print(f"\nENTRY SHAPE TEST: outcomes = {summarise(responses)}")
    print(f"ENTRY SHAPE TEST: entries for the transaction = {len(entries)}")
    print(
        "ENTRY SHAPE TEST: directions = " f"{sorted(e['direction'] for e in entries)}"
    )

    assert len(entries) == 2
    assert sorted(e["direction"] for e in entries) == ["CREDIT", "DEBIT"]
    assert sum(e["signed_amount_minor"] for e in entries) == 0
    assert {e["amount_minor"] for e in entries} == {3_333}


class _DetectionCollector(logging.Handler):
    """Captures the `detected_by` field that `ledger_service` logs.

    `ledger_service` records how each duplicate was caught: `pre_check`
    means the read at the top of the transaction saw an already-committed
    transaction with that key; `unique_index` means the insert lost a race
    and `uq_idempotency_key` rejected it. Reading that out of the logs lets
    these tests assert which mechanism actually did the work, rather than
    only that the outcome was right.
    """

    def __init__(self) -> None:
        super().__init__()
        self.detected_by: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        value = getattr(record, "detected_by", None)
        if value is not None:
            self.detected_by.append(str(value))


async def test_control_without_the_unique_index_duplicates_are_applied_twice(
    mongo_client: AsyncIOMotorClient, scratch_db: AsyncIOMotorDatabase
) -> None:
    """Control test. This one asserts the bug DOES happen without the index.

    Replicates the check-then-insert approach in a collection with no unique
    index on `idempotency_key`: inside a transaction, read whether the key
    has been seen, and if not, insert the transaction and its two entries.
    Every duplicate reads "not seen" before any of them writes, so several
    commit and the transfer is applied more than once.

    This is the reason the unique index exists, demonstrated rather than
    asserted. It is the idempotency counterpart to the overdraft control
    test, and it is why an application-level "have I seen this key?" check
    cannot carry the guarantee on its own.
    """
    # Both collections are created explicitly before the concurrent run.
    # This matters and it cost a debugging round to find: creating a
    # collection implicitly on first insert *inside* a transaction takes an
    # exclusive lock, so concurrent transactions get a WriteConflict, are
    # retried, and their retried pre-check then sees the committed winner.
    # That serialises the control and makes it wrongly appear that
    # check-then-insert is safe. It is the same sharp edge `app/core/db.py`
    # avoids by creating every collection at startup.
    await scratch_db.create_collection("transactions")
    await scratch_db.create_collection("ledger_entries")

    transactions = scratch_db["transactions"]
    entries = scratch_db["ledger_entries"]
    # Deliberately NO unique index on idempotency_key. That is the whole
    # point of the control.
    await transactions.create_index("idempotency_key")

    source_id = ObjectId()
    destination_id = ObjectId()
    shared_key = "control-no-unique-index"
    attempts = 20

    async def naive_submit() -> bool:
        async with await mongo_client.start_session() as session:

            async def body(active_session: object) -> bool:
                seen = await transactions.find_one(
                    {"idempotency_key": shared_key},
                    projection={"_id": 1},
                    session=active_session,
                )
                if seen is not None:
                    return False

                transaction_id = ObjectId()
                await transactions.insert_one(
                    {
                        "_id": transaction_id,
                        "idempotency_key": shared_key,
                        "amount_minor": 5_000,
                        "created_at": utc_now(),
                    },
                    session=active_session,
                )
                await entries.insert_many(
                    [
                        {
                            "transaction_id": transaction_id,
                            "account_id": source_id,
                            "direction": "DEBIT",
                            "amount_minor": 5_000,
                            "signed_amount_minor": -5_000,
                            "currency": "USD",
                            "created_at": utc_now(),
                        },
                        {
                            "transaction_id": transaction_id,
                            "account_id": destination_id,
                            "direction": "CREDIT",
                            "amount_minor": 5_000,
                            "signed_amount_minor": 5_000,
                            "currency": "USD",
                            "created_at": utc_now(),
                        },
                    ],
                    session=active_session,
                )
                return True

            applied: bool = await session.with_transaction(body)
            return applied

    results = await asyncio.gather(
        *(naive_submit() for _ in range(attempts)), return_exceptions=True
    )

    applied_count = sum(1 for r in results if r is True)
    stored_transactions = await transactions.count_documents(
        {"idempotency_key": shared_key}
    )
    credited = await entries.aggregate(
        [
            {"$match": {"account_id": destination_id}},
            {"$group": {"_id": None, "total": {"$sum": "$signed_amount_minor"}}},
        ]
    ).to_list(length=1)
    credited_total = int(credited[0]["total"]) if credited else 0

    print(
        f"\nCONTROL TEST (check-then-insert, no unique index): {attempts} "
        f"concurrent submissions of one key"
    )
    print(f"CONTROL TEST: applied = {applied_count} (correct answer is 1)")
    print(f"CONTROL TEST: transaction documents stored = {stored_transactions}")
    print(
        f"CONTROL TEST: destination credited {credited_total} for a single "
        f"5000 transfer"
    )
    print(
        "CONTROL TEST: this is why idempotency cannot rest on a pre-check "
        "read alone. The unique index on transactions.idempotency_key is "
        "what makes the duplicate fail at write time."
    )

    assert applied_count > 1, (
        "the control did not reproduce the duplicate application, so the "
        "idempotency tests above prove less than they appear to"
    )
    assert stored_transactions > 1
    assert credited_total > 5_000, "money should have been credited more than once"


async def test_the_unique_index_rejects_a_second_use_of_a_key(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """The constraint is enforced by MongoDB, not by application code.

    A direct driver insert reusing a committed key is rejected, with no
    application logic in the path at all. This is what makes the guarantee
    structural rather than a matter of remembering to check.
    """
    from pymongo.errors import DuplicateKeyError

    headers = funded_account["headers"]
    key = fresh_key("db-enforced")

    created = await submit_transfer(
        client,
        headers,
        funded_account["source_id"],
        funded_account["destination_id"],
        1_000,
        idempotency_key=key,
    )
    assert created.status_code == 201

    existing = await app_database["transactions"].find_one({"idempotency_key": key})
    assert existing is not None

    duplicate = dict(existing)
    duplicate["_id"] = ObjectId()

    with pytest.raises(DuplicateKeyError):
        await app_database["transactions"].insert_one(duplicate)

    print(
        "\nDB CONSTRAINT TEST: a direct driver insert reusing a committed "
        "idempotency key was rejected by uq_idempotency_key"
    )


async def test_how_concurrent_duplicates_are_actually_detected(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Records which mechanism reports each duplicate, as an observation.

    `ledger_service` tags every duplicate with `detected_by`: `pre_check` if
    the read at the top of the transaction saw a committed transaction with
    that key, `unique_index` if the insert itself was rejected.

    Measured behaviour, which is worth knowing and is not what a first guess
    would predict: under concurrent submission essentially every duplicate is
    reported by the *pre-check*, not by the index. That is not the index
    being idle. When a transaction tries to insert a key that another
    in-flight transaction has already written but not yet committed, MongoDB
    raises a WriteConflict rather than a duplicate-key error. WriteConflict
    carries the TransientTransactionError label, so `with_transaction`
    retries the callback from the top, and by then the winner has committed,
    so the retry's pre-check sees it and reports the duplicate.

    So the index is doing the work; the `unique_index` branch is the narrow
    case where the winner commits in the gap between a loser's pre-check and
    its insert. The control test above is what establishes that the index is
    necessary, since removing it lets duplicates through.

    This test asserts the outcome (exactly one application) and reports the
    distribution without asserting a particular split, because the split
    depends on timing and pinning it would make the test flaky for no gain.
    """
    headers = funded_account["headers"]
    shared_key = fresh_key("detection-observed")

    collector = _DetectionCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        responses = await asyncio.gather(
            *(
                submit_transfer(
                    client,
                    headers,
                    funded_account["source_id"],
                    funded_account["destination_id"],
                    1_000,
                    idempotency_key=shared_key,
                )
                for _ in range(20)
            )
        )
    finally:
        root.removeHandler(collector)

    by_index = collector.detected_by.count("unique_index")
    by_precheck = collector.detected_by.count("pre_check")

    print(f"\nDETECTION TEST: outcomes = {summarise(responses)}")
    print(f"DETECTION TEST: reported by the pre-check read = {by_precheck}")
    print(f"DETECTION TEST: reported by the unique index    = {by_index}")
    print(
        "DETECTION TEST: pre-check dominance is expected. A duplicate insert "
        "against an uncommitted transaction raises WriteConflict, which is "
        "retried, and the retry's pre-check then sees the committed winner."
    )

    assert sum(1 for r in responses if r.status_code == 201) == 1
    assert (
        by_index + by_precheck == 19
    ), "every duplicate must be reported with a reason"
    assert (
        await app_database["transactions"].count_documents(
            {"idempotency_key": shared_key}
        )
        == 1
    )


async def test_concurrent_duplicates_of_an_unaffordable_transfer(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Duplicates of a transfer that cannot succeed leave nothing behind.

    All of them must be rejected, none may be recorded, and the key must
    remain unused so the client can correct the amount and retry with it.
    """
    headers = funded_account["headers"]
    shared_key = fresh_key("unaffordable-duplicate")

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                999_999,
                idempotency_key=shared_key,
            )
            for _ in range(10)
        )
    )

    print(f"\nUNAFFORDABLE DUPLICATE TEST: outcomes = {summarise(responses)}")

    assert all(r.status_code == 422 for r in responses), summarise(responses)
    assert all(error_code_of(r) == "INSUFFICIENT_FUNDS" for r in responses)
    assert (
        await app_database["transactions"].count_documents(
            {"idempotency_key": shared_key}
        )
        == 0
    )

    # The key was never consumed, so a corrected retry must be accepted.
    corrected = await submit_transfer(
        client,
        headers,
        funded_account["source_id"],
        funded_account["destination_id"],
        1_000,
        idempotency_key=shared_key,
    )
    print(
        f"UNAFFORDABLE DUPLICATE TEST: corrected retry with the same key = "
        f"{corrected.status_code} (expected 201, the key was never consumed)"
    )
    assert corrected.status_code == 201, corrected.text


async def test_distinct_keys_submitted_concurrently_all_apply(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Idempotency must not accidentally deduplicate genuine transfers.

    Ten separate payments of the same amount between the same two accounts,
    submitted simultaneously with ten different keys. All ten are real and
    all ten must go through. A system that deduplicated on payload rather
    than on key would silently drop nine legitimate payments.
    """
    headers = await register_and_authenticate(client)
    source_id = await open_account(client, headers, label="Payer")
    destination_id = await open_account(client, headers, label="Payee")
    await fund(client, headers, source_id, 10_000)

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                source_id,
                destination_id,
                1_000,
                idempotency_key=fresh_key(f"distinct-{index}"),
            )
            for index in range(10)
        )
    )

    destination_balance = await read_balance(client, headers, destination_id)

    print(f"\nDISTINCT KEYS TEST: outcomes = {summarise(responses)}")
    print(
        f"DISTINCT KEYS TEST: destination balance = {destination_balance} "
        f"(expected 10000, all ten applied)"
    )

    assert all(r.status_code == 201 for r in responses), summarise(responses)
    assert destination_balance == 10_000
    assert await app_database["transactions"].count_documents({}) == 11  # +funding

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


async def test_funding_is_idempotent_too(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """The funding endpoint goes through the same path, so it inherits this.

    Worth asserting separately: funding is where value enters the ledger, so
    a duplicate applied twice would mint money outright.
    """
    headers = await register_and_authenticate(client)
    account_id = await open_account(client, headers)
    shared_key = fresh_key("duplicate-funding")

    responses = await asyncio.gather(
        *(
            client.post(
                f"/accounts/{account_id}/funding",
                json={"amount_minor": 75_000, "currency": "USD"},
                headers={**headers, "Idempotency-Key": shared_key},
            )
            for _ in range(12)
        )
    )

    balance = await read_balance(client, headers, account_id)

    print(f"\nDUPLICATE FUNDING TEST: outcomes = {summarise(responses)}")
    print(
        f"DUPLICATE FUNDING TEST: balance = {balance} (expected 75000, " f"funded once)"
    )

    assert sum(1 for r in responses if r.status_code == 201) == 1
    assert balance == 75_000, "duplicate funding minted money"

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()
