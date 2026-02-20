-- Widen analysis_mode to accommodate 'transcript_multimodal' (22 chars)
ALTER TABLE verdicts ALTER COLUMN analysis_mode TYPE varchar(50);
