#!/usr/bin/env bash
# Per-boot startup for the Cloud Agent environment.
#
# The database cluster (roles, schema, migrations) is durable state baked by
# .cursor/install.sh; this script only (re)starts the Postgres server process,
# which does not survive a reboot/snapshot restore. Idempotent.
set -euo pipefail

PG_VERSION=16

if pg_lsclusters -h 2>/dev/null | awk '{print $4}' | grep -q online; then
  echo "[start] PostgreSQL ${PG_VERSION} already online"
else
  echo "[start] starting PostgreSQL ${PG_VERSION}"
  sudo pg_ctlcluster "${PG_VERSION}" main start || sudo service postgresql start
fi

# Block until the server accepts connections so agents never race startup.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    echo "[start] PostgreSQL is accepting connections"
    exit 0
  fi
  sleep 1
done

echo "[start] WARNING: PostgreSQL did not become ready in time" >&2
exit 1
