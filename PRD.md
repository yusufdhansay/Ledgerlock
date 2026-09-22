# Ledgerlock — Product Requirements

## Product Overview
Ledgerlock is a double-entry bookkeeping API. Every movement of money
between two accounts is recorded as a matched pair of immutable ledger
entries, a debit on one account and a credit on the other, for the same
amount. An account's balance is never stored as a mutable field, it is
always computed on demand by aggregating that account's ledger entries.
This is the structurally correct way to build anything that tracks
money, because it makes certain classes of bugs (balance drift, lost
updates, silently duplicated transfers) impossible by construction
rather than something application code has to carefully avoid.

Ledgerlock is an original system, inspired by standard double-entry
bookkeeping principles (the same ones any real accounting or payments
system uses), not a clone of any specific product or company's ledger
implementation. The design decisions here are derived from the
accounting invariants themselves: entries are immutable, every
transaction nets to zero, and balance is a derived quantity.

## Problem Statement
Most portfolio-level "banking API" projects store balance as a mutable
integer field and update it directly, which is exactly the pattern
that causes real financial bugs under concurrent load: two simultaneous
debits both read the same stale balance, both pass a sufficient-funds
check, and the account goes negative. The real, interesting engineering
problem is proving a ledger stays correct, no lost updates, no
double-application of a transaction, no possibility of an account going
negative, even when many concurrent requests hit the same account at
once. This project exists to build and prove that guarantee, on
MongoDB specifically, which does not hand you the same relational
constraints a SQL database would, so the guarantee has to be built
deliberately rather than borrowed from a UNIQUE constraint.

## Goals
- Guarantee no overdraft: an account's balance can never go negative,
  even under many concurrent debit attempts that individually appear
  to have sufficient funds
- Guarantee idempotent transaction submission: resubmitting the same
  transaction (client retry, network duplicate) never applies it twice
- Guarantee a global reconciliation invariant: the sum of every ledger
  entry in the entire system always nets to zero, checked automatically
  after load, not just claimed
- Guarantee ledger entry immutability: once written, an entry can never
  be altered or deleted through the application
- Produce real, measured numbers (concurrency test results, load test
  throughput/latency) from actual test runs, not estimates

## Target Users
This is a portfolio/reference system, not a production financial
product. "Users" are: (1) the developer demonstrating correct
distributed-data-under-concurrency engineering, (2) anyone reviewing
the code or architecture in a technical interview context.

## Core Features (MVP)
1. User accounts, each with a currency and a status (ACTIVE, FROZEN,
   CLOSED)
2. Transactions between two accounts, each producing exactly two
   immutable ledger entries (one DEBIT, one CREDIT), written atomically
3. Idempotency keys on every transaction request; a resubmitted key
   is rejected, not reprocessed
4. Balance is always computed via aggregation over an account's ledger
   entries, never read from a stored field
5. Overdraft prevention: a debit that would take an account below zero
   is rejected, and this must hold under concurrent load, not just in
   a single-threaded happy path
6. A reconciliation endpoint/test that sums every ledger entry system-
   wide and asserts it nets to zero
7. Full test suite: unit tests, a concurrency test proving no
   overdraft under N simultaneous debit attempts, a duplicate-
   submission test proving idempotency, and an immutability test
   proving entries can't be altered through the API
8. Load test script producing real throughput/latency numbers against
   the transaction endpoint
9. Dockerized services, docker-compose for local orchestration
   (MongoDB as a single-node replica set, required for multi-document
   transactions), Kubernetes manifests for orchestration demonstration
   (tested locally against kind/minikube unless a live cluster is set
   up separately)

## Explicitly out of scope
- No frontend/UI. This project is proven through its API, its test
  suite, and its load test report, a UI adds nothing to the
  correctness story this project exists to tell
- No real payment rails, bank integration, or multi-currency
  conversion logic
- No message queue. Unlike a prior project in this portfolio (an
  offline mesh-relay payment system using RabbitMQ), this system's
  correctness guarantee comes from the database transaction layer
  itself, not from queue-based delivery semantics. Keeping this
  distinct is intentional, it's a different engineering problem
  being proven, not a restatement of the same one
