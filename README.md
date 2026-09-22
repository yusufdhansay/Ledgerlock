# Ledgerlock

A double-entry bookkeeping API on MongoDB. Every movement of money is a
matched pair of immutable ledger entries; balances are always computed by
aggregation, never stored as a mutable field.

This README is finalised in Phase 9, once every measured number it will
quote has actually been produced by a test run. Until then, see:

- [`PRD.md`](PRD.md) — what this is and why
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — stack, structure, how the parts connect
- [`RULES.md`](RULES.md) — the development rulebook
- [`DESIGN.md`](DESIGN.md) — API and code-level conventions
- [`TASK.md`](TASK.md) — the phased build plan
- [`MEMORY.md`](MEMORY.md) — build log, assumptions, known issues, measured numbers

## Run it

```bash
# 1. Configuration. There is no default JWT signing key: the app refuses
#    to start without one, and refuses the .env.example placeholder.
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # paste into JWT_SECRET_KEY

# 2. Bring up MongoDB (single-node replica set, required for transactions)
#    and the API.
docker compose up -d --build

# 3. Verify the whole flow end to end: register, open accounts, fund,
#    transfer, prove no overdraft under 30 concurrent debits, prove a
#    replayed idempotency key applies once, then reconcile the ledger.
./scripts/verify_compose.sh
```

API docs are at http://localhost:8000/docs once the stack is up.

## Run the tests

```bash
# MongoDB must be running (docker compose up -d mongo). The suite runs
# against a real replica set, not a mock: the guarantees under test are
# properties of MongoDB's transaction layer.
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q

# The concurrency suite on its own, with its measured output
.venv/bin/python -m pytest tests/concurrency -v -s
```
