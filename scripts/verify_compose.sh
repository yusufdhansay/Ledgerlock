#!/usr/bin/env bash
#
# End-to-end verification against a running docker-compose stack.
#
# Exercises the full flow the way a client would, over HTTP, against the
# containerised API and its replica set: register, open two accounts, bring
# value in, transfer, read both computed balances, prove the overdraft
# guarantee holds under concurrent load, prove a replayed idempotency key is
# not applied twice, and finish with the system-wide reconciliation check.
#
# Every assertion prints what it expected and what it got. The script exits
# non-zero on the first failure, so it is usable as a gate.
#
# Usage:
#   docker compose up -d --build
#   ./scripts/verify_compose.sh

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
EMAIL="verify-$(date +%s)-$RANDOM@example.com"
PASSWORD="a-perfectly-fine-password"

pass_count=0
fail_count=0

info()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()    { printf '    \033[32mPASS\033[0m %s\n' "$1"; pass_count=$((pass_count + 1)); }
bad()   { printf '    \033[31mFAIL\033[0m %s\n' "$1"; fail_count=$((fail_count + 1)); }

# assert_equal <description> <expected> <actual>
assert_equal() {
  local description="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    ok "$description (= $actual)"
  else
    bad "$description: expected '$expected', got '$actual'"
    return 1
  fi
}

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: '$1' is required but not installed." >&2
    exit 2
  }
}

require curl
require python3

# json <json-string> <python-expression using `d`>
json() {
  python3 -c '
import json, sys
d = json.loads(sys.argv[1])
print(eval(sys.argv[2]))
' "$1" "$2"
}

idem_key() { printf 'verify-%s-%s-%s' "$1" "$(date +%s)" "$RANDOM"; }

# ---------------------------------------------------------------------
info "Waiting for the API to become ready"
# ---------------------------------------------------------------------
# /health/ready confirms not just that the process is up, but that it is
# connected to a transaction-capable replica set primary. That is the
# precondition for every guarantee below.
ready=""
for attempt in $(seq 1 60); do
  if body=$(curl -fsS "$BASE_URL/health/ready" 2>/dev/null); then
    ready="$body"
    break
  fi
  sleep 2
done

if [[ -z "$ready" ]]; then
  echo "error: API never became ready at $BASE_URL. Try: docker compose logs api" >&2
  exit 1
fi
echo "    readiness: $ready"
assert_equal "readiness status" "ready" "$(json "$ready" 'd["status"]')"
assert_equal "connected to replica set" "rs0" "$(json "$ready" 'd["replica_set"]')"

# ---------------------------------------------------------------------
info "Registering a user"
# ---------------------------------------------------------------------
register=$(curl -fsS -X POST "$BASE_URL/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
TOKEN=$(json "$register" 'd["access_token"]')
AUTH="Authorization: Bearer $TOKEN"
[[ -n "$TOKEN" ]] && ok "received an access token" || bad "no access token"

# ---------------------------------------------------------------------
info "Opening two accounts"
# ---------------------------------------------------------------------
source_account=$(curl -fsS -X POST "$BASE_URL/accounts" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"currency":"USD","label":"Verify source"}')
SOURCE_ID=$(json "$source_account" 'd["id"]')

destination_account=$(curl -fsS -X POST "$BASE_URL/accounts" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"currency":"USD","label":"Verify destination"}')
DESTINATION_ID=$(json "$destination_account" 'd["id"]')

echo "    source:      $SOURCE_ID"
echo "    destination: $DESTINATION_ID"
assert_equal "account starts ACTIVE" "ACTIVE" "$(json "$source_account" 'd["status"]')"
assert_equal "account is a USER account" "USER" \
  "$(json "$source_account" 'd["account_type"]')"

# A new account has no ledger entries, so its balance must compute to zero.
opening=$(curl -fsS "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH")
assert_equal "new account balance is zero" "0" "$(json "$opening" 'd["balance_minor"]')"
assert_equal "new account entry count is zero" "0" \
  "$(json "$opening" 'd["entry_count"]')"

# ---------------------------------------------------------------------
info "Funding the source account with 100000 minor units"
# ---------------------------------------------------------------------
funding=$(curl -fsS -X POST "$BASE_URL/accounts/$SOURCE_ID/funding" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(idem_key fund)" \
  -d '{"amount_minor":100000,"currency":"USD"}')
assert_equal "funding status" "COMPLETED" "$(json "$funding" 'd["status"]')"
assert_equal "funding kind" "FUNDING" "$(json "$funding" 'd["kind"]')"

funded=$(curl -fsS "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH")
assert_equal "balance after funding" "100000" \
  "$(json "$funded" 'd["balance_minor"]')"
assert_equal "entry count after funding" "1" "$(json "$funded" 'd["entry_count"]')"

# ---------------------------------------------------------------------
info "Transferring 25000 between the accounts"
# ---------------------------------------------------------------------
transfer=$(curl -fsS -X POST "$BASE_URL/transactions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(idem_key transfer)" \
  -d "{\"source_account_id\":\"$SOURCE_ID\",\"destination_account_id\":\"$DESTINATION_ID\",\"amount_minor\":25000,\"currency\":\"USD\"}")
TRANSFER_ID=$(json "$transfer" 'd["id"]')
assert_equal "transfer status" "COMPLETED" "$(json "$transfer" 'd["status"]')"

source_after=$(curl -fsS "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH")
destination_after=$(curl -fsS "$BASE_URL/accounts/$DESTINATION_ID/balance" -H "$AUTH")
assert_equal "source balance after transfer" "75000" \
  "$(json "$source_after" 'd["balance_minor"]')"
assert_equal "destination balance after transfer" "25000" \
  "$(json "$destination_after" 'd["balance_minor"]')"
assert_equal "balance response carries its currency" "USD" \
  "$(json "$destination_after" 'd["currency"]')"

readback=$(curl -fsS "$BASE_URL/transactions/$TRANSFER_ID" -H "$AUTH")
assert_equal "transaction can be read back" "25000" \
  "$(json "$readback" 'd["amount_minor"]')"

# ---------------------------------------------------------------------
info "Replaying one idempotency key 10 times concurrently"
# ---------------------------------------------------------------------
# A duplicate must be rejected, not reprocessed. Exactly one 201.
REPLAY_KEY=$(idem_key replay)
replay_status_file=$(mktemp)
for _ in $(seq 1 10); do
  (
    curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/transactions" \
      -H "$AUTH" -H 'Content-Type: application/json' \
      -H "Idempotency-Key: $REPLAY_KEY" \
      -d "{\"source_account_id\":\"$SOURCE_ID\",\"destination_account_id\":\"$DESTINATION_ID\",\"amount_minor\":1000,\"currency\":\"USD\"}" \
      >> "$replay_status_file"
  ) &
done
wait

replay_created=$(grep -c '^201$' "$replay_status_file" || true)
replay_conflict=$(grep -c '^409$' "$replay_status_file" || true)
echo "    statuses: 201 x $replay_created, 409 x $replay_conflict"
assert_equal "exactly one replayed submission was accepted" "1" "$replay_created"
assert_equal "the other nine were rejected as duplicates" "9" "$replay_conflict"

after_replay=$(curl -fsS "$BASE_URL/accounts/$DESTINATION_ID/balance" -H "$AUTH")
assert_equal "the replayed transfer was applied exactly once" "26000" \
  "$(json "$after_replay" 'd["balance_minor"]')"

# ---------------------------------------------------------------------
info "Firing 30 concurrent debits that together exceed the balance"
# ---------------------------------------------------------------------
# Source holds 74000. 30 debits of 5000 would need 150000. Each looks
# affordable on its own. Exactly 14 can succeed (74000 / 5000 = 14 whole
# debits, leaving 4000).
before_race=$(json "$(curl -fsS "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH")" 'd["balance_minor"]')
echo "    balance before the race: $before_race"

race_status_file=$(mktemp)
for i in $(seq 1 30); do
  (
    curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/transactions" \
      -H "$AUTH" -H 'Content-Type: application/json' \
      -H "Idempotency-Key: $(idem_key "race-$i")" \
      -d "{\"source_account_id\":\"$SOURCE_ID\",\"destination_account_id\":\"$DESTINATION_ID\",\"amount_minor\":5000,\"currency\":\"USD\"}" \
      >> "$race_status_file"
  ) &
done
wait

race_created=$(grep -c '^201$' "$race_status_file" || true)
race_rejected=$(grep -c '^422$' "$race_status_file" || true)
expected_created=$((before_race / 5000))
expected_remaining=$((before_race - (expected_created * 5000)))

final_source=$(json "$(curl -fsS "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH")" 'd["balance_minor"]')

echo "    statuses: 201 x $race_created, 422 x $race_rejected"
echo "    final source balance: $final_source"

if (( final_source >= 0 )); then
  ok "the account never went negative (balance $final_source)"
else
  bad "OVERDRAFT: balance went to $final_source"
fi
assert_equal "exactly the affordable number of debits succeeded" \
  "$expected_created" "$race_created"
assert_equal "remaining balance is exact" "$expected_remaining" "$final_source"

# ---------------------------------------------------------------------
info "Reconciling the whole ledger"
# ---------------------------------------------------------------------
# The number that matters. Every ledger entry in the system, summed. It must
# be exactly zero, and it must be zero after the concurrent load above, not
# only on a quiet system.
reconciliation=$(curl -fsS "$BASE_URL/reconciliation" -H "$AUTH")
echo "    report: $reconciliation"

assert_equal "net_signed_minor" "0" "$(json "$reconciliation" 'd["net_signed_minor"]')"
assert_equal "balanced" "True" "$(json "$reconciliation" 'd["balanced"]')"
assert_equal "healthy" "True" "$(json "$reconciliation" 'd["healthy"]')"
assert_equal "entries without a transaction" "0" \
  "$(json "$reconciliation" 'd["entries_without_transaction"]')"
assert_equal "unbalanced transaction groups" "0" \
  "$(json "$reconciliation" 'd["unbalanced_transaction_groups"]')"
assert_equal "negative USER accounts" "0" \
  "$(json "$reconciliation" 'len(d["negative_user_accounts"])')"
assert_equal "two ledger entries per transaction, exactly" "True" \
  "$(json "$reconciliation" 'd["total_entries"] == d["total_transactions"] * 2')"

# ---------------------------------------------------------------------
info "Checking that ledger data cannot be mutated over HTTP"
# ---------------------------------------------------------------------
for method in PUT PATCH DELETE; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X "$method" \
    "$BASE_URL/transactions/$TRANSFER_ID" -H "$AUTH")
  if [[ "$code" == "404" || "$code" == "405" ]]; then
    ok "$method /transactions/{id} refused ($code)"
  else
    bad "$method /transactions/{id} returned $code; ledger data must be append-only"
  fi
done

code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
  "$BASE_URL/accounts/$SOURCE_ID/balance" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"balance_minor":999999999}')
if [[ "$code" == "404" || "$code" == "405" ]]; then
  ok "PUT /accounts/{id}/balance refused ($code) — balance is not stored"
else
  bad "PUT /accounts/{id}/balance returned $code"
fi

# ---------------------------------------------------------------------
info "Result"
# ---------------------------------------------------------------------
rm -f "$replay_status_file" "$race_status_file"

printf '    %d passed, %d failed\n' "$pass_count" "$fail_count"
if (( fail_count > 0 )); then
  echo "    END-TO-END VERIFICATION FAILED"
  exit 1
fi
echo "    END-TO-END VERIFICATION PASSED"
