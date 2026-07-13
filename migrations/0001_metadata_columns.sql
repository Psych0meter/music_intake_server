-- filesize/duration/filehash - used by the review UI and exact-duplicate
-- detection (grouping rows that share the same content hash).
ALTER TABLE queue ADD COLUMN filesize INTEGER;
ALTER TABLE queue ADD COLUMN duration REAL;
ALTER TABLE queue ADD COLUMN filehash TEXT;