#!/usr/bin/env bash
#
# Fail if anything that looks like a real secret is committed.
#
# Deliberately narrow. A scanner that flags every occurrence of the word
# "password" produces so much noise that people stop reading it, which leaves
# you worse off than having no scanner. This looks for the specific shapes a
# real leak takes in this repository, and allow-lists the places where the
# *name* of a secret legitimately appears: .env.example, the Kubernetes
# template, documentation, and the tests that assert secrets are rejected.
#
# Run by CI, and runnable by hand:
#   ./scripts/audit_secrets.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

findings=0

report() {
  echo "  FINDING: $1"
  findings=$((findings + 1))
}

# Only ever inspect tracked files. An untracked local .env is fine and
# expected; a tracked one is the thing we care about.
tracked_files() {
  git ls-files
}

echo "==> Checking that no environment or key file is tracked"
for pattern in '.env' '.env.local' '.env.production' '*.pem' '*.key' '*.p12' '*.pfx' 'id_rsa' '*.keyfile'; do
  while IFS= read -r file; do
    [[ -z "$file" ]] && continue
    # .env.example is a template of placeholders and is meant to be committed.
    [[ "$file" == ".env.example" ]] && continue
    report "tracked secret-shaped file: $file"
  done < <(git ls-files "$pattern" 2>/dev/null || true)
done
echo "  done"

echo "==> Checking for a real-looking JWT signing key"
# The placeholder in .env.example and the throwaway keys in tests and CI are
# fine. What must never appear is a long high-entropy value assigned to
# JWT_SECRET_KEY outside those places.
while IFS= read -r hit; do
  [[ -z "$hit" ]] && continue
  file="${hit%%:*}"
  case "$file" in
    .env.example|MEMORY.md|README.md|RULES.md|k8s/secret.example.yaml) continue ;;
    tests/*|scripts/audit_secrets.sh|.github/workflows/ci.yml) continue ;;
    app/core/config.py) continue ;;   # defines and rejects the placeholder
    docker-compose.yml) continue ;;   # interpolates from the environment
  esac
  report "possible hardcoded signing key in $file: ${hit#*:}"
done < <(grep -rnE 'JWT_SECRET_KEY[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_\-]{24,}' \
           $(tracked_files) 2>/dev/null || true)
echo "  done"

echo "==> Checking for MongoDB connection strings with embedded credentials"
while IFS= read -r hit; do
  [[ -z "$hit" ]] && continue
  file="${hit%%:*}"
  case "$file" in
    scripts/audit_secrets.sh) continue ;;
  esac
  report "MongoDB URI with inline credentials in $file: ${hit#*:}"
done < <(grep -rnE 'mongodb(\+srv)?://[A-Za-z0-9_.-]+:[^@/[:space:]]+@' \
           $(tracked_files) 2>/dev/null || true)
echo "  done"

echo "==> Checking for well-known cloud credential formats"
# AWS access key ids, Slack tokens, GitHub tokens, private key blocks.
patterns=(
  'AKIA[0-9A-Z]{16}'
  'xox[baprs]-[0-9A-Za-z-]{10,}'
  'gh[pousr]_[0-9A-Za-z]{30,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
for pattern in "${patterns[@]}"; do
  while IFS= read -r hit; do
    [[ -z "$hit" ]] && continue
    file="${hit%%:*}"
    [[ "$file" == "scripts/audit_secrets.sh" ]] && continue
    report "credential-shaped string in $file: ${hit#*:}"
  done < <(grep -rnE "$pattern" $(tracked_files) 2>/dev/null || true)
done
echo "  done"

echo "==> Checking that .gitignore covers secrets and local state"
for required in '.env' '*.pem' '*.key' '.venv'; do
  # Accept either form, since a directory entry may be written with a
  # trailing slash ('.venv/' and '.venv' both ignore the directory).
  if ! grep -qxF "$required" .gitignore && ! grep -qxF "${required}/" .gitignore; then
    report ".gitignore does not cover '$required'"
  fi
done
echo "  done"

echo "==> Checking that the Kubernetes secret template holds no value"
if grep -qE 'JWT_SECRET_KEY:[[:space:]]*["'"'"']?[A-Za-z0-9_-]{8,}' k8s/secret.example.yaml; then
  report "k8s/secret.example.yaml contains a non-empty JWT_SECRET_KEY"
fi
echo "  done"

echo
if (( findings > 0 )); then
  echo "SECRET AUDIT FAILED: $findings finding(s)"
  exit 1
fi
echo "SECRET AUDIT PASSED: no committed secrets found"
