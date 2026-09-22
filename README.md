# Ledgerlock

A double-entry bookkeeping API on MongoDB. Every movement of money is a matched
pair of immutable ledger entries — a debit on one account, a credit on another,
for the same amount — written in a single database transaction. An account's
balance is never stored; it is computed by aggregating that account's entries.

The project exists to prove four things actually hold under concurrent load,
not to assert them:

| Guarantee | How it is proven |
|---|---|
| A user account can never go negative | 50 concurrent debits against a balance that covers 10 of them → exactly **10** succeed, final balance exactly **0** |
| A resubmitted transaction is applied exactly once | 20 concurrent submissions of one idempotency key → exactly **1** accepted, exactly **one** entry pair exists |
| Every ledger entry in the system sums to exactly zero | After 104 randomised concurrent transfers → `net_signed_minor` = **0**; re-checked after each load test |
| Entries cannot be altered or deleted | 9 mutation attempts under the application's own database credentials → all refused with **Unauthorized** |

Each of those is paired with a **control test that asserts the bug does happen**
when the relevant mechanism is removed. A test showing "no overdraft occurred"
is worth very little on its own, because a harness too weak to produce the race
reports exactly the same result.

---

## The finding that shaped the design

**A correct MongoDB multi-document transaction around read-check-write does not
prevent overdraft.** This was measured, not assumed.

The obvious implementation is: open a transaction, aggregate the source
account's entries to get its balance, check it covers the amount, insert the two
entries, commit. Wrapping that in `with_transaction` feels like it should be
safe. It is not:

```
T1: read balance (100) → 100 ≥ 60 ✓ → insert entries → commit
T2: read balance (100) → 100 ≥ 60 ✓ → insert entries → commit
                                              final balance: −20
```

MongoDB gives snapshot isolation, and WiredTiger detects conflicts between
transactions that **write the same document**. The sufficiency check is a read,
and the writes are inserts of brand-new entry documents, so two concurrent
transfers out of one account touch no document in common. Both snapshots predate
either commit, both checks pass, there is no conflict to detect, and both commit.
That is write skew.

Measured, 20 concurrent debits of 1000 against a balance of 10000:

| Implementation | Succeeded | Final balance |
|---|---|---|
| Transaction around read-check-write, nothing else | **20 of 20** | **−10000** (overdrawn) |
| Ledgerlock | **10 of 20** | **0** |

### Why MongoDB, deliberately

SQL would let you write `SELECT ... FOR UPDATE` and take a row lock, or lean on
a `UNIQUE` constraint for idempotency. MongoDB hands you neither, which is
precisely why it was chosen here: the guarantee has to be **constructed and
demonstrated** rather than borrowed from a constraint the database happens to
provide. A project that got this right on PostgreSQL would mostly be
demonstrating that PostgreSQL got it right.

The two mechanisms that replace those relational features:

**1. A deliberate serialisation write, in place of row locking.** Before reading
the balance, the transaction increments `debit_serialisation_counter` on the
*source account document*. That gives concurrent debits of the same account a
document to conflict on. WiredTiger aborts one with a WriteConflict, which
carries MongoDB's `TransientTransactionError` label, which makes
`with_transaction` retry the whole callback; the retry re-reads the balance
including the winner's committed entries and either succeeds against the reduced
balance or is correctly rejected.

The counter is not a cached balance. It counts attempts, is never read to answer
a balance query, and two tests assert that distinction so it cannot be quietly
repurposed or deleted as a pointless write. It is taken **only** for USER-account
sources, because its sole job is serialising the overdraft check.

**2. A unique index, in place of a `UNIQUE` constraint — but the difference is in
where it fires.** `transactions.idempotency_key` is unique, so a duplicate fails
at *write* time inside the transaction, aborting its ledger entries with it. An
application-level "have I seen this key?" read cannot carry the guarantee,
because two simultaneous submissions both read "no" before either writes.
Measured with the index removed: **20 of 20** duplicate submissions were applied,
crediting **100000** for a single 5000 transfer.

A detail worth recording, because it is not what a first guess predicts: under
real concurrency, 19 of 19 duplicates are reported by the *pre-check*, not by
the index. When a transaction inserts a key another in-flight transaction has
written but not committed, MongoDB raises WriteConflict rather than
DuplicateKeyError; that is transient, so the callback is retried and the retry's
pre-check sees the committed winner. The index is still doing the work — the
control test above is what establishes that.

---

## Architecture

```
                        ┌──────────────────────────────────────────┐
  POST /transactions    │ FastAPI                                  │
  Idempotency-Key ─────►│                                          │
                        │  routes/      thin: validate, authorise, │
                        │               call a service, shape      │
                        │                    │                     │
                        │  services/         ▼                     │
                        │   ledger_service.py ── the only module   │
                        │     that writes transactions or          │
                        │     ledger_entries                       │
                        │   balance_service.py                     │
                        │   reconciliation.py                      │
                        └───────────────┬──────────────────────────┘
                                        │  ONE MongoDB session transaction
                                        ▼
        ┌────────────────────────────────────────────────────────────┐
        │ 1. idempotency pre-check      (find on unique-indexed key) │
        │ 2. load + validate both accounts (status, currency)        │
        │ 3. $inc serialisation counter on the SOURCE  ◄── the lock  │
        │ 4. compute balance by aggregation, in this same session    │
        │ 5. check sufficiency                                       │
        │ 6. insert the transaction document                         │
        │ 7. insert BOTH ledger entries (one call)                   │
        └────────────────────────────────┬───────────────────────────┘
                                         ▼
                          MongoDB 7 single-node replica set
                          (a replica set is required: a standalone
                           mongod refuses multi-document transactions)

  GET /accounts/{id}/balance ──► $match + $group over ledger_entries
                                 → computed, never a stored field
```

Steps 3, 4 and 5 in that order are the whole point. Any step failing rolls back
all of them, so there is no state in which a debit entry exists without its
matching credit entry.

### What the database enforces, not the application

| Constraint | Mechanism | What it makes impossible |
|---|---|---|
| Idempotency | `uq_idempotency_key` unique index | Applying one key twice |
| One debit + one credit per transaction | `uq_one_entry_per_direction_per_transaction` | Replaying one side of a pair |
| Sign matches direction | `$jsonSchema` + `$expr` collection validator | A DEBIT with a positive signed amount, on insert *or* update |
| One boundary account per currency | Unique partial index | A second untracked source of money |
| Entries cannot be updated or deleted | Least-privilege role (hardened profile) | The application mutating history at all |

### Sign convention, stated once

```
CREDIT → signed_amount_minor = +amount_minor
DEBIT  → signed_amount_minor = −amount_minor

balance(account) = sum(signed_amount_minor where account_id = account)
reconciliation   = sum(signed_amount_minor over every entry) == 0
```

Money is always an integer count of minor units. Never a float: binary floating
point cannot represent 0.01 exactly, and rounding drift in a ledger is a
correctness bug, not a display bug.

### Where money comes from

Double-entry has a structural consequence that is easy to miss: if every
transaction nets to zero and every account starts at zero, no account can hold a
positive balance unless some account is allowed to go negative. `SYSTEM` boundary
accounts are that somewhere — one per currency, exempt from the overdraft check,
with a negative balance equal to the total held across all user accounts in that
currency. They cannot be created or spent from through the API. Verified:
funding 12000 + 7500 + 33333 leaves the boundary account at exactly **−52833**
and total user holdings at exactly **52833**.

---

## Measured numbers

Every number below came from a run whose raw output is committed under
`tests/concurrency/results/` or `tests/load/results/`. Nothing here is an
estimate. Environment for all of them: MongoDB 7.0.43 single-node replica set,
Python 3.12.9, Darwin 25.6.0 arm64.

### Overdraft under concurrency
`pytest tests/concurrency -v -s` → **47 passed**
Raw: `tests/concurrency/results/phase4-concurrency-20260922T151427Z.txt`

| Scenario | Requests | Succeeded | Final balance |
|---|---|---|---|
| 50 concurrent debits of 1000, opening 10000 | 50 | **10** | **0** (40 × INSUFFICIENT_FUNDS) |
| 2 concurrent debits of 6000, opening 10000 | 2 | **1** | **4000** |
| 10 concurrent debits of mixed sizes, opening 10000 | 10 | 6 | **550** (9450 debited, never more than available) |
| 100 concurrent debits of 1 minor unit, opening 10000 | 100 | **100** | **9900** (a lost update would read above 9900) |
| 10 senders × 5000 → one account | 10 | **10** | recipient **50000** |
| A and B each send their whole 5000 to the other | 2 | 2 | A 5000, B 5000, **sum 10000** |

### Idempotency under concurrency

| Scenario | Accepted | Ledger effect |
|---|---|---|
| 20 concurrent submissions, one key | **1** | 1 transaction document, exactly 4 entries total, destination credited **2500** once |
| 12 concurrent duplicate *funding* requests | **1** | balance **75000**, not 900000 — a duplicate here would mint money |
| 10 concurrent duplicates of an unaffordable transfer | 0 | nothing written, and the key was **not** consumed — a corrected retry with the same key then returned 201 |
| 10 concurrent transfers, 10 *distinct* keys | **10** | destination credited 10000 — idempotency must not deduplicate genuine payments |

### The cost of the guarantee

Contention is per-account, and that is the most useful measurement in the
project. From the load test, comparing 5 shared hot accounts against 1:

| | 5 hot accounts | 1 hot account |
|---|---|---|
| Uncontended transfer p95 | **290 ms** | **270 ms** |
| Contended transfer p95 | 2500 ms | **11000 ms** |
| Aggregate throughput | **133.9 req/s** | 41.7 req/s |
| Transaction callback amplification | **4.51×** | **22.88×** |
| Max attempts for one commit | 121 | 463 |

Concentrating all contention on one account instead of five costs the contended
path roughly 4× its p95 and cuts throughput to about a third, **while leaving
uncontended transfer latency essentially unchanged**. Accounts nobody else is
touching do not pay for the hot account's contention — a direct consequence of
the serialisation write being on the source account document rather than on
anything global.

### Load test
`LOAD_HOT_ACCOUNT_COUNT=5 ./scripts/run_load_test.sh 50 10 60s`
50 users, 60 s, 1 uvicorn worker, 1 container, load generator on the same
machine. Rate limiting off, deliberately: at 100/minute the test would have
measured the limiter rather than the ledger.
Raw: `tests/load/results/20260922T154105Z-scenario-5-hot-accounts/`

| Request | n | failures | p50 | p95 | p99 |
|---|---|---|---|---|---|
| `POST /transactions` uncontended | 2721 | **0** | 200 ms | 290 ms | 370 ms |
| `POST /transactions` contended (5 hot accounts) | 2181 | **0** | 550 ms | 2500 ms | 4200 ms |
| `POST /transactions` over-debit, must reject | 563 | **0** | 190 ms | 420 ms | 610 ms |
| `POST /transactions` replayed key, must reject | 538 | **0** | 100 ms | 160 ms | 250 ms |
| `GET /accounts/{id}/balance` | 1726 | **0** | 67 ms | 110 ms | 150 ms |
| **Aggregated** | **7929** | **0** | **190 ms** | **1400 ms** | 2900 ms |

Every over-debit attempt was rejected and every replayed key was rejected,
across both scenarios, without exception. A `201` on either of those tasks is
reported as a *failure* by the load test, since either would mean a guarantee had
broken.

**Reconciliation immediately after the load** — the number that actually matters:

| | 5 hot accounts | 1 hot account |
|---|---|---|
| `net_signed_minor` | **0** | **0** |
| Entries / transactions | 10050 / 5025 | 3010 / 1505 |
| `total_entries == total_transactions × 2` | ✓ | ✓ |
| Orphaned entries, unbalanced groups, negative accounts | 0, 0, 0 | 0, 0, 0 |
| `healthy` | **true** | **true** |

### Reconciliation is verified capable of failing

A check that cannot fail proves nothing, so four corruptions are induced
deliberately. Three of the four leave the global net at exactly zero, which is
why the report carries more than one number:

| Induced corruption | `net_signed_minor` | Caught by |
|---|---|---|
| Deleted the CREDIT of a pair | −1500 | net, plus 1 unbalanced group |
| Orphaned pair belonging to no transaction | **0** | `entries_without_transaction=2` |
| Valid pair that overdraws an account | **0** | `negative_user_accounts` |
| −700 USD and +700 EUR, cancelling globally | **0** | per-currency net |

### Immutability
`./scripts/verify_append_only.sh` → **17 assertions passed**
Raw: `tests/concurrency/results/phase8-append-only-privileges-20260922T183456Z.txt`

Under the application's own database credentials in the hardened profile:

| Operation | Outcome |
|---|---|
| `find`, `insert` | **ALLOWED** — the role is restrictive, not broken |
| update amount, update direction, replace | **REFUSED — Unauthorized** |
| delete one, delete many, drop collection | **REFUSED — Unauthorized** |
| update / delete a transaction, drop database | **REFUSED — Unauthorized** |
| anonymous unauthenticated read | **REFUSED — Unauthorized** |

The full 33-assertion end-to-end flow passes under that restricted role,
including multi-document transactions, so the role is usable as well as strict.

### Deployed on Kubernetes
`./scripts/verify_k8s.sh` (cold run from a deleted cluster) → **33/33 assertions**
Raw: `tests/concurrency/results/phase7-kubernetes-20260922T175151Z.txt`

The strongest concurrency evidence here, because it ran against **two API pods
sharing one MongoDB**, reached through a Service: 30 concurrent debits of 5000
against a balance of 74000 gave exactly **14** successes and a final balance of
exactly **4000**; 10 concurrent submissions of one key gave exactly **1**
acceptance. HPA reported `cpu: 18%/70%` with `ScalingActive=True`.

### Test suite
**277 tests, all passing** — 229 unit, 48 concurrency. `ruff` (including bandit
security rules) and `black` clean. `pip-audit`: no known vulnerabilities.

---

## Run it

```bash
# 1. Configuration. There is no default JWT signing key: the app refuses to
#    start without one, and refuses the .env.example placeholder.
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # → JWT_SECRET_KEY

# 2. MongoDB (single-node replica set, required for transactions) and the API
docker compose up -d --build

# 3. Verify the whole flow end to end: register, open accounts, fund, transfer,
#    30 concurrent debits proving no overdraft, a replayed key proving
#    exactly-once, then reconcile the ledger.
./scripts/verify_compose.sh
```

API docs: http://localhost:8000/docs

### Tests

```bash
# MongoDB must be running: docker compose up -d mongo
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q

# The concurrency suite with its measured output
.venv/bin/python -m pytest tests/concurrency -v -s
```

The suite runs against a **real** replica set, never a mock. Every guarantee
under test is a property of MongoDB's transaction layer; mocking it out would
mean testing nothing that matters.

### Hardened profile

The default compose profile runs MongoDB without authentication, which suits a
local dev and test stack — the suite creates and drops databases, which a
least-privilege user deliberately cannot. The hardened overlay is what a
deployment should use:

```bash
# Set MONGO_ROOT_PASSWORD and MONGO_APP_PASSWORD in .env first
docker compose -f docker-compose.yml -f docker-compose.hardened.yml up -d --build
./scripts/verify_append_only.sh
```

### Kubernetes

```bash
./scripts/verify_k8s.sh            # create kind cluster, deploy, verify
./scripts/verify_k8s.sh --clean    # ...and tear it down
```

### Load test

```bash
LOAD_HOT_ACCOUNT_COUNT=5 ./scripts/run_load_test.sh 50 10 60s
```

---

## Honest limitations

Stated here rather than left to be discovered. Full detail, with measurements,
in [`MEMORY.md`](MEMORY.md) under *Known Issues*.

- **Extreme single-account contention has unbounded tail latency.** With 50
  clients all debiting one account, p99 reached 16 s and one request took 33 s,
  needing 463 transaction attempts. No request failed and no guarantee was
  violated, but a 33-second request is not acceptable to ship. The fix is a
  bounded retry budget returning `409 WRITE_CONFLICT`; the error code exists,
  the budget does not. Not implemented, because it is a change to the core
  write path and belongs in its own piece of work.
- **An in-flight debit may not see a concurrent incoming credit**, so a transfer
  can be rejected as `INSUFFICIENT_FUNDS` even though a payment arriving at the
  same instant would have covered it. A conservative failure: it never permits
  an overdraft, and the client retries. For a ledger, that is the right way to
  be wrong.
- **Rate limiting is per process, not distributed.** Counters live in each API
  process's memory, so with N replicas the effective global limit is roughly
  N × the configured value. Making it exact needs shared storage such as Redis.
- **Immutability is only database-enforced in the hardened profile.** Under the
  default profile, a caller with unrestricted credentials can rewrite an entry
  if the result stays self-consistent, because a `$jsonSchema` validator judges
  only the resulting document. There is a test asserting exactly that, so the
  boundary is explicit. Reconciliation still detects such a tamper.
- **A holder of MongoDB root credentials can still do anything.** The
  least-privilege role constrains the *application*, which is the realistic
  threat (a bug, or a compromised application process), not a DBA.
- **Load numbers are a baseline, not a capacity claim.** Load generator and
  service shared one laptop, one uvicorn worker, one MongoDB node, nothing
  tuned. Useful for comparing changes; not a statement about real hardware.
- **Single-node replica set.** Correct for a local demonstration and wrong for
  anything else: it is a single point of failure, and `w: majority` across one
  voting member is a weaker durability guarantee than it sounds.
- **`PENDING`, `FAILED` and `REVERSED` transaction statuses are never written.**
  Because the whole write is one transaction, a submission either commits as
  `COMPLETED` or rolls back leaving nothing. Rejections are logged with a
  specific reason but not persisted. The values exist because the contract needs
  them and clients should read the field rather than infer success from an HTTP
  status code.

---

## Project documentation

| File | Contents |
|---|---|
| [`PRD.md`](PRD.md) | What this is, the problem, goals, scope boundaries |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Stack, folder structure, how the parts connect |
| [`RULES.md`](RULES.md) | Development rulebook the build followed |
| [`DESIGN.md`](DESIGN.md) | API and code-level conventions |
| [`TASK.md`](TASK.md) | The phased build plan |
| [`MEMORY.md`](MEMORY.md) | Build log: every decision, assumption, finding, known issue, and measured number |

`MEMORY.md` is the most useful of these for a reviewer. It records the things
that went wrong and what they taught, including four bugs the verification work
found: a config parser that crashed only in a container, a reconciliation
checker that raised false alarms under load, an amount field that accepted
`true` as 1, and a Kubernetes bootstrap deadlock.

## Out of scope

No frontend — this project is proven through its API, its test suite and its
load test report. No real payment rails, and no multi-currency conversion. No
message queue: the correctness guarantee here comes from the database
transaction layer, which is deliberately a different engineering problem from
queue-based delivery semantics.

Ledgerlock is an original system built on standard double-entry bookkeeping
principles, not a clone of any specific product's ledger implementation.
