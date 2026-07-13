CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT UNIQUE NOT NULL,
    artist TEXT,
    album TEXT,
    title TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending', -- pending|approved|rejected
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scan_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    current_file TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);