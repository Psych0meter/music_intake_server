"""
Reads/writes the `settings` table (see migrations/0011_add_settings.sql) -
the detector enable/disable flags and config values (API keys, Whisper
model, SCAN_WORKERS) that used to live only in config/secrets.env, now
editable from the Settings page without a service restart.

This module is intentionally duplicated between app/ and pipeline/
rather than imported from a shared package - matching this codebase's
existing convention (get_db(), load_scan_roots() are already duplicated
the same way between app/server.py and pipeline/recognize.py) rather
than introducing a new cross-directory import path and the install-
script/deployment changes that would require.
"""

# key -> (type, default). Defaults are used when a key's row is missing
# entirely (e.g. a fresh DB not yet through migration 0011, or a key
# added in a later release before the corresponding migration runs).
SETTINGS_SCHEMA = {
    "songrec_enabled": ("bool", True),
    "acoustid_enabled": ("bool", True),
    "acoustid_api_key": ("str", ""),
    "genius_enabled": ("bool", False),
    "genius_access_token": ("str", ""),
    "whisper_model_size": ("str", "small"),
    "whisper_device": ("str", "cpu"),
    "whisper_compute_type": ("str", "int8"),
    "scan_workers": ("int", 0),  # 0 = auto (min(8, cpu_count))
}

# Keys whose value should never be echoed back verbatim over the API.
SECRET_KEYS = {"acoustid_api_key", "genius_access_token"}


def _coerce(value_str, kind):
    if value_str is None:
        return None
    if kind == "bool":
        return value_str == "1"
    if kind == "int":
        try:
            return int(value_str)
        except (TypeError, ValueError):
            return None
    return value_str


def _encode(value, kind):
    if kind == "bool":
        return "1" if value else "0"
    return "" if value is None else str(value)


def get_settings(conn):
    """Returns every known setting as a plain dict, DB rows overlaid onto
    SETTINGS_SCHEMA defaults so a key is always present even if its row
    doesn't exist yet."""
    result = {key: default for key, (_, default) in SETTINGS_SCHEMA.items()}
    for row in conn.execute("SELECT key, value FROM settings"):
        key, raw = row[0], row[1]
        if key in SETTINGS_SCHEMA:
            kind, _ = SETTINGS_SCHEMA[key]
            coerced = _coerce(raw, kind)
            if coerced is not None:
                result[key] = coerced
    return result


def update_settings(conn, updates):
    """Upserts only keys present in SETTINGS_SCHEMA (an allowlist -
    anything else in `updates` is silently ignored, never written).
    Returns the list of keys actually written."""
    written = []
    for key, value in updates.items():
        if key not in SETTINGS_SCHEMA:
            continue
        kind, _ = SETTINGS_SCHEMA[key]
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _encode(value, kind))
        )
        written.append(key)
    conn.commit()
    return written
