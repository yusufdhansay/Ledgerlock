# Ledgerlock — Architecture

## High-Level Architecture

Client --(POST /transactions, with Idempotency-Key)--> FastAPI service
--(MongoDB multi-document transaction: check idempotency key, check
sufficient balance via aggregation, insert transaction doc, insert
2 ledger entries)--> MongoDB (single-node replica set)

Client --(GET /accounts/{id}/balance)--> FastAPI service
--(aggregation pipeline over ledger_entries)--> MongoDB --> computed
balance returned, never a stored field

Key property: every write that touches money (transaction creation,
the two resulting ledger entries) happens inside a single MongoDB
session-scoped transaction. If any step fails, the whole write rolls
back, there is no partial state where a debit entry exists without
its matching credit entry.

## Tech Stack

- Language: Python 3.12
- API: FastAPI
- Database: MongoDB 7, accessed via Motor (async driver), run as a
  single-node replica set locally (required for MongoDB's
  multi-document ACID transactions)
- Schema/validation: Pydantic models application-side; MongoDB
  `$jsonSchema` collection validators as a second, database-level
  layer of defense
- Auth: JWT-based, following the pattern in the inspiration repo
  (bcrypt for password hashing, a token blacklist collection for
  logout/revocation)
- Rate limiting: in-process (e.g. `slowapi`), documented in
  MEMORY.md as a known limitation if the service is ever run with
  multiple replicas (in-process limits don't share state across
  instances, same honest-limitation pattern used in this developer's
  prior project)
- Containers: Docker, `docker-compose` for local orchestration
- Orchestration: Kubernetes manifests (Deployment, Service, HPA),
  tested locally against `kind` or `minikube`
- Testing: `pytest`, `pytest-asyncio` for concurrency tests
- Load testing: Locust
- CI: GitHub Actions (lint, test, build image on every push)

## Folder Structure

```
ledgerlock/
├── PRD.md
├── ARCHITECTURE.md
├── RULES.md
├── DESIGN.md
├── TASK.md
├── MEMORY.md
├── docker-compose.yml
├── .github/workflows/ci.yml
├── k8s/
│   ├── api-deployment.yaml
│   ├── api-service.yaml
│   ├── api-hpa.yaml
│   └── configmap.yaml
├── app/
│   ├── main.py
│   ├── models/
│   │   ├── user.py
│   │   ├── account.py
│   │   ├── transaction.py
│   │   └── ledger_entry.py
│   ├── routes/
│   │   ├── auth.py
│   │   ├── accounts.py
│   │   └── transactions.py
│   ├── services/
│   │   ├── ledger_service.py     # the core: atomic transaction + entry writes
│   │   ├── balance_service.py    # aggregation-based balance computation
│   │   └── reconciliation.py     # system-wide sum-to-zero check
│   └── core/
│       ├── config.py
│       ├── security.py
│       └── db.py
├── tests/
│   ├── unit/
│   ├── concurrency/               # overdraft and duplicate-submission tests
│   └── load/                      # Locustfile
└── README.md
```

## How the parts connect

- `ledger_service.py` is the only code path allowed to write to the
  `transactions` or `ledger_entries` collections. No route or other
  service writes to them directly, this keeps the atomicity and
  ordering guarantees in one place instead of scattered across routes
- Every write in `ledger_service.py` happens inside a single MongoDB
  client session with `with_transaction`, covering: idempotency key
  check, balance-sufficiency check, transaction document insert, both
  ledger entry inserts. If sufficiency check and the writes weren't in
  the same transaction, two concurrent debits could both pass the
  check before either commits, that ordering is the entire point
- `balance_service.py` never reads a stored balance field, because
  none exists, it always runs an aggregation over `ledger_entries`
  filtered by account
- Ledger entry immutability is enforced at the application layer (no
  update or delete route exists for that collection) and reinforced
  by a MongoDB collection validator rejecting writes that don't match
  the expected shape; document in RULES.md and MEMORY.md that this is
  an application-and-validator-level guarantee, not an unbypassable
  database-level one, same honest-limitation standard as this
  developer's other projects
