-- Add modification time tracking for file change detection
ALTER TABLE queue ADD COLUMN mtime REAL;