# Ledgerlock — Task Breakdown

Work through these phases in order. Complete, test, update MEMORY.md,
commit and push after each one before starting the next.

## Phase 0: Scaffolding
- Create the six root docs (PRD, ARCHITECTURE, RULES, DESIGN, TASK, MEMORY)
- Create folder structure per ARCHITECTURE.md
- Set up `pyproject.toml` or `requirements.txt`, `.env.example`,
  `.gitignore`
- Set up MongoDB as a single-node replica set in docker-compose
  (required for transactions), verify it actually supports
  `with_transaction` before building anything on top of it
- Initialize git repo if not already done, first commit

## Phase 1: Models and auth
- Define Pydantic models: User, Account, Transaction, LedgerEntry,
  matching ARCHITECTURE.md
- Implement JWT auth (register, login, token blacklist on logout)
- Unit tests for auth flows
- Commit: "Phase 1: models and authentication"

## Phase 2: Accounts and balance computation
- Account creation endpoint
- Balance computed via aggregation over `ledger_entries`, never a
  stored field, per RULES.md
- Unit tests: balance is correct after manually inserting known
  ledger entries; balance for an account with zero entries is zero
- Commit: "Phase 2: accounts and aggregation-based balance"

## Phase 3: Transaction creation, the atomic core
- Implement `ledger_service.py`: single MongoDB session transaction
  covering idempotency check, sufficiency check, transaction insert,
  both ledger entry inserts
- Unit tests: a valid transaction produces exactly one debit and one
  credit entry, both referencing the same transaction ID; an
  insufficient-funds transaction is rejected and produces no entries
- Commit: "Phase 3: atomic transaction creation"

## Phase 4: Concurrency guarantees, the core of the project
- Overdraft test: an account with a known balance receives N
  simultaneous debit requests whose combined total exceeds the
  balance; assert the account never goes negative, and assert exactly
  the correct subset of requests succeeded given the balance and
  ordering
- Idempotency test: the same transaction request (same idempotency
  key) submitted N times concurrently; assert exactly one set of
  ledger entries exists afterward
- Immutability test: attempt to update or delete a ledger entry
  through every available path (API, and directly via the driver if
  a collection validator is in place); assert all attempts fail
- Reconciliation test: after running a batch of randomized valid
  transactions, sum every ledger entry system-wide and assert it
  equals exactly zero
- Save raw test output for all of the above into
  `tests/concurrency/results/`
- Commit: "Phase 4: concurrency guarantees, no overdraft and idempotent writes"

## Phase 5: Dockerize and compose
- Dockerfile for the API service
- `docker-compose.yml` wiring MongoDB (replica set) and the API
  together
- Verify the full flow (account creation, transaction, balance check,
  reconciliation) works end to end via docker-compose
- Commit: "Phase 5: dockerized service with compose orchestration"

## Phase 6: Load testing
- Write a Locust load test simulating concurrent transaction
  submissions across multiple accounts, including some deliberately
  colliding on the same account to keep exercising the overdraft
  guarantee under load, not just uncontended throughput
- Run it, capture real throughput (req/sec) and latency (p50/p95)
  numbers, save the raw report into `tests/load/results/`
- Re-run the reconciliation check immediately after the load test and
  save that result too, this is the number that actually proves
  correctness survived real load, not just the throughput figure
- Commit: "Phase 6: load test with captured baseline numbers"

## Phase 7: Kubernetes manifests
- Write Deployment, Service, and HPA manifests for the API service
- Test locally against `kind` or `minikube` if available; if not
  available in this environment, write the manifests correctly and
  note in MEMORY.md that live cluster verification is still needed
- Commit: "Phase 7: Kubernetes manifests"

## Phase 8: CI and security pass
- GitHub Actions workflow: lint, run full test suite (including
  spinning up a MongoDB replica set as a service container), build
  Docker image on every push
- Security pass: rate limiting on public endpoints, dependency
  vulnerability scan (`pip-audit` or similar), confirm no secrets in
  repo, confirm all input is validated, confirm no user input can
  construct a raw MongoDB query operator
- Commit: "Phase 8: CI pipeline and security hardening"

## Phase 9: Final README and number consolidation
- Write a top-level README summarizing what the project does, the
  architecture diagram, and every real measured number from Phase 4
  and Phase 6, clearly labeled with how each was produced
- Explicitly state in the README that MongoDB was chosen deliberately
  to prove this guarantee without relying on a relational unique
  constraint, and briefly explain how the transaction-based approach
  differs from that
- Commit: "Phase 9: final README with verified metrics"
