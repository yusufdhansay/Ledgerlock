"""Locust load test for Ledgerlock.

Run it with `scripts/run_load_test.sh`, which brings up a clean stack,
drives this file headless, captures the raw report, and then runs the
reconciliation check.

--------------------------------------------------------------------
What this measures, and why it is shaped this way
--------------------------------------------------------------------

A load test that only hammers unrelated accounts measures the web
framework. It would report a flattering throughput number while never once
exercising the thing this project exists to prove. So the workload is
deliberately mixed:

* `transfer_from_private_account` — each user transfers between its own
  accounts. No two users contend, so this measures uncontended throughput:
  what the service costs when the guarantee is free.
* `transfer_from_shared_hot_account` — every user debits *the same* account,
  using a shared token. This is the expensive path. Concurrent debits of one
  account all serialise on that account document, so transactions hit write
  conflicts and get retried. Phase 4 measured roughly 21x callback
  amplification at full contention on one account; this is where that cost
  shows up as latency under real multi-connection load.
* `attempt_overdraft` — debits a deliberately thin account for more than it
  holds, so the rejection path stays exercised throughout the run rather
  than only when something goes wrong.
* `replay_idempotency_key` — resubmits a key that has already been used, so
  the idempotency guarantee is under load too.
* `read_balance` — the aggregation path. Balance is computed, never stored,
  so reads are not free and their cost belongs in the numbers.

--------------------------------------------------------------------
Counting failures honestly
--------------------------------------------------------------------

Locust treats any non-2xx as a failure by default. That would be actively
misleading here: a `422 INSUFFICIENT_FUNDS` is the system working correctly,
and a `409 DUPLICATE_SUBMISSION` is the idempotency guarantee doing its job.
Reporting them as failures would turn a correct run into a scary-looking
report, and would hide genuine failures among them.

So every request is evaluated against the outcomes that are *expected for
that task*. Expected business rejections are recorded as successes in
Locust's own statistics and counted separately in `OUTCOMES`, which is
printed at the end and written to JSON. Anything else -- a 5xx, an
unexpected status, a WRITE_CONFLICT reaching a client -- is a real failure
and is reported as one.

That distinction is exactly what RULES.md requires distinguishable error
codes for.
"""

from __future__ import annotations

import json
import os
import random
import uuid
from collections import Counter
from typing import Any

from locust import HttpUser, between, events, task
from locust.env import Environment

# ---------------------------------------------------------------------
# Tunables. All overridable from the environment so a run can be described
# completely by its command line plus these values.
# ---------------------------------------------------------------------

PASSWORD = os.getenv("LOAD_TEST_PASSWORD", "load-test-password-not-a-secret")

#: The shared hot account is funded generously so it does not drain
#: mid-run. A drained hot account would silently turn the contended-transfer
#: task into a pure rejection task and stop measuring committed throughput.
HOT_ACCOUNT_FUNDING_MINOR = int(os.getenv("LOAD_HOT_FUNDING_MINOR", "50000000000"))
HOT_TRANSFER_AMOUNT_MINOR = int(os.getenv("LOAD_HOT_AMOUNT_MINOR", "10"))

#: How many accounts the contended task spreads its debits across.
#:
#: 1 is the pathological worst case: every virtual user debits a single
#: account, so every transaction serialises on one document and the retry
#: amplification is as bad as it can possibly get. Informative, and not
#: representative of anything real.
#:
#: A small pool (say 5) is the realistic version of the same problem: hot
#: accounts exist in real systems, several of them, with traffic landing on
#: each. Both are worth measuring, and the difference between them is the
#: clearest statement of what contention actually costs.
HOT_ACCOUNT_COUNT = int(os.getenv("LOAD_HOT_ACCOUNT_COUNT", "5"))

PRIVATE_ACCOUNT_FUNDING_MINOR = int(
    os.getenv("LOAD_PRIVATE_FUNDING_MINOR", "10000000000")
)
PRIVATE_TRANSFER_AMOUNT_MINOR = int(os.getenv("LOAD_PRIVATE_AMOUNT_MINOR", "25"))

#: Funded with a small, fixed amount, then deliberately over-debited.
THIN_ACCOUNT_FUNDING_MINOR = int(os.getenv("LOAD_THIN_FUNDING_MINOR", "100"))
THIN_OVERDRAFT_ATTEMPT_MINOR = int(os.getenv("LOAD_THIN_ATTEMPT_MINOR", "100000"))

CURRENCY = os.getenv("LOAD_CURRENCY", "USD")

SUMMARY_PATH = os.getenv("LOAD_SUMMARY_PATH", "")


# ---------------------------------------------------------------------
# Outcome tracking
# ---------------------------------------------------------------------

#: Counts every observed (task, status, error_code) outcome. gevent is
#: cooperatively scheduled and these increments contain no await points, so a
#: plain Counter is safe here without a lock.
OUTCOMES: Counter[str] = Counter()

#: Recorded so the report can state plainly whether anything went wrong,
#: rather than leaving it to be inferred from a failure percentage.
UNEXPECTED: list[str] = []


def record(task_name: str, status_code: int, error_code: str | None) -> None:
    label = f"{task_name}: {status_code}"
    if error_code:
        label += f" {error_code}"
    OUTCOMES[label] += 1


def error_code_of(response: Any) -> str | None:
    """Extract the machine-readable error code, if the body has one."""
    try:
        return str(response.json()["error"]["code"])
    except Exception:  # noqa: BLE001 - success bodies have no error key
        return None


# ---------------------------------------------------------------------
# Shared state, created once before the run
# ---------------------------------------------------------------------


class SharedHotAccount:
    """The single account every virtual user contends on.

    Deliberately shared, and deliberately a *source*, because contention on
    a debited account is the only thing that exercises the serialisation
    path. A shared destination would not: credits have no sufficiency check
    to serialise.
    """

    token: str = ""
    #: Pool of contended source accounts. Size set by LOAD_HOT_ACCOUNT_COUNT.
    account_ids: list[str] = []
    sink_account_id: str = ""
    thin_account_id: str = ""
    replay_key: str = ""

    def pick_source(self) -> str:
        """Choose a contended source account for this request."""
        return random.choice(self.account_ids)


hot = SharedHotAccount()


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def idempotency_header(label: str) -> dict[str, str]:
    return {"Idempotency-Key": f"{label}-{uuid.uuid4()}"}


@events.test_start.add_listener
def provision_shared_state(environment: Environment, **_: object) -> None:
    """Register the shared user and set up the contended accounts.

    Runs once, before any virtual user starts, using a plain requests
    session rather than a Locust client so the setup traffic does not appear
    in the measured statistics.
    """
    import requests

    base_url = (environment.host or "http://localhost:8000").rstrip("/")
    run_id = uuid.uuid4().hex[:10]
    session = requests.Session()

    def post(path: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        response = session.post(
            f"{base_url}{path}", json=payload, headers=headers, timeout=30
        )
        response.raise_for_status()
        return response.json()

    registration = post(
        "/auth/register",
        {"email": f"hot-{run_id}@loadtest.example.com", "password": PASSWORD},
        {},
    )
    hot.token = registration["access_token"]
    headers = auth_header(hot.token)

    hot.account_ids = [
        post(
            "/accounts",
            {"currency": CURRENCY, "label": f"load hot source {index}"},
            headers,
        )["id"]
        for index in range(HOT_ACCOUNT_COUNT)
    ]
    hot.sink_account_id = post(
        "/accounts", {"currency": CURRENCY, "label": "load hot sink"}, headers
    )["id"]
    hot.thin_account_id = post(
        "/accounts", {"currency": CURRENCY, "label": "load thin source"}, headers
    )["id"]

    for index, account_id in enumerate(hot.account_ids):
        post(
            f"/accounts/{account_id}/funding",
            {"amount_minor": HOT_ACCOUNT_FUNDING_MINOR, "currency": CURRENCY},
            {**headers, **idempotency_header(f"setup-hot-{index}")},
        )
    post(
        f"/accounts/{hot.thin_account_id}/funding",
        {"amount_minor": THIN_ACCOUNT_FUNDING_MINOR, "currency": CURRENCY},
        {**headers, **idempotency_header("setup-thin")},
    )

    # One committed transfer whose key is then replayed all run long, so the
    # duplicate-submission path is exercised against a real prior commit.
    hot.replay_key = f"replay-{run_id}-{uuid.uuid4()}"
    post(
        "/transactions",
        {
            "source_account_id": hot.account_ids[0],
            "destination_account_id": hot.sink_account_id,
            "amount_minor": HOT_TRANSFER_AMOUNT_MINOR,
            "currency": CURRENCY,
        },
        {**headers, "Idempotency-Key": hot.replay_key},
    )

    print(
        f"[setup] {len(hot.account_ids)} shared hot source account(s), each "
        f"funded with {HOT_ACCOUNT_FUNDING_MINOR} minor units"
    )
    print(
        f"[setup] thin source {hot.thin_account_id} funded with "
        f"{THIN_ACCOUNT_FUNDING_MINOR} minor units (to be over-debited)"
    )
    print(f"[setup] replay key committed once: {hot.replay_key}")


@events.test_stop.add_listener
def report_outcomes(environment: Environment, **_: object) -> None:
    """Print, and optionally write, the outcome breakdown.

    The throughput and latency numbers come from Locust's own CSV output.
    This adds the thing Locust cannot know: which non-2xx responses were the
    system behaving correctly.
    """
    print("\n" + "=" * 70)
    print("OUTCOME BREAKDOWN (business result of each request)")
    print("=" * 70)
    for label, count in sorted(OUTCOMES.items()):
        print(f"  {count:>8}  {label}")

    total = sum(OUTCOMES.values())
    print(f"  {total:>8}  TOTAL")

    if UNEXPECTED:
        print("\n  UNEXPECTED OUTCOMES (these are real failures):")
        for line in UNEXPECTED[:25]:
            print(f"    {line}")
        print(f"  {len(UNEXPECTED)} unexpected outcome(s) in total")
    else:
        print("\n  No unexpected outcomes. Every non-2xx response was a")
        print("  documented business rejection (insufficient funds or")
        print("  duplicate submission), not an error.")
    print("=" * 70)

    if SUMMARY_PATH:
        summary = {
            "outcomes": dict(sorted(OUTCOMES.items())),
            "total_requests_counted": total,
            "unexpected_outcome_count": len(UNEXPECTED),
            "unexpected_outcomes": UNEXPECTED[:100],
            "configuration": {
                "hot_account_count": HOT_ACCOUNT_COUNT,
                "hot_account_funding_minor": HOT_ACCOUNT_FUNDING_MINOR,
                "hot_transfer_amount_minor": HOT_TRANSFER_AMOUNT_MINOR,
                "private_account_funding_minor": PRIVATE_ACCOUNT_FUNDING_MINOR,
                "private_transfer_amount_minor": PRIVATE_TRANSFER_AMOUNT_MINOR,
                "thin_account_funding_minor": THIN_ACCOUNT_FUNDING_MINOR,
                "thin_overdraft_attempt_minor": THIN_OVERDRAFT_ATTEMPT_MINOR,
                "currency": CURRENCY,
            },
        }
        with open(SUMMARY_PATH, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        print(f"outcome summary written to {SUMMARY_PATH}")


# ---------------------------------------------------------------------
# The virtual user
# ---------------------------------------------------------------------


class LedgerUser(HttpUser):
    """One virtual client of the ledger API."""

    # Near-zero think time: the goal is to find the service's throughput
    # ceiling, not to emulate a human. Stated explicitly because "requests
    # per second" means nothing without it.
    wait_time = between(0.0, 0.01)

    def on_start(self) -> None:
        """Register this user and set up its private accounts.

        Done outside the measured tasks. Registration is deliberately slow
        (bcrypt at cost 12), and including it would drag the reported
        latency of the transaction endpoint down with it.
        """
        run_id = uuid.uuid4().hex[:12]
        self.email = f"user-{run_id}@loadtest.example.com"

        with self.client.post(
            "/auth/register",
            json={"email": self.email, "password": PASSWORD},
            name="setup: POST /auth/register",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"registration failed: {response.status_code}")
                self.token = ""
                return
            response.success()
            self.token = response.json()["access_token"]

        self.headers = auth_header(self.token)
        self.private_source_id = self._open_account("load private source")
        self.private_sink_id = self._open_account("load private sink")

        with self.client.post(
            f"/accounts/{self.private_source_id}/funding",
            json={
                "amount_minor": PRIVATE_ACCOUNT_FUNDING_MINOR,
                "currency": CURRENCY,
            },
            headers={**self.headers, **idempotency_header("setup-private")},
            name="setup: POST /accounts/{id}/funding",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
            else:
                response.failure(f"funding failed: {response.status_code}")

    def _open_account(self, label: str) -> str:
        with self.client.post(
            "/accounts",
            json={"currency": CURRENCY, "label": label},
            headers=self.headers,
            name="setup: POST /accounts",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"account creation failed: {response.status_code}")
                return ""
            response.success()
            return str(response.json()["id"])

    # -----------------------------------------------------------------
    # Measured tasks
    # -----------------------------------------------------------------

    @task(5)
    def transfer_from_private_account(self) -> None:
        """Uncontended transfer: this user's own accounts, nobody else's.

        Measures what a transfer costs when no other request is competing for
        the same source account.
        """
        if not self.token:
            return

        with self.client.post(
            "/transactions",
            json={
                "source_account_id": self.private_source_id,
                "destination_account_id": self.private_sink_id,
                "amount_minor": PRIVATE_TRANSFER_AMOUNT_MINOR,
                "currency": CURRENCY,
            },
            headers={**self.headers, **idempotency_header("private")},
            name="POST /transactions [uncontended]",
            catch_response=True,
        ) as response:
            code = error_code_of(response)
            record("uncontended transfer", response.status_code, code)

            if response.status_code == 201:
                response.success()
            elif response.status_code == 422 and code == "INSUFFICIENT_FUNDS":
                # Correct behaviour if this user has spent its funding down.
                response.success()
            else:
                UNEXPECTED.append(
                    f"uncontended transfer -> {response.status_code} {code}"
                )
                response.failure(f"unexpected: {response.status_code} {code}")

    @task(4)
    def transfer_from_shared_hot_account(self) -> None:
        """Contended transfer: every user debits the same account.

        This is the expensive path and the one that matters. All of these
        serialise on the hot account's document, so the transactions collide,
        get retried, and pay for it in latency. Comparing this task's p95
        against the uncontended task's p95 is the actual price of the
        overdraft guarantee under load.
        """
        if not hot.account_ids:
            return

        with self.client.post(
            "/transactions",
            json={
                "source_account_id": hot.pick_source(),
                "destination_account_id": hot.sink_account_id,
                "amount_minor": HOT_TRANSFER_AMOUNT_MINOR,
                "currency": CURRENCY,
            },
            headers={
                **auth_header(hot.token),
                **idempotency_header("hot"),
            },
            name="POST /transactions [contended - shared account]",
            catch_response=True,
        ) as response:
            code = error_code_of(response)
            record("contended transfer", response.status_code, code)

            if response.status_code == 201:
                response.success()
            elif response.status_code == 422 and code == "INSUFFICIENT_FUNDS":
                # Should not happen: the hot account is funded far beyond
                # what a run can spend. Recorded as a success because it is
                # still correct behaviour, but surfaced as unexpected so the
                # report cannot quietly become "everything was rejected".
                UNEXPECTED.append(
                    "contended transfer drained the hot account; funding was "
                    "too low for this run length"
                )
                response.success()
            else:
                UNEXPECTED.append(
                    f"contended transfer -> {response.status_code} {code}"
                )
                response.failure(f"unexpected: {response.status_code} {code}")

    @task(3)
    def read_balance(self) -> None:
        """Balance read: an aggregation over the account's ledger entries.

        Not a cheap lookup of a stored number, because no stored number
        exists. Its cost belongs in the report.
        """
        if not self.token:
            return

        account_id = random.choice([self.private_source_id, self.private_sink_id])
        with self.client.get(
            f"/accounts/{account_id}/balance",
            headers=self.headers,
            name="GET /accounts/{id}/balance",
            catch_response=True,
        ) as response:
            record("balance read", response.status_code, error_code_of(response))
            if response.status_code == 200:
                response.success()
            else:
                UNEXPECTED.append(f"balance read -> {response.status_code}")
                response.failure(f"unexpected: {response.status_code}")

    @task(1)
    def attempt_overdraft(self) -> None:
        """Debit a thin account for far more than it holds.

        Keeps the rejection path under continuous load. The expected outcome
        is `422 INSUFFICIENT_FUNDS` every single time; a `201` here would
        mean the overdraft guarantee had failed, so it is reported as a
        failure rather than counted as throughput.
        """
        if not hot.thin_account_id:
            return

        with self.client.post(
            "/transactions",
            json={
                "source_account_id": hot.thin_account_id,
                "destination_account_id": hot.sink_account_id,
                "amount_minor": THIN_OVERDRAFT_ATTEMPT_MINOR,
                "currency": CURRENCY,
            },
            headers={
                **auth_header(hot.token),
                **idempotency_header("overdraft-attempt"),
            },
            name="POST /transactions [overdraft attempt - must reject]",
            catch_response=True,
        ) as response:
            code = error_code_of(response)
            record("overdraft attempt", response.status_code, code)

            if response.status_code == 422 and code == "INSUFFICIENT_FUNDS":
                response.success()
            elif response.status_code == 201:
                UNEXPECTED.append(
                    "OVERDRAFT GUARANTEE VIOLATED: a transfer of "
                    f"{THIN_OVERDRAFT_ATTEMPT_MINOR} from an account holding "
                    f"{THIN_ACCOUNT_FUNDING_MINOR} was accepted"
                )
                response.failure("OVERDRAFT: over-debit was accepted")
            else:
                UNEXPECTED.append(f"overdraft attempt -> {response.status_code} {code}")
                response.failure(f"unexpected: {response.status_code} {code}")

    @task(1)
    def replay_idempotency_key(self) -> None:
        """Resubmit a key that has already been committed.

        Expected outcome is `409 DUPLICATE_SUBMISSION` every time. A `201`
        would mean the transfer had been applied twice, so it is reported as
        a failure.
        """
        if not hot.replay_key:
            return

        with self.client.post(
            "/transactions",
            json={
                "source_account_id": hot.account_ids[0],
                "destination_account_id": hot.sink_account_id,
                "amount_minor": HOT_TRANSFER_AMOUNT_MINOR,
                "currency": CURRENCY,
            },
            headers={
                **auth_header(hot.token),
                "Idempotency-Key": hot.replay_key,
            },
            name="POST /transactions [replayed key - must reject]",
            catch_response=True,
        ) as response:
            code = error_code_of(response)
            record("replayed key", response.status_code, code)

            if response.status_code == 409 and code == "DUPLICATE_SUBMISSION":
                response.success()
            elif response.status_code == 201:
                UNEXPECTED.append(
                    "IDEMPOTENCY GUARANTEE VIOLATED: a replayed key was "
                    "applied again"
                )
                response.failure("DUPLICATE APPLIED: replayed key was accepted")
            else:
                UNEXPECTED.append(f"replayed key -> {response.status_code} {code}")
                response.failure(f"unexpected: {response.status_code} {code}")
