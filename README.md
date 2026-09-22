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

## Local setup

```bash
# 1. MongoDB as a single-node replica set (required for transactions)
docker compose up -d

# 2. Python environment
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 3. Configuration
cp .env.example .env
# then set JWT_SECRET_KEY:
#   python -c "import secrets; print(secrets.token_urlsafe(64))"

# 4. Verify the database actually supports multi-document transactions
.venv/bin/python -m pytest tests/unit/test_mongo_transactions.py -v
```
