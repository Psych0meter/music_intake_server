-- Per-source identification results (SongRec vs AcoustID) shown side by
-- side in the review UI, plus the cross-source agreement score used by
-- the majority-vote confidence logic.
ALTER TABLE queue ADD COLUMN sr_artist TEXT;
ALTER TABLE queue ADD COLUMN sr_title TEXT;
ALTER TABLE queue ADD COLUMN sr_album TEXT;
ALTER TABLE queue ADD COLUMN ac_artist TEXT;
ALTER TABLE queue ADD COLUMN ac_title TEXT;
ALTER TABLE queue ADD COLUMN ac_score REAL;
ALTER TABLE queue ADD COLUMN agreement REAL;