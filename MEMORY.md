# Ledgerlock — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 8: CI and security pass — not started

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

### Phase 4 — Concurrency guarantees (2026-09-22)
- `app/services/reconciliation.py`: the system-wide integrity check. Net
  signed sum, net per currency, entries with no transaction, transactions
  whose entries are not exactly one debit and one credit summing to zero,
  and USER accounts with a negative balance. Read-only; it never repairs
  anything, because silently correcting a discrepancy destroys the
  evidence of how it arose.
- `app/routes/reconciliation.py`: `GET /reconciliation` (PRD.md asks for an
  endpoint as well as a test, and the endpoint is what makes the invariant
  checkable against a running deployment after the Phase 6 load test).
- `ReconciliationReport` extended: added `healthy`, split `orphaned_entries`
  into `entries_without_transaction` and `unbalanced_transaction_groups`,
  added `negative_user_accounts`. **Assert on `healthy`, not `balanced`** —
  `balanced` only covers sum-to-zero, and three of the four corruptions
  tested net to zero while still being corrupt.
- `tests/concurrency/`: 47 tests across overdraft, idempotency,
  immutability and reconciliation, including two control tests that assert
  the bug *does* happen when the relevant mechanism is removed.
- Raw output captured to
  `tests/concurrency/results/phase4-concurrency-20260922T151427Z.txt`.

### Phase 5 — Dockerized service with compose orchestration (2026-09-22)
- `Dockerfile`: two stages so build tooling never reaches the runtime
  image. Pinned to `python:3.12.9-slim`, the exact patch version the suite
  runs on locally. Runs as unprivileged uid 1001, one uvicorn worker per
  container, container healthcheck hitting `/health` via the interpreter
  rather than adding curl to the image.
- `.dockerignore`: keeps `.env`, `.git`, the host `.venv`, tests and caches
  out of the build context entirely.
- `docker-compose.yml`: adds the `api` service, gated on
  `depends_on: mongo: condition: service_healthy` so it never starts
  against a mongod that cannot yet serve transactions. Runs with
  `read_only: true`, `no-new-privileges`, and a tmpfs for `/tmp`.
- `scripts/verify_compose.sh`: 33-assertion end-to-end check over HTTP.
- `tests/unit/test_config.py`: 38 configuration tests, added because of the
  bug below.
- Verified image hygiene: `id` reports uid 1001, `/app` contains only
  `app/`, and there is no `.env`, `tests/` or `.git` in the image.
- Raw verification output:
  `tests/concurrency/results/phase5-compose-e2e-20260922T152634Z.txt`

### Phase 6 — Load test with captured baseline numbers (2026-09-22)
- `tests/load/locustfile.py`: mixed workload. Uncontended transfers (each
  user's own accounts), contended transfers against a shared pool of hot
  source accounts, balance reads, a task that deliberately over-debits a
  thin account, and a task that replays a committed idempotency key. Every
  request is judged against the outcomes expected *for that task*, so a
  `422 INSUFFICIENT_FUNDS` counts as correct behaviour rather than as a
  failure, while a `201` on the over-debit task is reported as a failure
  because it would mean the guarantee had broken.
- `scripts/run_load_test.sh`: brings up a clean stack, records the
  environment, reconciles before, runs Locust headless, reconciles after,
  extracts the retry distribution from the API's own logs, and writes a
  verdict that fails the run on any correctness violation.
- **Fixed a real bug that this load test found, in
  `app/services/reconciliation.py`** — see assumptions below.
- Two scenarios captured, both passing:
  `tests/load/results/20260922T154105Z-scenario-5-hot-accounts/`
  `tests/load/results/20260922T154250Z-scenario-1-hot-account/`

### Phase 7 — Kubernetes manifests (2026-09-22)
- `k8s/namespace.yaml`, `configmap.yaml`, `secret.example.yaml`,
  `mongo-statefulset.yaml` (headless Service + StatefulSet),
  `mongo-init-job.yaml`, `api-deployment.yaml`, `api-service.yaml`,
  `api-hpa.yaml`.
- `scripts/verify_k8s.sh`: creates a `kind` cluster, builds and side-loads
  the image, installs metrics-server, applies everything, initiates the
  replica set, waits for rollout, then runs the Phase 5 end-to-end
  assertions through a port-forward to the Service and asserts the HPA is
  reading real CPU metrics.
- **Verified for real against `kind`, from a deleted cluster (cold run), not
  just linted.** 33/33 end-to-end assertions passed against 2 API pods
  behind a Service, and the HPA reported `cpu: 18%/70%` with
  `ScalingActive=True`.
- Raw output: `tests/concurrency/results/phase7-kubernetes-20260922T175151Z.txt`

## In Progress
Nothing in progress. Phase 7 closed; Phase 8 (CI pipeline and security
hardening) is next.

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

### Phase 4 assumptions and findings

- **Every guarantee has a paired control test.** A test asserting "no
  overdraft happened" is worth very little on its own, because a harness
  too weak to produce the race reports exactly the same result. So two
  control tests assert that the bug *does* occur when the relevant
  mechanism is removed:
  `test_control_a_plain_transaction_without_serialisation_overdraws` and
  `test_control_without_the_unique_index_duplicates_are_applied_twice`.
  Both are permanent parts of the suite. If either stops reproducing its
  bug, the corresponding guarantee tests have quietly stopped proving
  anything and should not be trusted until investigated.
- **Implicit collection creation inside a transaction silently serialises
  concurrency, and it cost a debugging round.** The idempotency control
  test initially reported that check-then-insert was safe (1 of 20
  applied). The cause was that `ledger_entries` did not exist yet in the
  scratch database, so the first insert created it implicitly *inside* the
  transaction; that takes an exclusive lock, every other transaction got a
  WriteConflict, was retried, and its retried pre-check then saw the
  committed winner. Creating both collections explicitly before the
  concurrent run changed the result to 19 of 20 applied. This is the same
  sharp edge `app/core/db.py` already avoids for the real application by
  creating every collection at startup — the incident is what confirms
  that decision was worth making. Worth remembering generally: a
  concurrency test that quietly passes may be measuring a lock you did not
  know you had.
- **Idempotency: the unique index does the work, but the error it produces
  under real concurrency is a WriteConflict, not a DuplicateKeyError.** The
  expectation was that concurrent duplicates would surface
  `DuplicateKeyError` from `uq_idempotency_key`. Measured: 0 of 19
  duplicates report `unique_index`; all 19 report `pre_check`. The reason
  is that when a transaction inserts a key another *in-flight* transaction
  has written but not yet committed, MongoDB raises WriteConflict, which
  carries the TransientTransactionError label, so `with_transaction`
  retries and the retry's pre-check sees the committed winner. The
  `unique_index` branch is the narrow case where the winner commits in the
  gap between a loser's pre-check and its insert. The index is still
  load-bearing — the control test shows removing it lets 19 of 20
  duplicates through — but the test asserts the observed distribution
  honestly rather than a split that does not occur. Two earlier attempts to
  force index catches (moving the test to funding, which has no
  serialisation lock, then calling the service directly to remove HTTP
  staggering) both still reported 0, which is what led to the correct
  explanation.
- **The serialisation counter is transactional, so it cannot evidence
  retries.** An initial test asserted the counter would exceed the number
  of committed debits under contention. It does not: the `$inc` is inside
  the transaction, so a rejected or retried attempt rolls its increment
  back. After 25 concurrent attempts of which 10 commit, the counter reads
  exactly 10. That is the stronger property and is now what the test
  asserts — a counter surviving rollback would mean the increment sat
  outside the transaction boundary and therefore was not creating the
  conflict when it needed to. Retry evidence comes instead from
  `LedgerWriteResult.attempts`, read by calling the service directly.
- **In-process ASGI concurrency is sufficient for these tests, and there is
  evidence rather than an assertion.** Requests are fired with
  `asyncio.gather` against the real app through an in-process transport;
  every request awaits real MongoDB round trips, so many are in flight at
  the database at once. The proof that this reproduces the races is that
  the control tests reliably reproduce both bugs under the same harness.
  What it does not cover is multi-process contention, which is Phase 6's
  job — hence re-running reconciliation after the load test.
- **Immutability: what is proven and what is not.** Proven: no mutating
  route exists (audited against the generated OpenAPI schema, so a route
  added later fails the test automatically), every plausible mutating HTTP
  request is refused, and the collection validator rejects direct driver
  writes that break the document shape or the sign convention, including
  flipping a DEBIT to a CREDIT and including on update. Not proven, and
  written as an executable test rather than buried in prose
  (`test_the_recorded_limit_a_privileged_update_can_still_succeed`): a
  caller with direct database credentials *can* rewrite an entry if it
  keeps shape and sign self-consistent, because a `$jsonSchema` validator
  judges only the resulting document and cannot compare it with the
  previous one. MongoDB has no per-collection append-only mode. That test
  asserts the tamper succeeds *and* that reconciliation detects it, so the
  corruption is loud rather than silent.
- **Privilege separation moved from Phase 4 to Phase 8.** The Phase 1 note
  planned to close the gap above in Phase 4 with a least-privilege MongoDB
  role (`find` + `insert` on `ledger_entries`, no `update` or `remove`).
  Moved to the Phase 8 security pass, where it belongs: it requires
  enabling authentication on the replica set, which additionally requires a
  keyfile for internal auth, and that is a change to the deployment rather
  than to the concurrency guarantees Phase 4 is about. TASK.md's own
  wording for this phase ("directly via the driver if a collection
  validator is in place") also points at validator-level enforcement here.
  The honest claim until Phase 8 is recorded under Known Issues.
- **Randomised reconciliation batch uses a fixed seed** (20260922) so a
  failure is reproducible. Randomised in shape, not in repeatability.
- **The reconciliation check is verified to be capable of failing.** Four
  corruptions are induced deliberately and each must be caught: a deleted
  credit, an orphaned pair that nets to zero, a correctly-balanced pair
  that overdraws an account, and two errors in different currencies that
  cancel globally. Three of those four leave `net_signed_minor == 0`, which
  is precisely why the report carries more than one number.

### Phase 5 assumptions and findings

- **A real bug found only by containerising: `SUPPORTED_CURRENCIES=USD,EUR`
  crashed startup.** pydantic-settings treats any list-typed field as
  "complex" and tries to `json.loads` the environment value *before* any
  validator runs, so the `mode="before"` validator written in Phase 2 never
  got a chance and the app raised `SettingsError` at boot. It passed the
  entire local suite because the development `.env` was created before that
  setting existed and so fell through to the default list. Fixed by
  annotating the field with `pydantic_settings.NoDecode`, which hands the
  raw string to the validator. Two lessons recorded rather than just the
  fix: configuration parsing is code and needs its own tests, and a test
  that constructs `Settings(**kwargs)` does not exercise the environment
  source where the bug lived. `tests/unit/test_config.py` now covers every
  documented input form *through the environment*, plus every case where
  the app is supposed to refuse to start.
- **The compose verification is genuine multi-process concurrency, which
  the Phase 4 suite is not.** `scripts/verify_compose.sh` fires each
  concurrent request from a separate `curl` process over TCP against the
  containerised server, rather than driving the ASGI app in one event loop.
  It independently reproduced both guarantees: 30 concurrent debits of 5000
  against a balance of 74000 produced exactly 14 successes and a final
  balance of exactly 4000, and 10 concurrent submissions of one idempotency
  key produced exactly 1 acceptance. That matters because it removes the
  main caveat on the Phase 4 harness.
- **`scripts/` added, which is not in ARCHITECTURE.md's tree.** TASK.md
  Phase 5 requires verifying the full flow end to end via docker-compose,
  and that is an operational check against a running stack rather than a
  unit test, so it does not belong under `tests/`. Its raw output is saved
  alongside the Phase 4 results because its overdraft and idempotency
  sections are concurrency evidence of the same kind.
- **Liveness does not touch the database; readiness does.** `/health`
  deliberately makes no database call, so a brief MongoDB outage does not
  make Docker or Kubernetes restart otherwise-healthy API containers and
  turn a partial outage into a full one. `/health/ready` does check for a
  transaction-capable primary, because an instance that cannot start a
  transaction cannot honour the atomicity guarantee and should be taken out
  of service rather than sent traffic.
- **One uvicorn worker per container, deliberately.** Scaling is horizontal
  (compose replicas, or the Phase 7 Deployment and HPA). Keeping it to one
  worker means the in-process rate limiter's known limitation is a function
  of replica count alone rather than replica count multiplied by worker
  count.
- **Environment values are listed explicitly in compose rather than via
  `env_file: .env`.** `.env` holds host-oriented values — notably
  `MONGODB_URI` pointing at `localhost` — which would be wrong inside the
  compose network and would silently override the correct value. Only
  `JWT_SECRET_KEY` is interpolated from `.env`, using
  `${JWT_SECRET_KEY:?...}` so compose fails with an actionable message
  instead of starting with a guessable key.
- **Image size is 306MB.** Not optimised further. A distroless or Alpine
  base would cut it, but Alpine's musl libc changes the wheels in play for
  bcrypt and pymongo, and that is a real risk to take on for a number that
  does not affect anything this project is demonstrating.

### Phase 6 assumptions and findings

- **The load test found a real bug, and it was in the reconciliation
  checker.** The first run reported
  `entry count 3086 is not twice the transaction count 1545` and failed.
  The ledger was fine. `reconcile()` was running several aggregations plus a
  `count_documents` as independent reads, so each observed the database at a
  slightly different instant; under concurrent writes the entry aggregation
  ran, two more transactions committed, and the transaction count then
  included them. The checker was not internally consistent.

  This mattered more than a cosmetic wrong number: a correctness checker
  that raises false alarms under load is worse than no checker, because it
  trains you to ignore it, and it would have been indistinguishable from a
  genuine half-written pair. Fixed by running every read inside one session
  with `readConcern: "snapshot"`, making the whole check a read-only
  transaction used purely for its isolation. Regression test:
  `test_reconciliation_is_self_consistent_while_writes_are_committing`,
  which reconciles 12 times while four writers commit and requires every
  report to satisfy `total_entries == total_transactions * 2`. Nice
  symmetry: the project is about read-then-write races, and the first tool
  written to detect them had one.
- **Business rejections are not failures, and the report has to say so.**
  Locust counts any non-2xx as a failure. Reporting `422
  INSUFFICIENT_FUNDS` that way would turn a perfectly correct run into an
  alarming report and would bury genuine failures among the noise. Each task
  declares which outcomes are expected for it; expected rejections are
  recorded as successes in Locust's statistics and counted separately in an
  outcome breakdown. Conversely, a `201` on the over-debit task or the
  replay task is reported as a failure, because either would mean a
  guarantee had broken. This is what RULES.md's requirement for
  distinguishable error codes is actually for.
- **Two scenarios, because one number would have been misleading.** The
  contended task's source pool size is configurable
  (`LOAD_HOT_ACCOUNT_COUNT`). With 1 hot account every virtual user debits
  the same document, which is the pathological worst case and not
  representative of anything real. With 5 it is the realistic version of the
  same problem. Reporting only the first would understate the system;
  reporting only the second would hide the worst case. Both are recorded.
- **Contention is per-account and provably does not spread.** The single
  most useful number in the whole run: uncontended transfer latency was
  p50 200ms / p95 290ms with 5 hot accounts and p50 170ms / p95 270ms with
  1 hot account — essentially unchanged, while the contended task's p95 went
  from 2500ms to 11000ms in the same runs. Accounts nobody else is touching
  do not pay for the hot account's contention. That is a direct consequence
  of the serialisation write being on the source account document rather
  than on anything global.
- **Setup traffic is measured separately.** Registration is deliberately
  slow (bcrypt at cost 12, measured at p50 490ms in the container) and runs
  in `on_start`, reported under `setup:` request names so it cannot drag the
  transaction latency figures down with it.
- **The load generator shares a machine with the service.** Both run on the
  same laptop, competing for CPU, with one uvicorn worker, one container and
  one MongoDB node, none of it tuned. These are a self-consistent baseline
  for comparing code changes, not a capacity statement about the service on
  real hardware. Recorded in each run's `environment.txt` so the numbers are
  never quoted without it.
- **Locust runs single-process on purpose.** The shared hot account pool is
  module state, which would not be shared across `--processes` workers; each
  worker would provision its own pool and the contention being measured
  would quietly disappear. A single generator process may itself be a
  limiting factor on the throughput figures, which is noted rather than
  worked around.
- **Phase 8 must not silently invalidate these numbers.** Rate limiting
  lands in Phase 8, and the default `RATE_LIMIT_TRANSACTIONS=100/minute`
  would throttle this workload to a fraction of what it achieved here. Any
  re-run must either raise the limits for the load profile or exempt it, and
  must say which it did.

### Phase 7 assumptions and findings

- **The manifests were verified by deploying them, not by linting them.**
  TASK.md allows writing them correctly and noting that live verification is
  still needed if no cluster is available. `kind` is installed, so there was
  no reason to take that option. `scripts/verify_k8s.sh` does a cold run from
  a deleted cluster and then runs the same 33 end-to-end assertions used for
  compose. This matters because three of the problems below only appear when
  you actually apply the YAML.
- **A genuine bootstrap deadlock: `publishNotReadyAddresses: true` is
  required on the headless Service.** By default a headless Service publishes
  DNS records only for pods that are Ready. The mongo pod's readiness probe
  reports ready only once it is a writable primary; it becomes a writable
  primary only when `rs.initiate()` runs; and `rs.initiate()` must address it
  by the DNS name that does not exist until it is Ready. The first run sat
  forever with the init Job logging "waiting for mongod to answer" and the
  pod at 0/1, each waiting on the other. Adding the flag fixed it
  immediately. Worth knowing for any StatefulSet whose readiness depends on
  cluster formation.
- **A StatefulSet and a headless Service, not a Deployment and a ClusterIP.**
  A replica set member must be reachable at the same stable hostname it
  advertises about itself. A Deployment gives pods random names and a
  ClusterIP load-balances across them, so an advertised address would not
  reliably resolve back to the member that published it. The member is
  registered as
  `ledgerlock-mongo-0.ledgerlock-mongo.ledgerlock.svc.cluster.local:27017`,
  verified in the init Job's output.
- **No `directConnection` needed here, unlike compose.** Because the replica
  set is initiated with a hostname that genuinely resolves inside the
  cluster, ordinary replica-set discovery works, so the URI uses
  `?replicaSet=rs0`. That is better than the compose arrangement: the driver
  will follow a primary election rather than being pinned to one node.
- **`kind load docker-image mongo:7.0` cannot work from Docker Desktop's
  containerd image store.** It fails with `content digest ... not found`
  because the locally cached image is a multi-platform manifest list and the
  other platforms' layers are not present. The script therefore tries the
  direct load, falls back to a single-platform `docker save` archive, and
  finally lets the kubelet pull, with a 420s wait to cover a cold pull. The
  first attempt at this timed out at 240s while the kubelet was still
  pulling, which looked like a hang rather than a slow download.
- **metrics-server has to be installed, and then waited for.** kind ships
  without it, so the HPA would report `<unknown>` targets and could not
  scale — an HPA that cannot read metrics is a manifest, not an autoscaler.
  It also needs `--kubelet-insecure-tls` on kind, because kind's kubelet
  serving certificates are not signed by the cluster CA. Separately, metrics
  take 60-90 seconds after a pod starts before the HPA can compute a
  utilisation figure: measured `<unknown>` immediately after the rollout and
  `cpu: 3%/70%` about 90 seconds later. The script now waits for
  `ScalingActive=True` and fails if it never arrives, rather than printing
  `<unknown>` and calling it verified.
- **No CPU limit on the API container, deliberately.** Under write-conflict
  retries the service is briefly CPU-hungry in bursts. A CPU limit would
  cause CFS throttling, which shows up as latency on exactly the requests
  that are already slow from contention. Memory is still capped, because
  memory exhaustion cannot be absorbed by the scheduler the same way. CPU
  *requests* are set because the HPA computes utilisation against them.
- **Liveness and readiness differ on purpose, in both workloads.** The API's
  liveness probe hits `/health`, which makes no database call, so a MongoDB
  outage does not cause Kubernetes to restart every healthy API pod and
  escalate a partial outage into a total one. Readiness hits
  `/health/ready`, which requires a transaction-capable primary, so a pod
  that cannot honour the atomicity guarantee is removed from the Service's
  endpoints instead of being sent traffic. Mongo's probes make the same
  split for the same reason, and its liveness probe deliberately does *not*
  require replica set membership, or an uninitiated node would be restarted
  forever and could never be initiated.
- **Scaling out does not fix single-account contention, and the HPA manifest
  says so.** Phase 6 measured 22.88x callback amplification on one hot
  account. That cost is a function of how many clients want the same account,
  not of how many pods serve them; more pods would simply let more requests
  queue for the same document. Recorded in the manifest so it is not mistaken
  for a remedy.
- **The JWT secret is never in a file in the repository.**
  `k8s/secret.example.yaml` is a template with an empty value and an
  explanation, and `verify_k8s.sh` creates the Secret imperatively from a
  freshly generated key. A committed placeholder is how an example signing
  key reaches production. The template also notes that a Kubernetes Secret is
  only base64-encoded at rest unless etcd encryption is configured, so
  anything beyond a local demonstration wants a real secret manager.
- **Files added beyond ARCHITECTURE.md's four k8s manifests.**
  `namespace.yaml`, `secret.example.yaml`, `mongo-statefulset.yaml` and
  `mongo-init-job.yaml`. The four named files cannot run without a database
  and a namespace to run in, and TASK.md asks for the manifests to be tested
  against a local cluster, which is impossible without them. No named file
  was renamed or dropped.

## Known Issues

- **Extreme single-account contention produces very long tail latencies, and
  nothing currently bounds them.** Measured in Phase 6 Scenario B: with 50
  concurrent clients all debiting one account, the contended endpoint reached
  p99 16000ms and a maximum of 33173ms, with one transaction needing 463
  callback attempts before committing. No request failed and no guarantee was
  violated — correctness is unaffected — but a 33-second request is not
  acceptable behaviour to ship.

  The cause is that `with_transaction` retries a write conflict until its own
  ~120 second deadline, with no fairness between contenders, so under heavy
  contention some transactions starve while others get through. The obvious
  fix is a bounded retry budget in `ledger_service`: after N attempts or T
  milliseconds, stop and return `409 WRITE_CONFLICT` so the client can retry
  deliberately instead of holding a connection open for half a minute. The
  error code and its handling already exist for exactly this case; what is
  missing is the budget that would trigger it.

  Not implemented, because it is a change to the Phase 3 core and belongs in
  its own piece of work rather than being slipped in while writing a load
  test. Recorded here with the measurements that justify it. Note that the
  realistic scenario is far milder (p99 4200ms, max attempts 121), so this is
  the worst case rather than the normal case.

- **Rate limiting is in-process.** `slowapi` keeps its counters in the
  API process's own memory. Running more than one replica (which the
  Phase 7 Deployment/HPA manifests explicitly do) means each replica
  enforces its own independent limit, so the effective global limit is
  roughly `limit × replica count`. Making this correct would need a
  shared backend such as Redis. Recorded here honestly rather than
  claimed as a distributed rate limiter.
- **Ledger immutability is application-and-validator level, not
  unbypassable.** Measured in Phase 4 rather than assumed. What holds: no
  update or delete route exists anywhere in the API (audited against the
  generated OpenAPI schema), and the `$jsonSchema` + `$expr` collection
  validator rejects any direct driver write that breaks an entry's shape or
  its sign convention, including flipping a DEBIT to a CREDIT, and
  including on update. What does not hold: a caller with direct database
  credentials can rewrite an entry if it keeps shape and sign
  self-consistent, for example changing `amount_minor` and
  `signed_amount_minor` together. A `$jsonSchema` validator judges only the
  resulting document and cannot compare it with the previous version, and
  MongoDB has no per-collection append-only mode.

  This gap is covered by an executable test
  (`test_the_recorded_limit_a_privileged_update_can_still_succeed`) which
  asserts the tamper succeeds *and* that reconciliation detects it
  (`healthy=False`, `net_signed_minor=−7999`,
  `unbalanced_transaction_groups=1`). Detection is not prevention, but it
  is the difference between silent corruption and loud corruption.

  **Fix scheduled for Phase 8:** a least-privilege MongoDB role granting
  `find` and `insert` on `ledger_entries` but not `update` or `remove`,
  since MongoDB privilege actions are per-collection. That turns "the
  application never updates entries" into "the application's credentials
  cannot update entries". It requires enabling authentication on the
  replica set, which additionally requires a keyfile for internal auth
  (generated at container start, never committed). When that lands, the
  test above must be inverted to assert the write is refused.

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

### Phase 4 — Concurrency guarantees
Command: `.venv/bin/python -m pytest tests/concurrency -v -s`
Run on 2026-09-22T15:14:27Z UTC. Python 3.12.9, MongoDB 7.0.43, replica set
`rs0` (single node via docker-compose), Darwin 25.6.0 arm64.
Result: **47 passed in 28.97s**
Raw output: `tests/concurrency/results/phase4-concurrency-20260922T151427Z.txt`
Full suite at the same commit: **159 passed in 38.41s**. `ruff` and `black`
clean.

**Overdraft, under concurrency**

| Scenario | Requests | Succeeded | Final balance | Notes |
|---|---|---|---|---|
| 50 concurrent debits of 1000, opening balance 10000 | 50 | **10** | **0** | 40 rejected, all INSUFFICIENT_FUNDS |
| 2 concurrent debits of 6000, opening balance 10000 | 2 | **1** | **4000** | the textbook lost-update case |
| 10 concurrent debits of mixed sizes, opening balance 10000 | 10 | 3 | **100** | total debited 9900, never exceeded available |
| 100 concurrent debits of 1 minor unit, opening balance 10000 | 100 | **100** | **9900** | a lost update would show as a balance above 9900 |
| 10 senders x 5000 into one account | 10 | **10** | recipient **50000** | different sources, so no serialisation conflict |
| A and B each sending their whole 5000 to the other | 2 | 2 | A 5000, B 5000, **sum 10000** | no value created or destroyed |

**Control: the same scenario with the serialisation write removed**

| Implementation | Requests | Succeeded | Final balance |
|---|---|---|---|
| Naive: `with_transaction` around read-check-write | 20 x 1000 against 10000 | **20** | **−10000 (overdrawn)** |
| Ledgerlock | 20 x 1000 against 10000 | **10** | **0** |

**Retry behaviour under maximum contention** (30 concurrent transfers on one
source account, measured via `LedgerWriteResult.attempts`):
- all 30 committed, **0** WRITE_CONFLICT errors reached any caller
- attempts per transfer ranged **1 to 44**
- **646 total transaction callback runs for 30 commits**, i.e. roughly 21x
  amplification when every request contends on the same account. This is the
  real cost of the guarantee and the number to watch in Phase 6. It is a
  worst case: contention is per-account, so unrelated accounts do not pay
  it (the 10-sender fan-in test committed 10 of 10 with no contention).
- serialisation counter after 25 concurrent attempts of which 10 committed:
  exactly **10** (rolled-back attempts leave no residue)

**Idempotency, under concurrency**

| Scenario | Requests | Accepted | Ledger effect |
|---|---|---|---|
| 20 concurrent submissions, one shared key | 20 | **1** (19 x 409 DUPLICATE_SUBMISSION) | 1 transaction document, exactly 4 entries total (2 funding + 2 transfer), destination credited **2500** once |
| 15 concurrent submissions, one shared key | 15 | **1** | exactly 2 entries for that transaction, 1 DEBIT + 1 CREDIT, netting to 0 |
| 12 concurrent duplicate *funding* requests | 12 | **1** | balance **75000**, not 900000 — a duplicate here would mint money |
| 10 concurrent duplicates of an unaffordable transfer | 10 | 0 | nothing written, and the key was **not** consumed: a corrected retry with the same key then returned 201 |
| 10 concurrent transfers with 10 *distinct* keys | 10 | **10** | destination credited 10000 — idempotency must not deduplicate genuine payments |

Detection mechanism, measured: of 19 concurrent duplicates, **19 reported by
the pre-check read and 0 by the unique index**. Explanation in the
assumptions section above; the index is still what makes it work.

**Control: idempotency with the unique index removed**

| Implementation | Requests (one shared key) | Applied | Destination credited |
|---|---|---|---|
| Check-then-insert, no unique index | 20 | **19** | **95000** for a single 5000 transfer |
| Ledgerlock | 20 | **1** | 5000 |

**Immutability**
- OpenAPI audit: **0** PUT/PATCH/DELETE routes anywhere in the API
- 10 plausible mutating HTTP requests against ledger entries, transactions
  and balances: all refused (404/405)
- direct driver `$set amount_minor`, `$set signed_amount_minor`, `$set
  direction: CREDIT`, `$set` of an unexpected field, `$unset` of a required
  field: **all rejected** by the collection validator
- injecting a second DEBIT into an existing transaction: **rejected** by
  `uq_one_entry_per_direction_per_transaction`
- a committed entry is **byte-identical** after 5 further transfers on the
  same accounts
- compensating transaction: after a mistaken 3000 transfer and a 3000
  reversal, the original 2 entries are untouched, total entries 6, both
  balances back to their starting values
- **known gap, tested and reported:** a direct-database update that keeps
  shape and sign self-consistent *does* succeed. Reconciliation caught it —
  `healthy=False`, `net_signed_minor=−7999`,
  `unbalanced_transaction_groups=1`

**Reconciliation**

| Scenario | Entries | Transactions | net_signed_minor | healthy |
|---|---|---|---|---|
| Empty ledger | 0 | 0 | **0** | True |
| 104 concurrent randomised transfers, 8 accounts, 2 currencies (seed 20260922) | 122 | 61 | **0** | True |
| 80 concurrent transfers out of one account (heavy retry churn) | — | — | **0** | True |

For the randomised batch: 53 accepted, 43 rejected CURRENCY_MISMATCH, 8
rejected INSUFFICIENT_FUNDS; per-currency net `{USD: 0, EUR: 0}`; entries
without a transaction 0; unbalanced groups 0; negative USER accounts none;
`total_entries == total_transactions * 2` exactly.

Boundary cross-check: after funding 12000 + 7500 + 33333 and moving money
between the accounts, the SYSTEM account balance was **−52833** and total
USER holdings **52833** — equal and opposite, exactly.

**The reconciliation check is verified to be able to fail**

| Induced corruption | net_signed_minor | balanced | What caught it |
|---|---|---|---|
| Deleted the CREDIT of a pair | −1500 | False | net, plus 1 unbalanced group |
| Orphaned pair belonging to no transaction | **0** | True | `entries_without_transaction=2` |
| Valid pair that overdraws an account | **0** | True | `negative_user_accounts` |
| −700 USD and +700 EUR, cancelling globally | **0** | False | per-currency net |

Three of those four leave the global net at zero, which is why the report
carries more than one number.

### Phase 5 — End-to-end verification against docker-compose
Command: `./scripts/verify_compose.sh`
Run on 2026-09-22T15:26:34Z UTC against a freshly built stack with empty
volumes. API image `ledgerlock-api:local` (python:3.12.9-slim, non-root uid
1001, `read_only: true`), MongoDB 7.0 single-node replica set `rs0`.
Result: **33 assertions passed, 0 failed**
Raw output: `tests/concurrency/results/phase5-compose-e2e-20260922T152634Z.txt`
Full test suite at the same commit: **197 passed** (159 + 38 new
configuration tests).

Unlike the Phase 4 suite, each concurrent request here comes from a separate
`curl` process over TCP, so this is genuine multi-process concurrency against
the containerised server.

| Step | Result |
|---|---|
| Readiness reports a transaction-capable primary | `{"status":"ready","replica_set":"rs0"}` |
| New account balance, computed with no entries | exactly **0**, entry_count 0 |
| After funding 100000 | balance **100000**, entry_count 1 |
| Transfer of 25000 | source **75000**, destination **25000** |
| 10 concurrent submissions of one idempotency key | **1 x 201, 9 x 409**; destination moved from 25000 to **26000**, i.e. applied once |
| 30 concurrent debits of 5000 against a balance of 74000 | **14 x 201, 16 x 422**; final balance exactly **4000**; never negative |
| Reconciliation after that load | `net_signed_minor` **0**, `balanced` true, `healthy` true, 34 entries, 17 transactions, `total_entries == total_transactions * 2` |
| PUT / PATCH / DELETE on a transaction | all **405** |
| PUT on a balance | **405** (there is no stored balance to write) |

Image hygiene, verified by inspecting the built image: process runs as
`uid=1001(ledgerlock)`, `/app` contains only `app/`, and the image contains
no `.env`, no `tests/`, and no `.git`.

### Phase 6 — Load test
Command: `LOAD_HOT_ACCOUNT_COUNT=<n> ./scripts/run_load_test.sh 50 10 60s`
Run on 2026-09-22, 50 virtual users, spawn rate 10/s, 60 seconds, against
the docker-compose stack (1 uvicorn worker, 1 container, MongoDB 7.0.43
single-node replica set). Locust 2.46.6, single process.
Host: Darwin 25.6.0 arm64, load generator on the same machine as the
service. See each run's `environment.txt` for the full caveats; these are a
baseline for comparing changes, not a capacity claim.

**Scenario A — 5 shared hot source accounts (realistic contention)**
`tests/load/results/20260922T154105Z-scenario-5-hot-accounts/`

| Request | n | failures | p50 ms | p95 ms | p99 ms | req/s |
|---|---|---|---|---|---|---|
| POST /transactions [uncontended] | 2721 | **0** | **200** | **290** | 370 | 46.0 |
| POST /transactions [contended, 5 hot accounts] | 2181 | **0** | 550 | 2500 | 4200 | 36.8 |
| POST /transactions [overdraft attempt, must reject] | 563 | **0** | 190 | 420 | 610 | 9.5 |
| POST /transactions [replayed key, must reject] | 538 | **0** | 100 | 160 | 250 | 9.1 |
| GET /accounts/{id}/balance | 1726 | **0** | 67 | 110 | 150 | 29.1 |
| **Aggregated** | **7929** | **0** | **190** | **1400** | 2900 | **133.9** |

**Scenario B — 1 shared hot source account (pathological worst case)**
`tests/load/results/20260922T154250Z-scenario-1-hot-account/`

| Request | n | failures | p50 ms | p95 ms | p99 ms | req/s |
|---|---|---|---|---|---|---|
| POST /transactions [uncontended] | 804 | **0** | **170** | **270** | 380 | 13.6 |
| POST /transactions [contended, 1 hot account] | 629 | **0** | 2700 | 11000 | 16000 | 10.6 |
| POST /transactions [overdraft attempt, must reject] | 162 | **0** | 150 | 270 | 480 | 2.7 |
| POST /transactions [replayed key, must reject] | 145 | **0** | 87 | 160 | 290 | 2.4 |
| GET /accounts/{id}/balance | 534 | **0** | 58 | 99 | 160 | 9.0 |
| **Aggregated** | **2474** | **0** | **160** | **6500** | 12000 | **41.7** |

Read those two tables together. Concentrating all contention on one account
instead of five costs the contended path roughly 4x its p95 and cuts overall
throughput to about a third, **while leaving uncontended transfer latency
essentially unchanged (p95 290ms vs 270ms)**. Contention is per-account and
does not spread to accounts nobody else is touching.

**Retry cost, measured from `ledger_service`'s own `attempts` field**

| | 5 hot accounts | 1 hot account |
|---|---|---|
| committed transactions | 5030 | 1512 |
| total transaction callback runs | 22699 | 34593 |
| amplification | **4.51x** | **22.88x** |
| committed on the first attempt | 3173 (63.1%) | 886 (58.6%) |
| needed at least one retry | 1857 (36.9%) | 626 (41.4%) |
| median attempts | 1 | 1 |
| max attempts | 121 | **463** |

The 22.88x figure independently corroborates the ~21x measured in Phase 4's
30-request contention test, from a completely different harness.

**Reconciliation immediately after each run — the number that matters**

| | 5 hot accounts | 1 hot account |
|---|---|---|
| `net_signed_minor` | **0** | **0** |
| `per_currency_net_minor` | `{USD: 0}` | `{USD: 0}` |
| total entries | 10050 | 3010 |
| total transactions | 5025 | 1505 |
| `total_entries == total_transactions * 2` | yes | yes |
| entries without a transaction | 0 | 0 |
| unbalanced transaction groups | 0 | 0 |
| negative USER accounts | **0** | **0** |
| `healthy` | **true** | **true** |

Outcome breakdown, Scenario A: 2721 uncontended transfers committed, 2181
contended transfers committed, 1726 balance reads, 563 over-debits rejected
with INSUFFICIENT_FUNDS, 538 replays rejected with DUPLICATE_SUBMISSION,
**0 unexpected outcomes**. Every over-debit attempt was rejected and every
replayed key was rejected, across both runs, without exception.

Full suite at this commit: **198 passed**.

### Phase 7 — Kubernetes verification against kind
Command: `./scripts/verify_k8s.sh` (full cold run from a deleted cluster)
Run on 2026-09-22T17:51:51Z UTC. kind v0.33.0, node image
`kindest/node:v1.37.0`, kubectl v1.36.1, Darwin 25.6.0 arm64.
Raw output: `tests/concurrency/results/phase7-kubernetes-20260922T175151Z.txt`

Deployed: MongoDB 7.0 StatefulSet as single-node replica set `rs0`, API
Deployment with 2 replicas behind a ClusterIP Service, HPA, metrics-server.

| Check | Result |
|---|---|
| Replica set initiated with a resolvable member address | `set=rs0`, `member=ledgerlock-mongo-0.ledgerlock-mongo.ledgerlock.svc.cluster.local:27017 state=PRIMARY` |
| API replicas Ready | **2 of 2** |
| Pods in the Service's endpoints (readiness-gated) | **2** |
| End-to-end assertions through the Service | **33 passed, 0 failed** |
| HPA metrics | `cpu: 18%/70%`, `memory: 26%/80%` |
| HPA `ScalingActive` | **True** — "able to successfully calculate a replica count from cpu resource utilization" |
| HPA `currentReplicas` | 2 (at `minReplicas`) |

This is the strongest concurrency evidence in the project, because the
assertions ran against **two separate API pods sharing one MongoDB**, reached
through the Service so requests were spread across both. Earlier phases
covered one event loop (Phase 4) and one container with many client processes
(Phase 5); this covers many processes across many pods.

| Concurrency assertion, across 2 pods | Result |
|---|---|
| 30 concurrent debits of 5000 against a balance of 74000 | **14 x 201, 16 x 422**, final balance exactly **4000**, never negative |
| 10 concurrent submissions of one idempotency key | **1 x 201, 9 x 409**, applied exactly once |
| Reconciliation immediately after | `net_signed_minor` **0**, `healthy` **true**, 34 entries / 17 transactions |
| PUT/PATCH/DELETE on ledger data | all **405** |

### Later phases
- CI pipeline and security hardening: not done yet (Phase 8)
