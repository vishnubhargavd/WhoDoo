-- ============================================================================
-- LegacyVault: Leadership Continuity and Club History
--
-- Preserves handover records, event debriefs, financial summaries,
-- and contact directories for newly authorized leadership terms.
-- ============================================================================

CREATE TABLE legacy_handover_records (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    club_id       uuid NOT NULL,
    term_year     text NOT NULL CHECK (length(btrim(term_year)) BETWEEN 4 AND 16),
    title         text NOT NULL CHECK (length(btrim(title)) BETWEEN 3 AND 200),
    category      text NOT NULL CHECK (category IN ('handover_note', 'event_summary', 'finance_summary', 'contact_directory', 'governance')),
    content       text NOT NULL CHECK (length(btrim(content)) > 0),
    author_id     uuid NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
    event_id      uuid REFERENCES events (id) ON DELETE SET NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX legacy_handover_club_term_idx ON legacy_handover_records (club_id, term_year DESC, created_at DESC);
CREATE INDEX legacy_handover_category_idx ON legacy_handover_records (club_id, category);
