# Ledgerlock — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 1: Models and auth — not started

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
- Commit: see git log for `Phase 0: scaffolding` / hash recorded below

## In Progress
Nothing in progress. Phase 0 closed; Phase 1 (models + auth) is next.

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

### Later phases
- Overdraft test: not run yet (Phase 4)
- Idempotency test: not run yet (Phase 4)
- Reconciliation: not run yet (Phase 4, re-run after Phase 6 load test)
- Load test: not run yet (Phase 6)
