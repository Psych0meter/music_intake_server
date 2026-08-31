-- Detector enable/disable + config values, editable from the Settings
-- page instead of only via config/secrets.env + a service restart. A
-- generic key/value table (not one column per setting) so new settings
-- can be added later without another schema migration.
--
-- Deliberately seeded with safe generic defaults only - NOT with
-- whatever is currently in config/secrets.env on this box. Reading a
-- live secrets file from inside a SQL migration isn't something
-- migrate.py can do (it just executes each .sql file verbatim), and
-- baking a real API key into a migration would leak it into git
-- history the moment this repo's migrations directory is committed.
-- After upgrading, re-enter ACOUSTID_API_KEY/GENIUS_ACCESS_TOKEN once
-- in the Settings page - see docs/CONFIGURATION.md.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('songrec_enabled', '1'),
    ('acoustid_enabled', '1'),
    ('acoustid_api_key', ''),
    ('genius_enabled', '0'),
    ('genius_access_token', ''),
    ('whisper_model_size', 'small'),
    ('whisper_device', 'cpu'),
    ('whisper_compute_type', 'int8'),
    ('scan_workers', '0');
