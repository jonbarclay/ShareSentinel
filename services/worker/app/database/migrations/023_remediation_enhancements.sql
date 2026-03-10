-- Migration 023: Remediation cascade and validation enhancements
-- Adds parent cascade tracking, post-removal validation columns to remediations.

ALTER TABLE remediations ADD COLUMN parent_remediation_id INT REFERENCES remediations(id);
ALTER TABLE remediations ADD COLUMN cascade_source_event_id VARCHAR(128);
ALTER TABLE remediations ADD COLUMN validation_passed BOOLEAN;
ALTER TABLE remediations ADD COLUMN validation_details JSONB DEFAULT '[]'::jsonb;

CREATE INDEX idx_remediations_parent ON remediations(parent_remediation_id)
  WHERE parent_remediation_id IS NOT NULL;

-- Partial unique index to prevent duplicate active remediation rows per event.
-- Completed/failed rows are excluded so re-remediation is still possible.
CREATE UNIQUE INDEX idx_remediations_event_active
  ON remediations(event_id)
  WHERE status IN ('pending', 'in_progress', 'completed', 'completed_with_warnings');
