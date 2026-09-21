-- D7: Record where the raw fetched bytes were landed in GCS.
--
-- raw_uri holds the fully-qualified GCS object URI written by the connector,
-- e.g. gs://juniper-ingest-raw/cms_general/2026-08-20/<sha256>.json.gz
-- NULL until the connector uploads (rows inserted by write_source_run start
-- with status='running' and raw_uri=NULL; finish_source_run sets both).

ALTER TABLE ingest.source_run
  ADD COLUMN IF NOT EXISTS raw_uri text;
