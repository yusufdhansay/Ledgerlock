# Ledgerlock — Development Rulebook

## General Principles
- Correctness over speed of delivery. This project's entire value is
  that its concurrency guarantees (no overdraft, no duplicate
  application, reconciliation to zero) actually hold under real
  concurrent load. Do not skip or weaken a test to make a phase "done"
  faster.
- No invented metrics anywhere, not in code comments, not in README,
  not in commit messages. If a number isn't from an actual test run
  you executed, don't write it down.
- Every phase must leave the repo in a working, tested state before
  you move to the next phase.
- If you hit a genuine architectural ambiguity not resolved in PRD.md
  or ARCHITECTURE.md, write your decision and reasoning into
  `MEMORY.md` under `## Assumptions` and proceed. Don't block.

## Tech and Coding Standards
- Python 3.12, type hints on all function signatures
- Format with `black`, lint with `ruff`
- All routes and services use Motor's async API properly, no blocking
  calls inside async routes
- All secrets (DB creds, JWT signing key) come from environment
  variables, never hardcoded, never committed. Provide a `.env.example`
  with placeholder values.
- All external input is validated with Pydantic before it reaches any
  service function
- Never build a MongoDB query from raw, unvalidated user input. Use
  the driver's parameterized query construction, never string-
  concatenate or `eval` anything into a query. Explicitly disallow
  operators like `$where` from ever being constructed from user input,
  this is MongoDB's equivalent of SQL injection and must be treated
  with the same seriousness
- Every write to `transactions` or `ledger_entries` happens inside a
  MongoDB session transaction via `ledger_service.py`. No other module
  writes to those collections, ever
- Balance is never stored as a mutable field on the account document.
  If you find yourself wanting to add one "for performance," don't,
  that reintroduces the exact bug class this project exists to prevent.
  If caching becomes genuinely necessary later, it must be documented
  as a cache with explicit invalidation, not a source of truth
- Write a test for every piece of logic that touches balances,
  transaction creation, or idempotency before considering that logic
  done

## Project Structure
- Follow the folder structure in ARCHITECTURE.md exactly, don't
  reorganize it
- Tests mirror the service structure: `tests/unit/test_<module>.py`
- Never commit `.env`, generated secrets, or database data

## Error Handling
- Never fail silently. Log every rejected transaction with a specific
  reason (insufficient funds, duplicate idempotency key, invalid
  account status)
- Return specific, distinguishable error codes for: insufficient
  funds, duplicate submission, invalid account, malformed request,
  internal error, so tests and load test reports can distinguish
  failure types
- If a phase's tests fail and you can't fix it within reasonable
  effort, log it clearly in `MEMORY.md` under `## Known Issues`,
  don't hide it or mark the phase complete anyway
