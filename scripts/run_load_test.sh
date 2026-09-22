#!/usr/bin/env bash
#
# Run the Locust load test against a clean docker-compose stack, capture the
# real numbers, and re-check reconciliation immediately afterwards.
#
# The reconciliation result is the point of the exercise. Throughput on its
# own says the service stayed up; reconciliation after that throughput says
# the ledger is still correct, which is the only claim worth making.
#
# Everything is written to tests/load/results/<timestamp>/ and nothing is
# edited by hand afterwards.
#
# Usage:
#   ./scripts/run_load_test.sh [users] [spawn_rate] [duration]
#
# Example:
#   ./scripts/run_load_test.sh 50 10 60s

set -euo pipefail

USERS="${1:-50}"
SPAWN_RATE="${2:-10}"
DURATION="${3:-60s}"

BASE_URL="${BASE_URL:-http://localhost:8000}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
LOCUST="$PROJECT_ROOT/.venv/bin/locust"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RESULTS_DIR="$PROJECT_ROOT/tests/load/results/$STAMP"
mkdir -p "$RESULTS_DIR"

info() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

[[ -x "$LOCUST" ]] || {
  echo "error: locust not found at $LOCUST. Install with:" >&2
  echo "  .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 2
}

cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------
info "Bringing up a clean stack"
# ---------------------------------------------------------------------
# Fresh volumes, so the entry and transaction counts in the report describe
# this run and nothing else.
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d --build >/dev/null

printf '    waiting for the API to report ready'
ready_body=""
for _ in $(seq 1 60); do
  if ready_body=$(curl -fsS "$BASE_URL/health/ready" 2>/dev/null); then
    printf ' ok\n'
    break
  fi
  printf '.'
  sleep 2
done
if [[ -z "$ready_body" ]]; then
  printf '\n'
  echo "error: API never became ready. Try: docker compose logs api" >&2
  exit 1
fi
echo "    $ready_body"

# ---------------------------------------------------------------------
info "Recording the environment"
# ---------------------------------------------------------------------
{
  echo "Ledgerlock load test run"
  echo "Started:      $(date -u +'%Y-%m-%dT%H:%M:%SZ') UTC"
  echo "Locust:       users=$USERS spawn-rate=$SPAWN_RATE run-time=$DURATION"
  echo "Target:       $BASE_URL"
  echo "Readiness:    $ready_body"
  echo
  echo "Host:         $(uname -srm)"
  echo "CPU cores:    $(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo unknown)"
  echo "Memory:       $(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 / 1024 )) GiB"
  echo "Docker:       $(docker --version)"
  echo "Locust ver:   $("$LOCUST" --version 2>&1 | head -1)"
  echo "Python:       $("$PYTHON" --version 2>&1)"
  echo "MongoDB:      $(docker exec ledgerlock-mongo mongosh --quiet --eval 'db.version()' 2>/dev/null)"
  echo "Replica set:  $(docker exec ledgerlock-mongo mongosh --quiet --eval 'rs.status().set' 2>/dev/null) (single node)"
  echo "API image:    ledgerlock-api:local"
  echo "API workers:  1 uvicorn worker, 1 container"
  echo
  echo "IMPORTANT CAVEATS for reading these numbers:"
  echo "  * Load generator and service run on the SAME machine, so they"
  echo "    compete for CPU. These figures are a self-consistent baseline for"
  echo "    comparing code changes, not a capacity statement about the"
  echo "    service on dedicated hardware."
  echo "  * One uvicorn worker, one container, one MongoDB node. Nothing here"
  echo "    is tuned for throughput."
  echo "  * Locust runs single-process so the shared hot account is coherent"
  echo "    across all virtual users. A single generator process may itself"
  echo "    be a limiting factor."
  echo "  * bcrypt runs at cost 12. Registration happens in on_start and is"
  echo "    reported under separate 'setup:' request names so it does not"
  echo "    distort transaction latency."
} > "$RESULTS_DIR/environment.txt"

cat "$RESULTS_DIR/environment.txt"

# ---------------------------------------------------------------------
info "Reconciliation before the load (baseline)"
# ---------------------------------------------------------------------
# Registers a throwaway user purely to read the reconciliation endpoint.
baseline_token=$("$PYTHON" - "$BASE_URL" <<'PY'
import json, sys, urllib.request, uuid
base = sys.argv[1]
payload = json.dumps({
    "email": f"recon-before-{uuid.uuid4().hex[:10]}@loadtest.example.com",
    "password": "load-test-password-not-a-secret",
}).encode()
request = urllib.request.Request(
    f"{base}/auth/register", data=payload,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.load(response)["access_token"])
PY
)

curl -fsS "$BASE_URL/reconciliation" -H "Authorization: Bearer $baseline_token" \
  > "$RESULTS_DIR/reconciliation-before.json"
"$PYTHON" -m json.tool "$RESULTS_DIR/reconciliation-before.json"

# ---------------------------------------------------------------------
info "Running the load test"
# ---------------------------------------------------------------------
set +e
LOAD_SUMMARY_PATH="$RESULTS_DIR/outcome-summary.json" \
"$LOCUST" \
  --locustfile tests/load/locustfile.py \
  --host "$BASE_URL" \
  --headless \
  --users "$USERS" \
  --spawn-rate "$SPAWN_RATE" \
  --run-time "$DURATION" \
  --csv "$RESULTS_DIR/locust" \
  --csv-full-history \
  --html "$RESULTS_DIR/locust-report.html" \
  2>&1 | tee "$RESULTS_DIR/locust-stdout.txt"
locust_exit=${PIPESTATUS[0]}
set -e

echo "locust exit code: $locust_exit" | tee -a "$RESULTS_DIR/locust-stdout.txt"

# ---------------------------------------------------------------------
info "Reconciliation AFTER the load — the number that matters"
# ---------------------------------------------------------------------
# This is the claim. Whatever interleaving, retrying and rejecting happened
# above, every ledger entry in the system must still sum to exactly zero,
# every transaction must still have exactly one debit and one credit, and no
# user account may be negative.
after_token=$("$PYTHON" - "$BASE_URL" <<'PY'
import json, sys, urllib.request, uuid
base = sys.argv[1]
payload = json.dumps({
    "email": f"recon-after-{uuid.uuid4().hex[:10]}@loadtest.example.com",
    "password": "load-test-password-not-a-secret",
}).encode()
request = urllib.request.Request(
    f"{base}/auth/register", data=payload,
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.load(response)["access_token"])
PY
)

curl -fsS "$BASE_URL/reconciliation" -H "Authorization: Bearer $after_token" \
  > "$RESULTS_DIR/reconciliation-after.json"
"$PYTHON" -m json.tool "$RESULTS_DIR/reconciliation-after.json"

# ---------------------------------------------------------------------
info "Verdict"
# ---------------------------------------------------------------------
"$PYTHON" - "$RESULTS_DIR" <<'PY'
import csv, json, pathlib, sys

results = pathlib.Path(sys.argv[1])
report = json.loads((results / "reconciliation-after.json").read_text())

lines = []
failures = []

lines.append("Reconciliation after load:")
lines.append(f"  net_signed_minor              = {report['net_signed_minor']}")
lines.append(f"  per_currency_net_minor        = {report['per_currency_net_minor']}")
lines.append(f"  total_entries                 = {report['total_entries']}")
lines.append(f"  total_transactions            = {report['total_transactions']}")
lines.append(f"  entries_without_transaction   = {report['entries_without_transaction']}")
lines.append(f"  unbalanced_transaction_groups = {report['unbalanced_transaction_groups']}")
lines.append(f"  negative_user_accounts        = {len(report['negative_user_accounts'])}")
lines.append(f"  balanced                      = {report['balanced']}")
lines.append(f"  healthy                       = {report['healthy']}")

if report["net_signed_minor"] != 0:
    failures.append(f"ledger does not net to zero: {report['net_signed_minor']}")
if not report["healthy"]:
    failures.append("reconciliation reports the ledger as unhealthy")
if report["total_entries"] != report["total_transactions"] * 2:
    failures.append(
        f"entry count {report['total_entries']} is not twice the transaction "
        f"count {report['total_transactions']}"
    )
if report["negative_user_accounts"]:
    failures.append(
        f"{len(report['negative_user_accounts'])} USER account(s) are negative"
    )

# Throughput and latency, straight from Locust's own aggregated CSV row.
stats_path = results / "locust_stats.csv"
if stats_path.exists():
    with stats_path.open() as handle:
        rows = list(csv.DictReader(handle))
    aggregated = next((r for r in rows if r.get("Name") == "Aggregated"), None)
    if aggregated:
        lines.append("")
        lines.append("Locust aggregated (all request types):")
        lines.append(f"  requests                = {aggregated['Request Count']}")
        lines.append(f"  failures                = {aggregated['Failure Count']}")
        lines.append(f"  requests/sec            = {float(aggregated['Requests/s']):.1f}")
        lines.append(f"  median (p50) ms         = {aggregated['Median Response Time']}")
        lines.append(f"  p95 ms                  = {aggregated['95%']}")
        lines.append(f"  p99 ms                  = {aggregated['99%']}")
        lines.append(f"  mean ms                 = {float(aggregated['Average Response Time']):.1f}")
        lines.append(f"  max ms                  = {aggregated['Max Response Time']}")
        if int(aggregated["Failure Count"]) > 0:
            failures.append(
                f"locust recorded {aggregated['Failure Count']} failed requests"
            )

summary = json.loads((results / "outcome-summary.json").read_text())
if summary["unexpected_outcome_count"] > 0:
    failures.append(
        f"{summary['unexpected_outcome_count']} unexpected outcome(s); see "
        "outcome-summary.json"
    )

text = "\n".join(lines)
print(text)
(results / "verdict.txt").write_text(
    text
    + "\n\n"
    + ("LOAD TEST FAILED\n" + "\n".join(f"  - {f}" for f in failures)
       if failures else "LOAD TEST PASSED: correctness survived the load.\n")
)

if failures:
    print("\nLOAD TEST FAILED")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)

print("\nLOAD TEST PASSED: correctness survived the load.")
PY

# ---------------------------------------------------------------------
info "Transaction retry cost, read from the API's own logs"
# ---------------------------------------------------------------------
# ledger_service logs `attempts` on every committed transaction: how many
# times the MongoDB transaction callback had to run. Above 1 means a write
# conflict was detected and the transaction was replayed against fresh
# state. This is the real, measured cost of the overdraft guarantee under
# contention, as opposed to an estimate of it.
docker compose logs api --no-log-prefix > "$RESULTS_DIR/api-logs.txt" 2>&1 || true

"$PYTHON" - "$RESULTS_DIR" <<'PY' | tee "$RESULTS_DIR/retry-distribution.txt"
import json, pathlib, statistics, sys

results = pathlib.Path(sys.argv[1])
attempts = []
for line in (results / "api-logs.txt").read_text(errors="replace").splitlines():
    line = line.strip()
    if not line.startswith("{") or "transaction_committed" not in line:
        continue
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        continue
    if record.get("event") == "transaction_committed" and "attempts" in record:
        attempts.append(int(record["attempts"]))

if not attempts:
    print("No transaction_committed log records found; cannot report retries.")
    raise SystemExit(0)

first_try = sum(1 for a in attempts if a == 1)
retried = len(attempts) - first_try
buckets = {"1": first_try, "2-5": 0, "6-20": 0, "21-100": 0, ">100": 0}
for a in attempts:
    if a == 1:
        continue
    if a <= 5:
        buckets["2-5"] += 1
    elif a <= 20:
        buckets["6-20"] += 1
    elif a <= 100:
        buckets["21-100"] += 1
    else:
        buckets[">100"] += 1

print("Transaction retry cost (from ledger_service `attempts`):")
print(f"  committed transactions       = {len(attempts)}")
print(f"  total callback runs          = {sum(attempts)}")
print(f"  amplification                = {sum(attempts) / len(attempts):.2f}x")
print(f"  committed on the first try   = {first_try} ({100 * first_try / len(attempts):.1f}%)")
print(f"  needed at least one retry    = {retried} ({100 * retried / len(attempts):.1f}%)")
print(f"  median attempts              = {statistics.median(attempts):.0f}")
print(f"  max attempts                 = {max(attempts)}")
print("  distribution:")
for label, count in buckets.items():
    print(f"    {label:>8} attempts : {count}")
PY

# The raw API log is the evidence behind the retry numbers above, so it is
# kept rather than discarded, but it is several megabytes of JSON per run and
# compresses roughly tenfold.
gzip -f "$RESULTS_DIR/api-logs.txt"

info "Artifacts"
ls -1 "$RESULTS_DIR"
echo
echo "Results directory: ${RESULTS_DIR#"$PROJECT_ROOT"/}"
