"""The overdraft guarantee under concurrency. The core of the project.

PRD.md goal 1: an account's balance can never go negative, even under many
concurrent debit attempts that individually appear to have sufficient
funds.

This file contains two kinds of test, and the second kind is what makes
the first kind meaningful:

1. Tests that fire N simultaneous debits at one account and assert the
   balance never goes below zero, and that exactly the right number of
   them succeeded.
2. A control test that removes the one mechanism preventing the race, and
   demonstrates that the overdraft then actually happens. Without it, all
   the first kind proves is "the test did not catch a bug", which is also
   what a test running against a harness too weak to produce the race
   would report.
"""

from __future__ import annotations

import asyncio

import pytest
from bson import ObjectId
from httpx import AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.models.common import utc_now
from app.services import reconciliation as reconciliation_service
from tests.concurrency.conftest import (
    error_code_of,
    fund,
    open_account,
    read_balance,
    register_and_authenticate,
    submit_transfer,
    summarise,
)

pytestmark = pytest.mark.concurrency


# ---------------------------------------------------------------------
# The guarantee
# ---------------------------------------------------------------------


async def test_fifty_concurrent_debits_cannot_overdraw_the_account(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """50 simultaneous debits of 1000 against a balance of 10000.

    Every one of the 50 requests, considered on its own against the opening
    balance, looks affordable. Only 10 of them can be. The assertion is
    both that the balance never goes negative and that *exactly* 10
    succeeded, because "no overdraft" could also be satisfied by a system
    that panicked and rejected everything.
    """
    headers = funded_account["headers"]
    source_id = funded_account["source_id"]
    destination_id = funded_account["destination_id"]

    attempts = 50
    debit_amount = 1_000
    affordable = funded_account["opening_balance"] // debit_amount  # 10

    responses = await asyncio.gather(
        *(
            submit_transfer(client, headers, source_id, destination_id, debit_amount)
            for _ in range(attempts)
        )
    )

    succeeded = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code != 201]
    final_balance = await read_balance(client, headers, source_id)
    destination_balance = await read_balance(client, headers, destination_id)

    print(
        f"\nOVERDRAFT TEST: {attempts} concurrent debits of {debit_amount} "
        f"against an opening balance of {funded_account['opening_balance']}"
    )
    print(f"OVERDRAFT TEST: outcomes = {summarise(responses)}")
    print(f"OVERDRAFT TEST: succeeded = {len(succeeded)} (expected {affordable})")
    print(f"OVERDRAFT TEST: final source balance = {final_balance} (expected 0)")
    print(f"OVERDRAFT TEST: destination balance = {destination_balance}")

    assert final_balance >= 0, f"OVERDRAFT: balance went to {final_balance}"
    assert len(succeeded) == affordable, (
        f"{len(succeeded)} transfers succeeded, but only {affordable} were "
        f"affordable from a balance of {funded_account['opening_balance']}"
    )
    assert final_balance == 0
    assert destination_balance == affordable * debit_amount

    # Every rejection must be the correct, specific reason.
    assert all(
        error_code_of(r) == "INSUFFICIENT_FUNDS" for r in rejected
    ), f"unexpected rejection reasons: {summarise(rejected)}"

    # The money that left the source arrived at the destination, exactly.
    assert funded_account["opening_balance"] - final_balance == destination_balance

    # And the ledger as a whole is still sound.
    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()
    assert report.net_signed_minor == 0


async def test_two_concurrent_debits_that_individually_fit_but_jointly_do_not(
    client: AsyncClient, funded_account: dict
) -> None:
    """The textbook lost-update case, reduced to its smallest form.

    Balance 10000, two simultaneous debits of 6000. Each passes a
    sufficient-funds check against the opening balance. Only one can
    actually be allowed.
    """
    headers = funded_account["headers"]

    first, second = await asyncio.gather(
        submit_transfer(
            client,
            headers,
            funded_account["source_id"],
            funded_account["destination_id"],
            6_000,
        ),
        submit_transfer(
            client,
            headers,
            funded_account["source_id"],
            funded_account["destination_id"],
            6_000,
        ),
    )

    statuses = sorted([first.status_code, second.status_code])
    final_balance = await read_balance(client, headers, funded_account["source_id"])

    print(f"\nPAIRED DEBIT TEST: statuses = {statuses}")
    print(f"PAIRED DEBIT TEST: final balance = {final_balance} (expected 4000)")

    assert statuses == [201, 422]
    assert final_balance == 4_000


async def test_concurrent_debits_of_mixed_sizes_never_overdraw(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Uneven amounts, so the outcome is not a neat multiple.

    With varying sizes, which subset wins depends on the interleaving, so
    the assertion cannot be on an exact count. What must hold regardless is
    that the balance never goes below zero and that the total debited never
    exceeds what was there.
    """
    headers = funded_account["headers"]
    amounts = [1_500, 2_750, 900, 4_000, 3_300, 1_100, 2_450, 6_000, 750, 5_000]

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                amount,
            )
            for amount in amounts
        )
    )

    succeeded_amounts = [
        amounts[i] for i, r in enumerate(responses) if r.status_code == 201
    ]
    total_debited = sum(succeeded_amounts)
    final_balance = await read_balance(client, headers, funded_account["source_id"])

    print(f"\nMIXED SIZE TEST: requested amounts = {amounts}")
    print(f"MIXED SIZE TEST: outcomes = {summarise(responses)}")
    print(f"MIXED SIZE TEST: succeeded amounts = {succeeded_amounts}")
    print(
        f"MIXED SIZE TEST: total debited = {total_debited} of "
        f"{funded_account['opening_balance']} available"
    )
    print(f"MIXED SIZE TEST: final balance = {final_balance}")

    assert final_balance >= 0, f"OVERDRAFT: balance went to {final_balance}"
    assert total_debited <= funded_account["opening_balance"]
    assert final_balance == funded_account["opening_balance"] - total_debited

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


async def test_concurrent_debits_from_several_users_to_one_destination(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Contention spread across accounts, converging on one destination.

    Different source accounts mean no serialisation conflict between the
    debits, so all of them should succeed. This is the case that shows the
    locking is per-account rather than a global bottleneck: correctness
    without serialising unrelated work.
    """
    senders = []
    recipient_headers = await register_and_authenticate(client)
    recipient_id = await open_account(client, recipient_headers, label="Recipient")

    for index in range(10):
        headers = await register_and_authenticate(client)
        account_id = await open_account(client, headers, label=f"Sender {index}")
        await fund(client, headers, account_id, 5_000)
        senders.append((headers, account_id))

    responses = await asyncio.gather(
        *(
            submit_transfer(client, headers, account_id, recipient_id, 5_000)
            for headers, account_id in senders
        )
    )

    recipient_balance = await read_balance(client, recipient_headers, recipient_id)

    print(f"\nFAN-IN TEST: outcomes = {summarise(responses)}")
    print(f"FAN-IN TEST: recipient balance = {recipient_balance} (expected 50000)")

    assert all(r.status_code == 201 for r in responses), summarise(responses)
    assert recipient_balance == 50_000

    for headers, account_id in senders:
        assert await read_balance(client, headers, account_id) == 0

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


async def test_concurrent_transfers_in_both_directions_stay_consistent(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """A and B paying each other at the same time.

    Two accounts, each funded with 5000, each sending 5000 to the other
    simultaneously. Either ordering is legitimate and both may succeed;
    what must not happen is either account going negative or value being
    created or destroyed.
    """
    headers = await register_and_authenticate(client)
    account_a = await open_account(client, headers, label="A")
    account_b = await open_account(client, headers, label="B")
    await fund(client, headers, account_a, 5_000)
    await fund(client, headers, account_b, 5_000)

    responses = await asyncio.gather(
        submit_transfer(client, headers, account_a, account_b, 5_000),
        submit_transfer(client, headers, account_b, account_a, 5_000),
    )

    balance_a = await read_balance(client, headers, account_a)
    balance_b = await read_balance(client, headers, account_b)

    print(f"\nBIDIRECTIONAL TEST: outcomes = {summarise(responses)}")
    print(
        f"BIDIRECTIONAL TEST: A = {balance_a}, B = {balance_b}, sum = "
        f"{balance_a + balance_b} (expected 10000)"
    )

    assert balance_a >= 0 and balance_b >= 0
    assert balance_a + balance_b == 10_000, "value was created or destroyed"

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


async def test_a_hundred_concurrent_debits_of_one_minor_unit(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """Maximum contention on one account, minimum amount per request.

    100 requests all serialising on the same source document. Every one is
    affordable, so all 100 must succeed and the balance must land on
    exactly 9900. This is the case that would expose a lost update as a
    balance that is too high.
    """
    headers = funded_account["headers"]

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                1,
            )
            for _ in range(100)
        )
    )

    succeeded = sum(1 for r in responses if r.status_code == 201)
    final_balance = await read_balance(client, headers, funded_account["source_id"])

    print(f"\nHIGH CONTENTION TEST: outcomes = {summarise(responses)}")
    print(f"HIGH CONTENTION TEST: succeeded = {succeeded} of 100")
    print(f"HIGH CONTENTION TEST: final balance = {final_balance} (expected 9900)")

    assert succeeded == 100, summarise(responses)
    assert (
        final_balance == 9_900
    ), "a lost update would show up here as a balance above 9900"

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()


# ---------------------------------------------------------------------
# The control: proof that the mechanism is doing the work
# ---------------------------------------------------------------------


async def test_control_a_plain_transaction_without_serialisation_overdraws(
    mongo_client: AsyncIOMotorClient, scratch_db: AsyncIOMotorDatabase
) -> None:
    """Control test. This one asserts that the bug DOES happen.

    It replicates the obvious implementation directly against MongoDB: open
    a transaction, aggregate the balance, check sufficiency, insert the two
    entries, commit. A completely correct multi-document ACID transaction,
    with one thing missing -- there is no write to a document that
    concurrent debits share.

    MongoDB's snapshot isolation plus WiredTiger conflict detection only
    catches transactions that write the *same document*. Here the
    sufficiency check is a read and the writes are inserts of brand-new
    entries, so two concurrent debits of the same account touch nothing in
    common. Both snapshots predate either commit, both checks pass, there
    is no conflict to detect, and both commit.

    The point of keeping this in the suite permanently: it proves the
    harness above can actually produce the race, and it proves the
    serialisation write in `ledger_service` is load-bearing rather than
    decorative. If someone deletes that `$inc` as a pointless write, the
    tests above start failing and this one explains why.
    """
    entries = scratch_db["ledger_entries"]
    await entries.create_index([("account_id", 1)])

    account_id = ObjectId()
    counterparty_id = ObjectId()
    opening_balance = 10_000
    debit_amount = 1_000
    attempts = 20

    await entries.insert_one(
        {
            "transaction_id": ObjectId(),
            "account_id": account_id,
            "direction": "CREDIT",
            "amount_minor": opening_balance,
            "signed_amount_minor": opening_balance,
            "currency": "USD",
            "created_at": utc_now(),
        }
    )

    async def naive_debit(amount: int) -> bool:
        """Transaction-wrapped, but with no shared document to conflict on."""
        async with await mongo_client.start_session() as session:

            async def body(active_session: object) -> bool:
                rows = await entries.aggregate(
                    [
                        {"$match": {"account_id": account_id}},
                        {
                            "$group": {
                                "_id": None,
                                "balance": {"$sum": "$signed_amount_minor"},
                            }
                        },
                    ],
                    session=active_session,
                ).to_list(length=1)
                balance = rows[0]["balance"] if rows else 0

                if balance < amount:
                    return False

                transaction_id = ObjectId()
                await entries.insert_many(
                    [
                        {
                            "transaction_id": transaction_id,
                            "account_id": account_id,
                            "direction": "DEBIT",
                            "amount_minor": amount,
                            "signed_amount_minor": -amount,
                            "currency": "USD",
                            "created_at": utc_now(),
                        },
                        {
                            "transaction_id": transaction_id,
                            "account_id": counterparty_id,
                            "direction": "CREDIT",
                            "amount_minor": amount,
                            "signed_amount_minor": amount,
                            "currency": "USD",
                            "created_at": utc_now(),
                        },
                    ],
                    session=active_session,
                )
                return True

            committed: bool = await session.with_transaction(body)
            return committed

    results = await asyncio.gather(
        *(naive_debit(debit_amount) for _ in range(attempts))
    )

    rows = await entries.aggregate(
        [
            {"$match": {"account_id": account_id}},
            {"$group": {"_id": None, "balance": {"$sum": "$signed_amount_minor"}}},
        ]
    ).to_list(length=1)
    final_balance = int(rows[0]["balance"])
    succeeded = sum(1 for committed in results if committed)
    affordable = opening_balance // debit_amount

    print(
        f"\nCONTROL TEST (naive implementation, no serialisation write): "
        f"{attempts} concurrent debits of {debit_amount} against "
        f"{opening_balance}"
    )
    print(
        f"CONTROL TEST: succeeded = {succeeded} (only {affordable} were " f"affordable)"
    )
    print(f"CONTROL TEST: final balance = {final_balance} (OVERDRAWN)")
    print(
        "CONTROL TEST: this is what a plain MongoDB transaction around "
        "read-check-write does. It is why ledger_service takes a "
        "serialisation write on the source account before reading the "
        "balance."
    )

    assert succeeded > affordable, (
        "the control test did not reproduce the race, so the tests above "
        "prove less than they appear to. Investigate before trusting them."
    )
    assert final_balance < 0, (
        f"expected the naive implementation to overdraw, but the balance "
        f"was {final_balance}"
    )


async def test_the_serialisation_write_is_itself_transactional(
    client: AsyncClient, app_database: AsyncIOMotorDatabase, funded_account: dict
) -> None:
    """The counter ends up equal to the number of *committed* debits.

    Worth asserting because the first guess is wrong. The `$inc` happens
    inside the transaction, so an attempt that is rejected or retried has
    its increment rolled back along with everything else. After 25
    concurrent attempts of which 10 commit, the counter reads exactly 10,
    not 25.

    That is the correct behaviour and it is the stronger property: the
    serialisation write leaves no residue on a failed attempt. A counter
    that survived rollback would mean the transaction boundary did not
    cover it, which would mean it was not creating the conflict at the
    moment it needed to.

    It also means this field cannot be used as evidence of how many retries
    happened. For that, see the next test, which reads the retry count from
    the service itself.
    """
    from app.services.ledger_service import DEBIT_SERIALISATION_FIELD

    headers = funded_account["headers"]

    responses = await asyncio.gather(
        *(
            submit_transfer(
                client,
                headers,
                funded_account["source_id"],
                funded_account["destination_id"],
                1_000,
            )
            for _ in range(25)
        )
    )

    source = await app_database["accounts"].find_one(
        {"_id": ObjectId(funded_account["source_id"])}
    )
    assert source is not None
    counter = source[DEBIT_SERIALISATION_FIELD]
    succeeded = sum(1 for r in responses if r.status_code == 201)

    print(f"\nROLLBACK TEST: 25 concurrent debits, {succeeded} committed")
    print(
        f"ROLLBACK TEST: serialisation counter = {counter} "
        f"(equals committed debits; rejected and retried attempts rolled "
        f"their increment back)"
    )

    assert succeeded == 10
    assert counter == succeeded, (
        f"counter is {counter} but {succeeded} transfers committed. If the "
        "counter exceeds the committed count, the increment survived a "
        "rollback and is therefore outside the transaction boundary."
    )


async def test_write_conflicts_are_retried_rather_than_surfaced(
    client: AsyncClient, app_database: AsyncIOMotorDatabase
) -> None:
    """Contention must be absorbed by retrying, not returned to the caller.

    Calls the service directly, because `LedgerWriteResult.attempts` reports
    how many times the transaction callback actually ran and the HTTP
    response does not carry it. Under contention on one source account, at
    least one transfer should need more than one attempt: that is the write
    conflict being detected and the transaction being replayed against
    fresh state.

    Also asserts that no caller saw a WRITE_CONFLICT error. A write conflict
    is an internal consequence of the design and should never reach a
    client, as long as retrying resolves it.
    """
    from app.core.errors import WriteConflictError
    from app.services import ledger_service
    from tests.concurrency.conftest import fresh_key

    headers = await register_and_authenticate(client)
    source_id = await open_account(client, headers, label="Contended")
    destination_id = await open_account(client, headers, label="Sink")
    await fund(client, headers, source_id, 100_000)

    async def direct_transfer() -> object:
        try:
            return await ledger_service.create_transfer(
                idempotency_key=fresh_key("direct"),
                source_account_id=ObjectId(source_id),
                destination_account_id=ObjectId(destination_id),
                amount_minor=100,
                currency="USD",
            )
        except WriteConflictError as exc:
            return exc

    results = await asyncio.gather(*(direct_transfer() for _ in range(30)))

    write_conflicts = [r for r in results if isinstance(r, WriteConflictError)]
    attempt_counts = [
        r.attempts for r in results if not isinstance(r, WriteConflictError)
    ]

    print("\nRETRY TEST: 30 concurrent transfers on one source account")
    print(f"RETRY TEST: attempts per transfer = {sorted(attempt_counts)}")
    print(f"RETRY TEST: total callback runs = {sum(attempt_counts)} for 30 commits")
    print(
        f"RETRY TEST: WRITE_CONFLICT errors surfaced to callers = "
        f"{len(write_conflicts)}"
    )

    assert len(attempt_counts) == 30, "some transfers did not commit"
    assert (
        not write_conflicts
    ), "a write conflict reached the caller; retrying should have absorbed it"
    assert max(attempt_counts) > 1, (
        "no transfer needed a retry, so this run did not actually exercise "
        "the write-conflict path. The contention level may be too low."
    )

    final_balance = await read_balance(client, headers, source_id)
    assert final_balance == 100_000 - (30 * 100)

    report = await reconciliation_service.reconcile(app_database)
    assert report.healthy, report.model_dump()
