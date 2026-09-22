# Ledgerlock — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 4: Concurrency guarantees — not started

## Completed Phases

### Phase 0 — Scaffolding (2026-09-22)
- Wrote the six root docs (PRD, ARCHITECTURE, RULES, DESIGN, TASK, MEMORY)
- Created the folder structure exactly as specified in ARCHITECTURE.md
- `requirements.txt` (runtime) and `requirements-dev.txt` (test/tooling),
  all dependencies pinned with `==`
- `pyproject.toml` for black/ruff/pytest configuration only (no packaging
  metadata; the app is run with uvicorn, not installed)
- `.env.example` with placeholders only, `.gitignore` covering `.env`,
  secrets, `__pycache__`, and database data
- `docker-compose.yml` running MongoDB 7.0 as a single-node replica set
  (`rs0`) with a healthcheck that idempotently runs `rs.initiate()` and
  then reports whether the node is a writable primary
- Verified MongoDB transaction support for real before building on it
  (see Measured Numbers below)
- Commit: `3aba6e8` — "Phase 0: scaffolding, docs, and verified MongoDB
  replica-set transactions"

### Phase 1 — Models and authentication (2026-09-22)
- `app/models/common.py`: shared field types. `ObjectIdStr` (validated
  24-hex string), `AmountMinor` (positive integer, minor units),
  `CurrencyCode`, `IdempotencyKey`, `utc_now()`, a `MongoDocument` base
  with `to_bson()`, and a `PageMeta` envelope
- `app/models/user.py`, `account.py`, `transaction.py`,
  `ledger_entry.py`: request, response, and stored-document models for
  all four entities
- `app/core/config.py`: pydantic-settings, JWT key required with no
  default and the `.env.example` placeholder explicitly rejected,
  algorithm whitelisted to HMAC variants
- `app/core/logging_config.py`: JSON logging with a redaction filter that
  blanks any field whose name suggests a secret
- `app/core/errors.py`: `ErrorCode` StrEnum, one exception class per
  failure mode, and handlers guaranteeing the single
  `{"error": {"code", "message"}}` envelope on every path including
  unhandled exceptions
- `app/core/db.py`: connection lifecycle that refuses to start against a
  non-replica-set, explicit collection creation, `$jsonSchema` +
  `$expr` validators, and all indexes
- `app/core/security.py`: bcrypt hashing off the event loop, pinned-
  algorithm JWT decode, jti-keyed revocation, `get_current_user`
- `app/routes/auth.py`: register, login, logout, me
- `app/main.py`: app factory, lifespan, liveness and readiness probes
- Commit hash recorded in git log for `Phase 1: models and authentication`

### Phase 2 — Accounts and aggregation-based balance (2026-09-22)
- `app/services/balance_service.py`: `compute_balance()` runs a `$match` +
  `$group` aggregation over `ledger_entries`, summing the stored
  `signed_amount_minor` for the net and using `$cond` for the credited and
  debited subtotals. Accepts an optional `session` so Phase 3 can run the
  sufficiency check *inside* the ledger transaction — that parameter is
  what ties read to write. An account with no entries returns
  `ZERO_BALANCE`, i.e. exactly 0, not a missing value.
- `app/services/account_service.py`: create, lookup, owner-scoped lookup,
  bounded listing, and `ensure_system_accounts()`.
- `app/routes/accounts.py`: `POST /accounts`, `GET /accounts` (paginated),
  `GET /accounts/{id}`, `GET /accounts/{id}/balance`.
- `Settings.supported_currencies` added (default USD, EUR, GBP, INR;
  accepts comma-separated or JSON from the environment).
- `AccountDocument.owner_id` made nullable so SYSTEM accounts can exist
  with no owner. Since every owner-scoped query filters on the caller's
  id, a null owner can never match, so SYSTEM accounts are unreachable
  through any user-facing route. Tested.
- Startup now provisions one SYSTEM boundary account per supported
  currency.

### Phase 3 — Atomic transaction creation (2026-09-22)
- `app/services/ledger_service.py`: the only module that writes to
  `transactions` or `ledger_entries`. `create_transfer()` wraps everything
  in one `session.with_transaction(...)`: idempotency pre-check, load and
  validate both accounts, take the serialisation write on the source,
  compute the balance inside the same transaction, check sufficiency,
  insert the transaction document, insert both ledger entries.
- **The important part is the serialisation write.** See the long
  docstring at the top of that module and the Phase 3 assumptions below. A
  plain MongoDB transaction around read-check-write does *not* prevent
  overdraft, and this was verified experimentally rather than assumed.
- `app/routes/transactions.py`: `POST /transactions`,
  `GET /transactions/{id}`, and the `require_idempotency_key` dependency.
- `POST /accounts/{id}/funding` added to the accounts router: how value
  enters the ledger, debiting the SYSTEM boundary account.
- New error codes: `MALFORMED_IDEMPOTENCY_KEY`, `TRANSACTION_NOT_FOUND`.

## In Progress
Nothing in progress. Phase 3 closed; Phase 4 (concurrency guarantees) is
next. The two throwaway probes written during Phase 3 to validate the
design are to be rebuilt as permanent Phase 4 tests: the 20-concurrent-
debit overdraft test, and the control test showing the naive
implementation overdraws.

## Assumptions

- **Python interpreter.** ARCHITECTURE.md specifies Python 3.12. The
  machine's default `python3` is 3.13.3, so the project venv is built
  explicitly from Homebrew's `python@3.12` (resolved: **3.12.9**). The
  Docker image will also pin 3.12 so local and container match.
- **Motor vs PyMongo native async.** ARCHITECTURE.md specifies Motor, so
  Motor 3.7.1 is used. Worth knowing: PyMongo 4.9+ ships a native
  `AsyncMongoClient` and Motor is on a deprecation path. ARCHITECTURE.md
  is load-bearing per the build instructions, so it is not being
  overridden here. Migration would be mechanical (swap
  `AsyncIOMotorClient` for `AsyncMongoClient`) if it ever matters.
- **bcrypt without passlib.** ARCHITECTURE.md says "bcrypt for password
  hashing" without naming a wrapper. Using the `bcrypt` package directly
  rather than `passlib[bcrypt]`, because passlib's bcrypt backend has
  repeatedly broken against new bcrypt majors and adds a layer with no
  benefit at this project's scope.
- **Replica set addressing.** The single-node replica set advertises its
  member as `localhost:27017`, and clients connect with
  `directConnection=true`. This makes one URI shape work from both the
  host (test runs against the mapped port) and from inside the compose
  network (`mongo:27017`), without needing a `/etc/hosts` entry on the
  host. `directConnection=true` skips replica-set topology discovery, so
  the driver never tries to follow the advertised `localhost` address
  from inside a container. Transactions still work, because they require
  the *deployment* to be a replica set, not the client to declare it.
- **Files added beyond ARCHITECTURE.md's tree.** `requirements-dev.txt`,
  `pyproject.toml`, `tests/conftest.py`, and `.env.example` are not in
  the tree diagram but are implied by the build instructions
  (`.env.example` and a requirements file are named explicitly in
  TASK.md Phase 0). No directories were reorganised or renamed.
- **Where the Phase 0 transaction check lives.** TASK.md asks to verify
  `with_transaction` works before building on it. That verification is
  written as `tests/unit/test_mongo_transactions.py` rather than a
  throwaway script, so it re-runs on every test run and in CI. If the
  deployment is ever misconfigured as a standalone mongod, the suite
  fails loudly at that test instead of silently losing atomicity.
- **Tests run against real MongoDB, not a mock.** Every guarantee this
  project claims is a property of MongoDB's transaction layer. Mocking
  the database would mean the tests prove nothing, so the suite requires
  a live replica set.
- **Local Kubernetes.** `kind` is installed on this machine; `minikube`
  is not. Phase 7 will verify manifests against `kind`.

### Phase 1 assumptions

- **Money has to enter the ledger from somewhere: SYSTEM accounts.** This
  is the one genuine gap in PRD.md, and it follows from the rules PRD.md
  sets. If every transaction nets to zero and every account starts at
  zero, then no account can ever hold a positive balance unless some
  account is allowed to go negative. PRD.md describes accounts, transfers
  between two accounts, and a hard no-negative-balance rule, with no
  mechanism for value to enter. Resolution: `AccountType` is `USER` or
  `SYSTEM`. A `SYSTEM` account represents the ledger's boundary with the
  outside world and is exempt from the overdraft check; its negative
  balance is a meaningful figure, namely the total value held across all
  USER accounts in that currency. `USER` accounts can never go negative,
  and that is the guarantee the Phase 4 tests prove. This is how real
  ledgers handle it, and it keeps the reconciliation-to-zero invariant
  exactly true. Guardrails: `account_type` is *not* accepted from clients
  (accepting it would let a caller mint money), SYSTEM accounts are
  provisioned by the application, and a unique partial index
  (`uq_system_account_per_currency`) makes a duplicate SYSTEM account for
  a currency impossible.
- **How value enters: `POST /accounts/{id}/funding`.** Modelled as a
  sub-resource rather than a verb endpoint, per DESIGN.md. It debits the
  SYSTEM boundary account for that currency and credits the USER account,
  through the same atomic `ledger_service` path as any transfer, and it
  requires an `Idempotency-Key` like any other transaction. In a
  production system this would be driven by a settlement webhook from a
  payment provider rather than by the account holder; PRD.md puts real
  payment rails out of scope, so this endpoint is the seam where that
  integration would attach. Stated plainly so nobody mistakes it for a
  production deposit flow.
- **Only `COMPLETED` transactions are ever written.** DESIGN.md requires
  every transaction response to carry an explicit status from
  PENDING/COMPLETED/FAILED/REVERSED. Because the entire write is wrapped
  in one MongoDB transaction, a submission either commits fully as
  COMPLETED or rolls back leaving no document at all. So there is no
  FAILED row to read: rejected submissions are logged with a specific
  reason (satisfying RULES.md) but not persisted, because persisting them
  would need a second write outside the aborted transaction. The other
  three values are in the enum because the contract needs them and
  callers should read the field rather than infer success from the HTTP
  status code.
- **`signed_amount_minor` is stored on each ledger entry.** A deliberate
  exception to the project's "no redundant stored values" stance, and the
  reasoning differs from the stored-balance case that RULES.md forbids: a
  running balance is mutated repeatedly and can therefore drift, whereas
  this value is written once inside the same atomic write as the entry and
  never updated. Storing it makes balance and reconciliation plain indexed
  `$group` sums instead of a `$cond` over every document. It is enforced
  twice: by a model validator, and by the collection's own validator
  (below), so a wrong sign cannot be written even by a direct driver call.
- **Sign convention:** CREDIT is `+amount_minor`, DEBIT is
  `-amount_minor`. Therefore `balance = sum(signed)` for an account, and
  `reconciliation = sum(signed) over all entries == 0`.
- **Database-enforced constraints, not just conventions.** Worth being
  precise about which claims the database backs:
  - `uq_idempotency_key` (unique on `transactions.idempotency_key`) is
    what makes idempotency hold under concurrency. An application-level
    "have I seen this key?" read cannot, because two concurrent
    submissions both read "no" before either writes. The duplicate fails
    at *write* time inside the transaction, aborting its ledger entries
    with it.
  - `uq_one_entry_per_direction_per_transaction` (unique on
    `(transaction_id, direction)`) makes it impossible to write two debits
    for one transaction or to replay one side of a pair.
  - The `ledger_entries` validator combines `$jsonSchema` (shape,
    `additionalProperties: false`, positive magnitude) with an `$expr`
    clause asserting `signed_amount_minor` equals `±amount_minor`
    according to `direction`. That is checked on insert *and* on any
    attempted update.
  - `uq_system_account_per_currency` (unique partial index) allows at most
    one SYSTEM account per currency.
- **Write and read concerns.** The client uses `w=majority`,
  `journal=true`, and `readPreference=primary`. Majority+journal means an
  acknowledged transfer survives a primary failover. Primary reads matter
  more than they look: a balance read from a secondary could be stale by a
  committed transfer, and a stale balance is an overdraft waiting to
  happen.
- **bcrypt runs in a worker thread.** bcrypt is CPU-bound and takes tens
  to hundreds of milliseconds. Calling it inline in an async route would
  block the event loop and stall every other in-flight request, which
  would be a real bug and would also corrupt the Phase 6 load test
  numbers. Both hashing and verification go through
  `anyio.to_thread.run_sync`. `BCRYPT_ROUNDS` is configurable (default
  12); the test suite sets 4 purely so fixtures that create many users
  stay fast.
- **Login does not leak whether an email exists.** Unknown-email and
  wrong-password return an identical status, code and message, and the
  unknown-email path performs a throwaway bcrypt verification so the two
  take comparable time. Tested.
- **Idempotency is reject, not replay.** PRD.md says a resubmitted key is
  "rejected, not reprocessed", so a duplicate returns
  `409 DUPLICATE_SUBMISSION` rather than replaying the original
  response body. Noting it because many payment APIs do the opposite.
- **Files added beyond ARCHITECTURE.md's tree (Phase 1).**
  `app/models/common.py`, `app/core/errors.py`, and
  `app/core/logging_config.py`. Justification: the error envelope and code
  set are required by DESIGN.md and RULES.md and needed somewhere
  importable by both routes and services without a circular import
  through `main.py`; the shared field types would otherwise be duplicated
  across four model modules. Named `logging_config.py` rather than
  `logging.py` to avoid any ambiguity with the standard library module.
  No directories were reorganised or renamed.
- **Rate limiting is deferred to Phase 8.** ARCHITECTURE.md lists
  `slowapi` in the stack and TASK.md places rate limiting in the Phase 8
  security pass, so it is not wired up yet. The dependency is pinned and
  installed.
- **Plan for the Phase 4 immutability test.** Today, immutability is
  enforced by there being no update/delete route plus the `$jsonSchema`
  validator. That validator constrains *shape*, so an update that keeps
  the shape valid (for example `$set: {amount_minor: 999}` on a CREDIT,
  with `signed_amount_minor` changed to match) would still be rejected by
  the `$expr` clause only if the two disagree. In Phase 4 the intent is to
  go further and give the application a least-privilege MongoDB user with
  `find` and `insert` on `ledger_entries` but not `update` or `remove`,
  since MongoDB's privilege actions are per-collection. That turns "the
  application never updates entries" into "the application's credentials
  cannot update entries", which is a materially stronger claim, and lets
  the immutability test assert a real `Unauthorized` from a direct driver
  call. Enabling auth on a replica set additionally requires a keyfile for
  internal authentication, generated at container start rather than
  committed. Not built yet; recorded so the claim made in Phase 4 matches
  what is actually implemented.

### Phase 2 assumptions

- **`app/services/account_service.py` added.** ARCHITECTURE.md's tree
  lists three services (`ledger_service`, `balance_service`,
  `reconciliation`), and account logic fits none of them. Putting it in the
  route module would break DESIGN.md's rule that routes are thin and
  services hold all business logic. DESIGN.md's rule is the stronger
  constraint, so a fourth service module was added rather than fattening
  the route. No existing module was moved or renamed.
- **Read-only account routes beyond "account creation endpoint".**
  TASK.md Phase 2 names only the creation endpoint and the balance
  computation. `GET /accounts`, `GET /accounts/{id}` were added because
  DESIGN.md specifies pagination conventions for list endpoints, which
  implies at least one exists, and because the API is otherwise unusable:
  a client could create an account but never enumerate its accounts. Both
  are read-only and owner-scoped.
- **No ledger entry listing endpoint.** Considered and deliberately not
  built: RULES.md forbids inventing scope, and nothing in PRD.md, TASK.md
  or DESIGN.md calls for one. The Phase 4 immutability test therefore
  asserts the *absence* of any mutating route on ledger data (verified by
  inspecting the generated OpenAPI paths) as well as attempting direct
  driver writes. Confirmed at the end of Phase 2 that the only routes the
  app exposes are: `/accounts` GET+POST, `/accounts/{id}` GET,
  `/accounts/{id}/balance` GET, the four `/auth/*` routes, and the two
  health probes.
- **Not-found instead of forbidden for someone else's account.**
  Owner-scoped lookups put the ownership check in the query filter, so
  requesting an account owned by another user returns
  `404 ACCOUNT_NOT_FOUND` rather than `403 FORBIDDEN`. A 403 would confirm
  that the id exists, turning the route into a probe for valid account
  ids. `require_account_owner` in `security.py` (which does raise 403)
  remains available for cases where the resource's existence is already
  known to the caller.
- **Balance is reported honestly, including if it is negative.** The
  aggregation does not clamp at zero. The overdraft guarantee lives in the
  write path, not in the reader. A reader that floored negatives at zero
  would hide exactly the corruption the reconciliation check exists to
  detect, so there is a test asserting that a directly-inserted debit shows
  up as a negative balance.

### Phase 3 assumptions

- **A MongoDB transaction around read-check-write does NOT prevent
  overdraft. This is the central finding of the whole project, and it was
  measured, not assumed.** The naive implementation is: open a
  transaction, aggregate the source account's entries for its balance,
  check the balance covers the amount, insert the two entries, commit.
  MongoDB gives snapshot isolation and WiredTiger detects conflicts
  between transactions that write *the same document* — but the
  sufficiency check is a read, and the writes are inserts of brand-new
  entry documents, so two concurrent transfers out of one account touch no
  document in common. Both snapshots predate either commit, both checks
  pass, there is no conflict to detect, and both commit.
  **Measured, 20 concurrent debits of 1000 against a balance of 10000:
  all 20 succeeded, final balance −10000.** Wrapping it in
  `with_transaction` is not enough. SQL would offer `SELECT ... FOR
  UPDATE`; MongoDB has no equivalent, which is exactly why PRD.md chose
  it — the guarantee has to be constructed.
- **The fix: a deliberate serialisation write.** Before reading the
  balance, the transaction increments `debit_serialisation_counter` on the
  *source account document*. That gives concurrent transfers out of the
  same account a document to conflict on. WiredTiger aborts one with a
  WriteConflict, which carries MongoDB's `TransientTransactionError`
  label, which makes `with_transaction` re-run the whole callback from the
  top; the retry re-reads the balance including the winner's committed
  entries and either succeeds against the reduced balance or is correctly
  rejected. **Measured, same scenario with the serialisation write:
  exactly 10 of 20 succeeded, final balance exactly 0.**
- **The counter is not a cached balance.** It counts debit attempts, it is
  never read to answer a balance query, and nothing breaks if it is wrong.
  RULES.md forbids a stored balance and this is not one. There are two
  tests asserting the distinction, so that the field cannot later be
  mistaken for one or quietly repurposed, and so that anyone deleting the
  `$inc` as a pointless write gets a failing test pointing at the
  explanation.
- **The lock is taken only for USER sources.** Its sole job is to
  serialise the overdraft check, and SYSTEM boundary accounts have no
  overdraft check. If funding took the lock, every funding request for a
  currency would contend on one document and serialise the whole system,
  which would also have distorted the Phase 6 load test. Tested: the
  SYSTEM account never acquires the field.
- **Only the source is locked, never the destination, and there is a real
  consequence.** Locking the debited account is sufficient, because a
  credit cannot push an account below zero. The consequence, stated
  plainly because it is a genuine behaviour: an in-flight debit will not
  observe a credit that commits after its snapshot was taken, so a
  transfer can be rejected as INSUFFICIENT_FUNDS even though a payment
  arriving at the same moment would have covered it. That is a
  conservative failure — it never permits an overdraft, and the client
  retries. For a ledger, a spurious rejection is the right way to be
  wrong.
- **A rejected transfer does not consume its idempotency key.** Since the
  whole transaction rolls back, the key was never recorded, so a client
  can fix the amount and retry with the same key. Tested. The alternative
  (burning the key on failure) would wrongly reject the corrected retry.
- **Idempotency needs both the pre-check and the unique index.** The read
  at step 1 handles the common case cheaply (a client retrying seconds
  later) and gives a clean 409. It cannot carry the guarantee alone,
  because two simultaneous submissions both read "not seen" before either
  inserts. The `DuplicateKeyError` from `uq_idempotency_key` closes that
  window and aborts the loser's whole transaction, entries included. The
  error context records which of the two detected it (`pre_check` vs
  `unique_index`) so the Phase 4 test can show the index doing the work.
- **`attempts` is returned from the service.** `LedgerWriteResult.attempts`
  reports how many times the transaction callback ran, so contention is a
  measured number in Phase 6 rather than a guess.
- **Self-transfer is rejected before opening a transaction.** It would
  write a debit and a credit on the same account for the same amount,
  netting to zero, while still consuming an idempotency key.
- **Source ownership is checked in the route, not the service.** It is an
  authorisation question about the caller rather than a bookkeeping
  invariant. It also blocks spending directly from the SYSTEM boundary
  account, which has no owner; tested.
- **`GET /transactions/{id}` added.** Not named in TASK.md Phase 3, but a
  client that received `409 DUPLICATE_SUBMISSION`, or lost the response to
  its original request, otherwise has no way to find out what happened.
  Visible to the owner of either account involved; anyone else gets 404
  rather than 403, so it cannot be used to enumerate transaction ids.

## Known Issues

- **Rate limiting is in-process.** `slowapi` keeps its counters in the
  API process's own memory. Running more than one replica (which the
  Phase 7 Deployment/HPA manifests explicitly do) means each replica
  enforces its own independent limit, so the effective global limit is
  roughly `limit × replica count`. Making this correct would need a
  shared backend such as Redis. Recorded here honestly rather than
  claimed as a distributed rate limiter.
- **Ledger immutability is application-and-validator level, not
  unbypassable.** There are no update or delete routes for
  `ledger_entries`, and a `$jsonSchema` collection validator rejects
  malformed writes. But a client holding direct database credentials can
  still modify documents. MongoDB has no equivalent of a
  `REVOKE UPDATE`-style per-collection immutability guarantee that the
  application could rely on. The honest claim is: immutable through the
  application, enforced in one place, with a second schema-level check —
  not physically immutable at rest.

## Real Measured Numbers (fill in only from actual test runs)

### Phase 0 — MongoDB transaction support
Command: `.venv/bin/python -m pytest tests/unit/test_mongo_transactions.py -v`
Run on 2026-09-22 against `mongo:7.0` via docker-compose, Python 3.12.9.
Result: **4 passed in 0.39s**

| Check | Result |
|---|---|
| Deployment is a replica set, node is writable primary | PASS (`setName=rs0`, `isWritablePrimary=true`) |
| `with_transaction` commits all writes across 2 collections | PASS |
| Failure mid-transaction rolls back *every* write (0 docs left) | PASS |
| Duplicate-key error inside a transaction aborts the whole transaction | PASS |

The last two are the ones that matter: rollback-on-failure is what makes
"a debit entry can never exist without its matching credit entry" true,
and duplicate-key-aborts-transaction is the mechanism Phase 3 uses for
idempotency (fail at write time via a unique index, so there is no
read-then-write race window to lose).

### Phase 1 — Authentication test suite
Command: `.venv/bin/python -m pytest -q`
Run on 2026-09-22, Python 3.12.9, against `mongo:7.0` replica set `rs0`.
Result: **39 passed in 2.73s** (35 auth tests + the 4 Phase 0 transaction
checks). `ruff check app tests` and `black --check app tests` both clean.

Notable assertions that passed, beyond the happy paths:
- 8 concurrent registrations of the same email produced exactly 1 user
  (1 x 201, 7 x 409 EMAIL_ALREADY_REGISTERED) — uniqueness comes from the
  index, not a check-then-insert
- unknown-email and wrong-password login responses are byte-identical
- expired token, wrong-signing-key token, and an `alg: none` token are all
  rejected
- logout revokes, a second logout does not 500, and the blacklist record
  stores the `jti` rather than the token itself
- a password of 25 three-byte characters (75 bytes) is rejected while 24
  (72 bytes) is accepted — the bcrypt limit is enforced in bytes

### Phase 2 — Accounts and balance test suite
Command: `.venv/bin/python -m pytest -q` (full suite)
Run on 2026-09-22, Python 3.12.9, against `mongo:7.0` replica set `rs0`.
Result: **71 passed in 5.08s** (4 Phase 0 + 35 Phase 1 + 32 Phase 2).
`ruff check app tests` and `black app tests` both clean.

Balance assertions that passed, all computed by aggregation with nothing
stored:
- credits 10000 + 2500, debits 4000 + 1 -> balance exactly **8499**,
  credited 12500, debited 4001, entry_count 4
- an account with no entries -> balance exactly **0**, entry_count 0
- 500 entries of 1 minor unit -> exactly **500**
- 9007199254740993 (2^53 + 1) credited, 1 debited -> exactly
  **9007199254740992**, which a float64 path could not have produced
- account documents contain no `balance` / `balance_minor` /
  `available_balance` field, asserted against the stored document

Database-level rejections that passed:
- a DEBIT with a positive `signed_amount_minor` is refused by the
  collection validator's `$expr` clause on a direct driver insert
- `amount_minor` of 0 or -100 is refused
- a second DEBIT for the same `transaction_id` is refused by
  `uq_one_entry_per_direction_per_transaction`
- a second SYSTEM account for USD is refused by
  `uq_system_account_per_currency`
- `{"$ne": null}` and `$where` submitted as an account id are rejected at
  the edge as MALFORMED_REQUEST, never reaching a query document

### Phase 3 — Atomic transaction creation test suite
Command: `.venv/bin/python -m pytest -q` (full suite)
Run on 2026-09-22, Python 3.12.9, against `mongo:7.0` replica set `rs0`.
Result: **112 passed in 14.98s** (4 + 35 + 32 + 41). `ruff` and `black`
clean.

Also run during Phase 3, as throwaway probes, to validate the design
before building Phase 4 on it. Both were deleted after the run; permanent
versions are to be written in Phase 4. Numbers below are from the actual
probe output:

| Implementation | Concurrent debits | Succeeded | Final balance |
|---|---|---|---|
| Naive: `with_transaction` around read-check-write, no shared write | 20 x 1000 against 10000 | **20** | **−10000** (overdrawn) |
| Ledgerlock: serialisation write on the source before the balance read | 20 x 1000 against 10000 | **10** | **0** |

The first row is the measurement that justifies the whole design. It is
not a hypothetical: a correct, committed MongoDB multi-document
transaction permits the overdraft, because the read and the writes share
no document for WiredTiger to detect a conflict on.

### Later phases
- Overdraft test: probe run during Phase 3 (above); the permanent test is
  Phase 4
- Idempotency test: not run yet (Phase 4)
- Reconciliation: not run yet (Phase 4, re-run after Phase 6 load test)
- Load test: not run yet (Phase 6)
