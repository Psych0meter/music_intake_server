-- The blended "confidence" score (a heuristic percentage synthesized
-- across SongRec/AcoustID/Genius agreement, iTunes verification, and a
-- handful of arbitrary tiers - see pipeline/recognize.py's git history)
-- and the "agreement" cross-source similarity it partly fed became
-- redundant once the review UI grew a per-detector-column score display
-- (AcoustID's ac_score, already shown per-row) and a per-field picker
-- showing each detector's own raw value - a separate blended number no
-- longer added information over what's already on screen per source,
-- and `agreement` itself was already write-only (computed, stored,
-- never actually read back by any page). Whether a track is
-- "Unrecognized" is now derived directly from artist/title both being
-- NULL, which is exactly the condition identify_and_tag() already used
-- internally to decide confidence was 0 - see app/server.py and
-- app/templates/index.html.

-- idx_queue_pagination (from 0005_pagination_index.sql) indexed
-- (status, confidence) for the review queue's old default status+
-- confidence sort/filter - it has to be dropped before the column below
-- (SQLite refuses to drop a column an index still references), and
-- there's no confidence to sort by any more to replace it with. A plain
-- status index still helps every query here that filters on status
-- (the review queue's status='pending', history's optional status
-- filter) regardless of which column the caller sorts by.
DROP INDEX IF EXISTS idx_queue_pagination;
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);

ALTER TABLE queue DROP COLUMN confidence;
ALTER TABLE queue DROP COLUMN agreement;
