-- Tracks per-file processing errors (unreadable files, etc.) so they
-- surface in the review UI instead of silently vanishing from the queue.
ALTER TABLE queue ADD COLUMN error TEXT;