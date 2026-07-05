# Review Working Data — Handling & Retention

The repo working tree accumulates review artifacts that contain **real PII/PHI**
(downloaded flagged documents, legal-review exports, breach-review CSVs, access
checkpoint exports, run logs). As of 2026-07-05 these include:

- `review_downloads/` — downloaded Tier 1/Tier 2 documents organized per user, plus
  `legal_phi/`, `legal_pii/`, `manual_review_videos/`, and review CSV/log artifacts
- Root-level exports: `access_checkpoint_part*.csv`, `BroadSharing_Filtered_*.csv`,
  `flagged_tier1_tier2_export_*.csv`, `onedrive_tier1_tier2_breach_review_*.csv`,
  `public-sites_*.csv`, `cws_failed_users_*.csv`, `cws_stuck_users_*.csv`

## Rules

1. **Never commit this data.** `.gitignore` now excludes `review_downloads/`, root/scripts
   CSVs, `*.log`, and `*.jsonl`. If you add a new artifact type, extend `.gitignore` first.
   Note: gitleaks CI detects secrets, not PII — the ignore rules are the only guard.
2. **Storage location.** This data should live outside the repo tree on an
   access-restricted, encrypted volume (recommended: `/srv/sharesentinel-reviews/`,
   `chmod 700`, owner-only). Scripts that write review artifacts should take an output
   directory argument pointing there.
3. **Retention.** Delete review exports when the associated review/legal matter is closed,
   or after 90 days, whichever comes first — the authoritative records live in Postgres
   (`site_breach_review`, `targeted_pii_review`, `targeted_phi_review`, `verdicts`).
   Downloaded document copies in `review_downloads/` are working copies only.
4. **Database dumps** (`backups/`, produced by `export_db.sh`) contain the same sensitive
   verdict data in plaintext; apply the same storage/retention rules.
