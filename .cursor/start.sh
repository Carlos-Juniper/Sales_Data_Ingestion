#!/usr/bin/env bash
# Per-boot startup for the Cloud Agent environment.
#
# The database cluster (roles, schema, migrations) is durable state baked by
# .cursor/install.sh; this script only (re)starts the Postgres server process,
# which does not survive a reboot/snapshot restore. Idempotent.
#
# PG_VERSION is the single source of truth for the Postgres major. install.sh
# reads the bare PG_VERSION= assignment below — keep that line unchanged.
set -euo pipefail

PG_VERSION=18

if pg_lsclusters -h 2>/dev/null | awk -v v="$PG_VERSION" '$1 == v && $2 == "main" && $4 == "online" { found = 1 } END { exit !found }'; then
  echo "[start] PostgreSQL ${PG_VERSION} already online"
else
  echo "[start] starting PostgreSQL ${PG_VERSION}"
  sudo pg_ctlcluster "${PG_VERSION}" main start
fi

# Block until the server accepts connections so agents never race startup.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    echo "[start] PostgreSQL is accepting connections"
    exit 0
  fi
  sleep 1
done

echo "[start] PostgreSQL did not become ready in time" >&2
exit 1
