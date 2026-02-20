-- Migration 017: Add audio/video transcription support columns to events table.
-- transcript_source: how the transcript was obtained ('graph_api', 'whisper', or NULL)
-- media_duration_seconds: duration of the audio/video file in seconds

ALTER TABLE events ADD COLUMN IF NOT EXISTS transcript_source VARCHAR(50);
ALTER TABLE events ADD COLUMN IF NOT EXISTS media_duration_seconds INT;
