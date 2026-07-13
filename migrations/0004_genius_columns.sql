-- Genius lyrics-fallback identification result columns, matching your
-- current server.py's required_columns (gn_artist/gn_title).
ALTER TABLE queue ADD COLUMN gn_artist TEXT;
ALTER TABLE queue ADD COLUMN gn_title TEXT;