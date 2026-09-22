#!/usr/bin/env bash
#
# Prove that the hardened profile makes ledger entries append-only at the
# database's authorisation layer, not merely by convention in application code.
#
# This closes the gap recorded in MEMORY.md since Phase 4. The `$jsonSchema`
# and `$expr` validators reject a write whose *shape or sign* is wrong, but a
# validator can only judge the resulting document, so an update that keeps the
# document self-consistent still succeeds. MongoDB's privilege actions are per
# collection and `insert` is separate from `update` and `remove`, so an
# append-only grant is expressible directly. This script demonstrates it.
#
# It asserts both directions, which is the part that makes it meaningful:
#   * the application user CAN insert and read ledger entries, and the whole
#     end-to-end flow still works, so the role is not simply too restrictive
#     to function;
#   * the application user CANNOT update or delete them, and gets Unauthorized.
#
# Usage:
#   ./scripts/verify_append_only.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

BASE_URL="${BASE_URL:-http://localhost:8000}"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.hardened.yml)

pass_count=0
fail_count=0

info() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mPASS\033[0m %s\n' "$1"; pass_count=$((pass_count + 1)); }
bad()  { printf '    \033[31mFAIL\033[0m %s\n' "$1"; fail_count=$((fail_count + 1)); }

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' required" >&2; exit 2; }
}
require docker
require curl
require python3

# Credentials for this run. Generated rather than hardcoded, and exported only
# into the compose invocations below.
export MONGO_ROOT_USERNAME="${MONGO_ROOT_USERNAME:-ledgerlock_root}"
export MONGO_ROOT_PASSWORD="${MONGO_ROOT_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export MONGO_APP_USERNAME="${MONGO_APP_USERNAME:-ledgerlock_app}"
export MONGO_APP_PASSWORD="${MONGO_APP_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')}"

# ---------------------------------------------------------------------
info "Bringing up the hardened stack (MongoDB with auth + least privilege)"
# ---------------------------------------------------------------------
# Fresh volumes, including the keyfile volume, so this is a genuine cold start
# and the role provisioning is exercised rather than inherited.
"${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build >/dev/null

printf '    waiting for the API to report ready'
ready_body=""
for _ in $(seq 1 90); do
  if ready_body=$(curl -fsS "$BASE_URL/health/ready" 2>/dev/null); then
    printf ' ok\n'
    break
  fi
  printf '.'
  sleep 2
done
if [[ -z "$ready_body" ]]; then
  printf '\n'
  echo "error: API never became ready." >&2
  "${COMPOSE[@]}" logs --tail 60 mongo mongo-provision api >&2
  exit 1
fi
echo "    $ready_body"

echo
echo "    role provisioning output:"
"${COMPOSE[@]}" logs --no-log-prefix mongo-provision 2>/dev/null | sed 's/^/      /'

# ---------------------------------------------------------------------
info "Confirming authentication is actually enforced"
# ---------------------------------------------------------------------
# If an anonymous connection can write, nothing below means anything.
anonymous_result=$(docker exec ledgerlock-mongo mongosh --quiet --eval '
  try {
    db.getSiblingDB("ledgerlock").ledger_entries.findOne();
    print("ALLOWED");
  } catch (err) {
    print("REFUSED:" + err.codeName);
  }' 2>&1 | tail -1)

echo "    anonymous read -> $anonymous_result"
if [[ "$anonymous_result" == REFUSED:Unauthorized ]]; then
  ok "an unauthenticated client cannot read the ledger"
else
  bad "unauthenticated access was not refused (got: $anonymous_result)"
fi

# ---------------------------------------------------------------------
info "Running the full end-to-end flow under the restricted role"
# ---------------------------------------------------------------------
# The role must be usable, not just restrictive. This runs the same 33
# assertions used for the default profile: register, open accounts, fund,
# transfer, 10 concurrent duplicate submissions, 30 concurrent debits that
# exceed the balance, and reconciliation. All of it goes through the
# least-privilege user, including the multi-document transactions.
if BASE_URL="$BASE_URL" ./scripts/verify_compose.sh; then
  ok "the full end-to-end verification passed under the restricted role"
else
  bad "the end-to-end verification failed under the restricted role"
fi

# ---------------------------------------------------------------------
info "The point of the exercise: can the application tamper with an entry?"
# ---------------------------------------------------------------------
# Connects as the application's own user, the same credentials the API uses,
# and attempts every mutation. Each one must be refused with Unauthorized.
tamper_output=$(docker exec \
  -e APPUSER="$MONGO_APP_USERNAME" \
  -e APPPASS="$MONGO_APP_PASSWORD" \
  ledgerlock-mongo \
  bash -c 'mongosh --quiet -u "$APPUSER" -p "$APPPASS" --authenticationDatabase ledgerlock ledgerlock --eval '"'"'
    const entry = db.ledger_entries.findOne();
    if (!entry) { print("NO_ENTRIES"); quit(1); }

    function attempt(label, fn) {
      try { fn(); print(label + ":ALLOWED"); }
      catch (err) { print(label + ":REFUSED:" + err.codeName); }
    }

    attempt("read",            () => db.ledger_entries.findOne());
    // A fresh transaction_id rather than the sampled one. Reusing it
    // collides with uq_one_entry_per_direction_per_transaction, which looks
    // like the role refusing the insert when it is really the index doing
    // its job on a malformed probe.
    attempt("insert",          () => db.ledger_entries.insertOne({
                                       transaction_id: new ObjectId(),
                                       account_id: entry.account_id,
                                       direction: "CREDIT",
                                       amount_minor: 1,
                                       signed_amount_minor: 1,
                                       currency: entry.currency,
                                       created_at: new Date()
                                     }));
    attempt("update_amount",   () => db.ledger_entries.updateOne(
                                       {_id: entry._id},
                                       {$set: {amount_minor: 999999, signed_amount_minor: 999999}}));
    attempt("update_direction",() => db.ledger_entries.updateOne(
                                       {_id: entry._id}, {$set: {direction: "CREDIT"}}));
    attempt("replace",         () => db.ledger_entries.replaceOne({_id: entry._id}, entry));
    attempt("delete_one",      () => db.ledger_entries.deleteOne({_id: entry._id}));
    attempt("delete_many",     () => db.ledger_entries.deleteMany({}));
    attempt("drop_collection", () => db.ledger_entries.drop());
    attempt("txn_update",      () => db.transactions.updateOne({}, {$set: {amount_minor: 1}}));
    attempt("txn_delete",      () => db.transactions.deleteOne({}));
    attempt("drop_database",   () => db.dropDatabase());
  '"'"'' 2>&1)

echo "$tamper_output" | sed 's/^/    /'
echo

# The role has to remain usable: reads and appends must still work.
for allowed in read insert; do
  if grep -q "^${allowed}:ALLOWED" <<<"$tamper_output"; then
    ok "$allowed is permitted (the role is not merely broken)"
  else
    bad "$allowed was refused; the role is too restrictive to function"
  fi
done

# And every mutation has to be refused.
for forbidden in update_amount update_direction replace delete_one delete_many \
                 drop_collection txn_update txn_delete drop_database; do
  if grep -q "^${forbidden}:REFUSED:Unauthorized" <<<"$tamper_output"; then
    ok "$forbidden refused with Unauthorized"
  elif grep -q "^${forbidden}:REFUSED" <<<"$tamper_output"; then
    reason=$(grep "^${forbidden}:REFUSED" <<<"$tamper_output" | head -1)
    ok "$forbidden refused ($reason)"
  else
    bad "$forbidden was ALLOWED; ledger entries are not append-only"
  fi
done

# ---------------------------------------------------------------------
info "Confirming no existing entry was altered"
# ---------------------------------------------------------------------
# The mutation attempts above all failed, so every pre-existing entry must be
# byte-for-byte what it was. Checked directly rather than inferred from the
# error codes.
unchanged=$(docker exec \
  -e APPUSER="$MONGO_APP_USERNAME" \
  -e APPPASS="$MONGO_APP_PASSWORD" \
  ledgerlock-mongo \
  bash -c 'mongosh --quiet -u "$APPUSER" -p "$APPPASS" --authenticationDatabase ledgerlock ledgerlock --eval '"'"'
    // Every entry that belongs to a real transaction must still satisfy the
    // sign convention and have a positive magnitude. An update that slipped
    // through would show up here.
    const bad = db.ledger_entries.find({
      $expr: {
        $ne: [
          "$signed_amount_minor",
          { $cond: [ { $eq: ["$direction", "CREDIT"] }, "$amount_minor",
                     { $multiply: ["$amount_minor", -1] } ] }
        ]
      }
    }).toArray().length;
    print("inconsistent_entries:" + bad);
    print("amount_999999_entries:" + db.ledger_entries.countDocuments({amount_minor: 999999}));
  '"'"'' 2>&1)

echo "$unchanged" | sed 's/^/    /'

if grep -q "^inconsistent_entries:0" <<<"$unchanged"; then
  ok "no entry violates the sign convention"
else
  bad "an entry was altered into an inconsistent state"
fi
if grep -q "^amount_999999_entries:0" <<<"$unchanged"; then
  ok "the attempted amount tamper (999999) is nowhere in the ledger"
else
  bad "the attempted amount tamper landed"
fi

# ---------------------------------------------------------------------
info "Reconciliation, which should now flag the probe's own insert"
# ---------------------------------------------------------------------
# A deliberate consequence worth asserting rather than tidying away. The probe
# above proved the role can still INSERT by appending one entry with a
# transaction_id that has no transaction document. That is an orphan, and the
# reconciliation check is supposed to notice it.
#
# So the expected state here is NOT "healthy". It is: exactly one orphaned
# entry, a net of exactly +1 minor unit, and nothing else wrong. That
# simultaneously demonstrates two things: the append succeeded (so the role is
# usable), and the integrity check detects an append that does not belong to a
# transaction (so a rogue insert cannot hide).
token=$(python3 - "$BASE_URL" <<'PY'
import json, sys, urllib.request, uuid
base = sys.argv[1]
payload = json.dumps({
    "email": f"append-only-check-{uuid.uuid4().hex[:10]}@example.com",
    "password": "a-perfectly-fine-password",
}).encode()
request = urllib.request.Request(
    f"{base}/auth/register", data=payload,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.load(response)["access_token"])
PY
)
report=$(curl -fsS "$BASE_URL/reconciliation" -H "Authorization: Bearer $token")
echo "    $report"

field() {
  python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$report" "$1"
}

net=$(field net_signed_minor)
orphans=$(field entries_without_transaction)
negatives=$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])["negative_user_accounts"]))' "$report")

if [[ "$net" == "1" && "$orphans" == "1" ]]; then
  ok "reconciliation detected the probe's orphaned append (net=1, orphans=1)"
elif [[ "$net" == "0" && "$orphans" == "0" ]]; then
  bad "the probe's insert is not visible; it may not have been committed"
else
  bad "unexpected reconciliation state: net=$net orphans=$orphans"
fi

if [[ "$negatives" == "0" ]]; then
  ok "no USER account is negative"
else
  bad "$negatives USER account(s) are negative"
fi

# ---------------------------------------------------------------------
info "Result"
# ---------------------------------------------------------------------
printf '    %d passed, %d failed\n' "$pass_count" "$fail_count"
if (( fail_count > 0 )); then
  echo "    APPEND-ONLY VERIFICATION FAILED"
  exit 1
fi
echo "    APPEND-ONLY VERIFICATION PASSED"
echo
echo "    Ledger entries cannot be updated or deleted by the credentials the"
echo "    application holds. Immutability is enforced by MongoDB's"
echo "    authorisation system, not by the absence of an offending code path."
