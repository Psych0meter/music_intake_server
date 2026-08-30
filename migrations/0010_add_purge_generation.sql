-- Bumped by the review UI's Purge Queue action so a scan pass already
-- in progress can notice its queue was cleared out from under it and
-- stop immediately, instead of continuing to re-insert the files
-- already in its in-memory batch list.
ALTER TABLE scan_status ADD COLUMN generation INTEGER DEFAULT 0;
