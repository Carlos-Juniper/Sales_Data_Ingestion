#!/usr/bin/env bash
# Cloud Agent environment bootstrap for the arcgis-ingest-connectors pipeline.
#
# Idempotent: safe to re-run. Prepares durable state that persists into an
# environment snapshot:
#   - system packages (Python 3.13, PostgreSQL 18 + PostGIS 3.5, OCR tooling)
#   - a Python 3.13 virtualenv at .venv with the connector deps + pytest
#   - a local database owned by the role in .env, migrated to head
#   - a gitignored .env copied from .env.example
#
# Per-boot work (starting the Postgres server) lives in .cursor/start.sh.
# The Postgres major version is defined only there.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Single source of truth: the bare PG_VERSION= line in start.sh.
PG_VERSION="$(sed -n 's/^PG_VERSION=//p' "$REPO_DIR/.cursor/start.sh")"
if ! [[ "$PG_VERSION" =~ ^[0-9]+$ ]]; then
  echo "[install] could not read PG_VERSION from .cursor/start.sh" >&2
  exit 1
fi

# Latest PostGIS 3.5 that upstream builds against PostgreSQL 18 (3.5.3+).
# PGDG's postgresql-18-postgis-3 packages are 3.6+; 3.5 was never published
# for PostgreSQL 18. Prefer a 3.5 apt package when one exists, otherwise
# build this release. docker-compose pins the same 3.5 series
# (imresamu/postgis:18-3.5-alpine, which is 3.5.3).
POSTGIS_VERSION=3.5.7

echo "[install] loading .env"
# cp -n exits non-zero on coreutils >= 9.2 when the destination already exists.
cp -n .env.example .env || [ -f .env ]
set -a
# shellcheck disable=SC1091
. ./.env
set +a
: "${POSTGRES_USER:?POSTGRES_USER is not set in .env}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB is not set in .env}"

echo "[install] installing system packages"
export DEBIAN_FRONTEND=noninteractive

# apt-get update is expensive. Track whether the package lists are already
# current so a machine that already has Python 3.13 does not install
# software-properties-common or run a second update for the deadsnakes PPA.
lists_fresh=0
refresh_lists() {
  if [ "$lists_fresh" -eq 0 ]; then
    sudo apt-get update -y
    lists_fresh=1
  fi
}

# Python 3.13 is not in Ubuntu 24.04's default repos.
if ! command -v python3.13 >/dev/null 2>&1; then
  refresh_lists
  sudo apt-get install -y --no-install-recommends software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  lists_fresh=1
fi

# PostgreSQL 18 comes from PGDG. Ubuntu 24.04 only ships PostgreSQL 16.
if [ ! -f /etc/apt/sources.list.d/pgdg.sources ] && [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
  refresh_lists
  sudo apt-get install -y --no-install-recommends postgresql-common ca-certificates
  sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
  lists_fresh=1
fi

refresh_lists
# libpq-dev, python3.13-dev, curl, and git are not required: psycopg[binary]
# ships wheels, and this script never invokes curl or git.
sudo apt-get install -y --no-install-recommends \
  python3.13 python3.13-venv \
  "postgresql-${PG_VERSION}" \
  libpq5 \
  tesseract-ocr poppler-utils

install_postgis_from_apt() {
  local pkg="postgresql-${PG_VERSION}-postgis-3"
  local scripts="postgresql-${PG_VERSION}-postgis-3-scripts"
  local ver sver
  ver="$(apt-cache madison "$pkg" | awk '/3\.5\./ { print $3; exit }' || true)"
  if [ -z "$ver" ]; then
    return 1
  fi
  echo "[install] installing ${pkg}=${ver} from PGDG"
  sver="$(apt-cache madison "$scripts" | awk -v v="$ver" '$3 == v { print $3; exit }' || true)"
  if [ -n "$sver" ]; then
    sudo apt-get install -y --no-install-recommends "${pkg}=${ver}" "${scripts}=${sver}"
  else
    sudo apt-get install -y --no-install-recommends "${pkg}=${ver}"
  fi
}

install_postgis_from_source() {
  local control="/usr/share/postgresql/${PG_VERSION}/extension/postgis.control"
  if [ -f "$control" ] && grep -Eq "^default_version = '3\\.5\\." "$control"; then
    echo "[install] PostGIS 3.5 already installed"
    return 0
  fi

  echo "[install] PGDG has no PostGIS 3.5 package for PostgreSQL ${PG_VERSION}; building PostGIS ${POSTGIS_VERSION}"
  sudo apt-get install -y --no-install-recommends \
    build-essential \
    "postgresql-server-dev-${PG_VERSION}" \
    libgeos-dev \
    libproj-dev \
    libjson-c-dev \
    libxml2-dev \
    pkg-config

  local tarball="/tmp/postgis-${POSTGIS_VERSION}.tar.gz"
  local src="/tmp/postgis-${POSTGIS_VERSION}"
  python3.13 - "$POSTGIS_VERSION" "$tarball" <<'PY'
import sys
import urllib.request

version, dest = sys.argv[1], sys.argv[2]
url = f"https://download.osgeo.org/postgis/source/postgis-{version}.tar.gz"
urllib.request.urlretrieve(url, dest)
PY
  rm -rf "$src"
  tar -xzf "$tarball" -C /tmp
  (
    cd "$src"
    ./configure \
      --with-pgconfig="/usr/lib/postgresql/${PG_VERSION}/bin/pg_config" \
      --without-raster \
      --without-topology \
      --without-protobuf
    make -j"$(nproc)"
    sudo make install
  )
  rm -rf "$src" "$tarball"

  if ! grep -Eq "^default_version = '3\\.5\\." "$control"; then
    echo "[install] PostGIS 3.5 did not install into ${control}" >&2
    exit 1
  fi
}

if ! install_postgis_from_apt; then
  install_postgis_from_source
fi

echo "[install] ensuring PostgreSQL cluster is running"
bash "$REPO_DIR/.cursor/start.sh"

echo "[install] provisioning role and database from .env"
# SUPERUSER is required so migration 001 can CREATE EXTENSION postgis. This is a
# throwaway local dev database, never production. Identifiers and the password
# are passed as psql variables and quoted with format(), not interpolated.
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -v pguser="$POSTGRES_USER" \
  -v pgpass="$POSTGRES_PASSWORD" \
  -v pgdb="$POSTGRES_DB" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'pguser', :'pgpass')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'pguser')
\gexec
SELECT format('ALTER ROLE %I WITH SUPERUSER LOGIN PASSWORD %L', :'pguser', :'pgpass')
\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'pgdb', :'pguser')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'pgdb')
\gexec
SQL

echo "[install] creating Python 3.13 virtualenv and installing dependencies"
if [ ! -x .venv/bin/python ]; then
  python3.13 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements-dev.txt

echo "[install] applying database migrations"
python db/run_migrations.py

ext_ver="$(sudo -u postgres psql -d "$POSTGRES_DB" -tAc "SELECT extversion FROM pg_extension WHERE extname = 'postgis'")"
server_ver="$(sudo -u postgres psql -d "$POSTGRES_DB" -tAc "SHOW server_version")"
echo "[install] PostgreSQL ${server_ver}, PostGIS ${ext_ver}"
case "$ext_ver" in
  3.5.*) ;;
  *)
    echo "[install] expected PostGIS 3.5, found '${ext_ver}'" >&2
    exit 1
    ;;
esac
case "$server_ver" in
  "${PG_VERSION}".*) ;;
  *)
    echo "[install] expected PostgreSQL ${PG_VERSION}, found '${server_ver}'" >&2
    exit 1
    ;;
esac

echo "[install] done"
