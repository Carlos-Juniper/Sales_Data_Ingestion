"""
GCS raw-landing helper — content-addressed upload to gs://juniper-ingest-raw.

All public functions gracefully degrade to None / no-op when:
  - google-cloud-storage is not installed
  - Application Default Credentials (ADC) are unavailable
  - DISABLE_GCS=1 is set in the environment

This means connectors that call upload_raw never fail in local dev even when
the GCS dependency is absent.  Real uploads happen only in Cloud Run where
the SA ingestion-connector@... holds bucket-scoped storage.objectAdmin.

Bucket is controlled by the GCS_RAW_BUCKET env var (default juniper-ingest-raw).

Contract (other agents depend on these exact signatures):
    upload_raw(source_id, run_date, raw_bytes, *, gzip=True, suffix="json") -> str | None
    raw_sha256(raw_bytes) -> str
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_BUCKET = "juniper-ingest-raw"


def raw_sha256(raw_bytes: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *raw_bytes*.

    Computed over the actual transferred bytes — not a re-serialisation of a
    pandas DataFrame.  This is the fix for B4: the same bytes always produce
    the same digest regardless of pandas version or column order.

    Args:
        raw_bytes: The bytes to hash (e.g. the raw HTTP response body, or a
                   file read verbatim from disk).

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(raw_bytes).hexdigest()


def upload_raw(
    source_id: str,
    run_date: str,
    raw_bytes: bytes,
    *,
    gzip: bool = True,
    suffix: str = "json",
) -> Optional[str]:
    """Upload *raw_bytes* to the GCS raw-landing bucket and return the gs:// URI.

    Object path is content-addressed:
        gs://<bucket>/<source_id>/<run_date>/<sha256>.<suffix>.gz  (gzip=True)
        gs://<bucket>/<source_id>/<run_date>/<sha256>.<suffix>     (gzip=False)

    The default suffix is ``json``, so existing callers keep the historical
    ``.json.gz`` / ``.json`` names. Pass ``suffix="pdf"`` for a real PDF
    payload (TX TREC management certificates).

    Because the object name is derived from the SHA-256 of *raw_bytes*, a
    re-run with unchanged bytes writes to the exact same object path —
    satisfying the §3 idempotency contract (0 net new GCS objects when data
    is unchanged).

    The sha256 is computed over *raw_bytes* (the bytes before gzip compression)
    so that the digest stays stable regardless of the gzip implementation.

    Args:
        source_id: Connector identifier, e.g. ``"cms_general"``.
        run_date:  ISO date string ``"YYYY-MM-DD"`` for the partition prefix.
        raw_bytes: The raw payload to store — real fetched/read bytes, not a
                   pandas re-serialisation.
        gzip:      When True (default), compress with gzip before uploading.
                   The object name ends in ``.<suffix>.gz`` in that case.
        suffix:    File suffix without a leading dot. Default ``"json"``.
                   Must be alphanumeric (no path separators).

    Returns:
        The full ``gs://`` URI on success, or ``None`` when GCS is
        unavailable / disabled (so the connector path never raises for an
        infra reason). A bad ``suffix`` still raises: that is a caller bug,
        not an infrastructure miss.
    """
    # Validate before the DISABLE_GCS short-circuit so a path-injection
    # suffix fails the same way in local dev as it does in Cloud Run.
    suffix = _validate_suffix(suffix)

    # Honour explicit local-dev bypass first — cheapest check.
    if os.environ.get("DISABLE_GCS", "").strip() in ("1", "true", "yes"):
        logger.debug("upload_raw: GCS disabled via DISABLE_GCS — skipping upload")
        return None

    # Lazy import: importing lib.gcs must never hard-require google-cloud-storage.
    try:
        from google.cloud import storage as gcs_storage  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "upload_raw: google-cloud-storage is not installed — "
            "skipping GCS upload. Install it with: "
            "pip install google-cloud-storage"
        )
        return None

    bucket_name = os.environ.get("GCS_RAW_BUCKET", _DEFAULT_BUCKET)

    # SHA-256 is always over the *uncompressed* raw_bytes so the digest is
    # stable and independent of the gzip implementation.
    sha = raw_sha256(raw_bytes)
    extension = f"{suffix}.gz" if gzip else suffix
    object_name = f"{source_id}/{run_date}/{sha}.{extension}"

    payload = _compress_gzip(raw_bytes) if gzip else raw_bytes
    content_type = _content_type(suffix, gzip=gzip)

    try:
        client = gcs_storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(payload, content_type=content_type)
        uri = f"gs://{bucket_name}/{object_name}"
        logger.info(
            "upload_raw: uploaded %d bytes -> %s (sha256=%s…)",
            len(payload),
            uri,
            sha[:16],
        )
        return uri
    except Exception as exc:  # noqa: BLE001
        # Any credential, network, or permission error must not propagate into
        # the connector path — GCS is advisory infrastructure, not a blocker.
        logger.warning(
            "upload_raw: GCS upload failed — %s: %s. "
            "Continuing without raw landing.",
            type(exc).__name__,
            exc,
        )
        return None


# ------------------------------------------------------------------ internals


def _validate_suffix(suffix: str) -> str:
    """Reject anything that could change the object-path prefix.

    ``suffix`` is interpolated into the GCS object name. A value containing
    a slash, dot, or empty string would let a caller escape the
    ``{source_id}/{run_date}/`` partition.
    """
    cleaned = (suffix or "").strip().lstrip(".")
    if not cleaned or not cleaned.isalnum():
        raise ValueError(
            f"upload_raw suffix must be alphanumeric, got {suffix!r}"
        )
    return cleaned


def _content_type(suffix: str, *, gzip: bool) -> str:
    if gzip:
        return "application/gzip"
    if suffix == "pdf":
        return "application/pdf"
    if suffix == "json":
        return "application/json"
    return "application/octet-stream"


def _compress_gzip(data: bytes) -> bytes:
    """Return gzip-compressed *data* using the stdlib ``gzip`` module.

    Uses compresslevel=6 — the stdlib default, balancing speed and ratio.
    Named ``_compress_gzip`` (not ``_gzip``) so it doesn't shadow the stdlib
    module in this namespace; the ``gzip`` parameter in ``upload_raw`` already
    occupies that name in that scope.
    """
    import gzip as _gzip_mod  # local import to keep module-level deps minimal

    buf = io.BytesIO()
    with _gzip_mod.open(buf, "wb", compresslevel=6) as gz:
        gz.write(data)
    return buf.getvalue()
