# Ledgerlock — API Design Conventions

There is no frontend for this project (see PRD.md, explicitly out of
scope). This document defines API and code-level design conventions
instead of visual design, since that's what this project actually
needs to be consistent and reviewable.

## API Design Principles
- RESTful resource naming: `/accounts`, `/accounts/{id}/balance`,
  `/transactions`, not verb-based endpoints
- Every mutating endpoint that creates a transaction requires an
  `Idempotency-Key` header, documented clearly in the endpoint's
  docstring/OpenAPI description, not just implemented silently
- Consistent error response shape across every endpoint:
  `{"error": {"code": "...", "message": "..."}}`, so a client (or a
  test) can distinguish failure types programmatically, not by
  parsing a free-text message
- Money amounts are represented as integers (minor units, e.g. cents/
  paise), never floats, to avoid floating-point rounding errors in
  financial calculations
- Timestamps in ISO 8601, UTC, always

## Response conventions
- List endpoints are paginated, no unbounded result sets
- A balance response includes both the computed balance and the
  currency, never a bare number
- Every response for a transaction includes its status
  (PENDING/COMPLETED/FAILED/REVERSED) explicitly, a client should
  never have to infer status from HTTP status code alone

## Code-level "design" standard
- Service functions are the only layer that contains business logic;
  routes are thin, they validate input, call a service function,
  and shape the response, nothing more
- Prefer explicit, readable function and variable names over
  clever abbreviations, this project should be legible to someone
  reviewing it in an interview without a walkthrough
