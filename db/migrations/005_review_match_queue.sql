CREATE TABLE review.match_queue (
  match_queue_id    bigserial PRIMARY KEY,
  source_record_a   bigint NOT NULL REFERENCES core.source_record(source_record_id),
  source_record_b   bigint NOT NULL REFERENCES core.source_record(source_record_id),
  score             numeric NOT NULL,
  feature_breakdown jsonb,
  status            text NOT NULL DEFAULT 'pending',  -- pending|merged|rejected
  created_at        timestamptz NOT NULL DEFAULT now(),
  resolved_at       timestamptz,
  resolved_by       text
);
