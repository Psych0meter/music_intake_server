-- Speeds up duplicate-detection lookups on filehash once the queue has
-- thousands of rows rather than dozens.
CREATE INDEX IF NOT EXISTS idx_queue_filehash ON queue(filehash);