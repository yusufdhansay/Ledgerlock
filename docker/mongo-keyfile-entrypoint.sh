#!/usr/bin/env bash
#
# Entrypoint wrapper that gives mongod a keyfile before starting it.
#
# Why this exists: enabling authorization on a replica set *requires* internal
# authentication between members. Verified empirically rather than assumed --
# starting `mongod --replSet rs0 --auth` with no keyfile exits with code 2 and
# logs:
#
#   BadValue: security.keyFile is required when authorization is enabled
#   with replica sets
#
# So a keyfile is mandatory, and a keyfile is a shared secret. It is generated
# here, inside the container, on a volume, rather than committed to the
# repository or baked into the image. For a single-member set its only job is
# to let the member authenticate to itself, but it is still a credential and is
# treated as one: 0400, owned by mongod's user, never printed.
#
# A multi-member deployment would need the *same* keyfile on every member,
# which means distributing it through a secret manager rather than generating
# it per container. Noted so this script is not copied into a real cluster
# unchanged.

set -euo pipefail

KEYFILE_DIR="${LEDGERLOCK_KEYFILE_DIR:-/etc/ledgerlock}"
KEYFILE="${KEYFILE_DIR}/mongo-keyfile"

mkdir -p "$KEYFILE_DIR"

if [[ ! -s "$KEYFILE" ]]; then
  # 756 bytes of base64 is the length MongoDB's documentation uses for a
  # generated keyfile.
  openssl rand -base64 756 > "$KEYFILE"
  echo "[keyfile] generated a new internal authentication keyfile"
else
  echo "[keyfile] reusing the existing keyfile"
fi

# mongod refuses to start if the keyfile is group- or world-readable.
chmod 400 "$KEYFILE"
chown mongodb:mongodb "$KEYFILE" 2>/dev/null || true

# Hand off to the image's own entrypoint so that MONGO_INITDB_ROOT_USERNAME
# and MONGO_INITDB_ROOT_PASSWORD are still honoured: it starts a temporary
# mongod, creates the root user, shuts it down, then starts the real one.
exec docker-entrypoint.sh mongod \
  --replSet "${LEDGERLOCK_REPLICA_SET:-rs0}" \
  --bind_ip_all \
  --port 27017 \
  --keyFile "$KEYFILE" \
  "$@"
