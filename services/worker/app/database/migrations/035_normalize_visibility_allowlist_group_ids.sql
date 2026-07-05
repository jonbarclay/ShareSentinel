-- Normalize visibility allow-list group IDs to lowercase.
-- Graph returns lowercase GUIDs; dashboard-entered values were stored verbatim,
-- and the scanner's membership check was previously case-sensitive — a
-- mixed-case entry silently lost its allow-listing (group flipped Private).
-- The dashboard now lowercases on insert and the scanner compares
-- case-insensitively; this backfills any pre-existing mixed-case rows.

UPDATE site_visibility_allowlist
SET group_id = LOWER(TRIM(group_id))
WHERE group_id <> LOWER(TRIM(group_id));
