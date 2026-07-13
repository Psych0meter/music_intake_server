-- Speeds up the paginated/sorted queue listing (status + confidence
-- filter/sort combination used by the review UI's pagination).
CREATE INDEX IF NOT EXISTS idx_queue_pagination ON queue(status, confidence);