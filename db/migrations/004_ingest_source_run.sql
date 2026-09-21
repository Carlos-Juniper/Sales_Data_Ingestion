CREATE TABLE ingest.source_run (
  source_run_id     bigserial PRIMARY KEY,
  source_id         text NOT NULL,
  run_started_at    timestamptz NOT NULL DEFAULT now(),
  byte_count        bigint,
  sha256            text,
  row_count         integer,
  connector_version text,
  license_string    text,
  status            text NOT NULL DEFAULT 'running'  -- running|succeeded|failed
);
