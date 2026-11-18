-- Keep recoverable metadata for files removed from the review queue.
CREATE TABLE IF NOT EXISTS file_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL,
    original_filepath TEXT NOT NULL,
    quarantine_filepath TEXT NOT NULL UNIQUE,
    queue_data TEXT NOT NULL,
    quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_file_quarantine_quarantined_at
    ON file_quarantine(quarantined_at);
