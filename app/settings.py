"""Typed, validated settings shared by the review UI and recognition worker."""

# key -> (type, default)
SETTINGS_SCHEMA = {
    "songrec_enabled": ("bool", True),
    "acoustid_enabled": ("bool", True),
    "acoustid_api_key": ("str", ""),
    "genius_enabled": ("bool", False),
    "genius_access_token": ("str", ""),
    "whisper_model_size": ("str", "small"),
    "whisper_device": ("str", "cpu"),
    "whisper_compute_type": ("str", "int8"),
    "scan_workers": ("int", 0),  # 0 = automatic
}

SECRET_KEYS = {"acoustid_api_key", "genius_access_token"}
ENUM_VALUES = {
    "whisper_model_size": {"tiny", "base", "small", "medium", "large"},
    "whisper_device": {"cpu", "cuda"},
    "whisper_compute_type": {"int8", "float16", "float32"},
}
MAX_SCAN_WORKERS = 32
MAX_SECRET_LENGTH = 4096


def _normalize_value(key, value):
    """Validate and normalize a value supplied by the API or loaded from DB."""
    if key not in SETTINGS_SCHEMA:
        raise ValueError(f"Unknown setting: {key}")
    kind, _default = SETTINGS_SCHEMA[key]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        # DB values are stored as strings; do not accept arbitrary truthy values.
        if value in ("1", 1):
            return True
        if value in ("0", 0):
            return False
        raise ValueError(f"{key} must be a boolean")
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        if not 0 <= value <= MAX_SCAN_WORKERS:
            raise ValueError(f"{key} must be between 0 and {MAX_SCAN_WORKERS}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if key in ENUM_VALUES:
        if value not in ENUM_VALUES[key]:
            choices = ", ".join(sorted(ENUM_VALUES[key]))
            raise ValueError(f"{key} must be one of: {choices}")
        return value
    if key in SECRET_KEYS:
        if len(value) > MAX_SECRET_LENGTH:
            raise ValueError(f"{key} is too long (maximum {MAX_SECRET_LENGTH} characters)")
        if any(ord(ch) < 32 and ch not in "\t" for ch in value):
            raise ValueError(f"{key} contains unsupported control characters")
        return value
    raise ValueError(f"No validator configured for {key}")


def _coerce(value_str, kind, key=None):
    if value_str is None:
        return None
    if kind == "bool":
        if value_str not in ("0", "1", 0, 1):
            return None
        normalized = value_str == "1" or value_str == 1
    elif kind == "int":
        try:
            normalized = int(value_str)
        except (TypeError, ValueError):
            return None
    else:
        normalized = value_str
    if key is not None:
        try:
            return _normalize_value(key, normalized)
        except (ValueError, TypeError):
            return None
    return normalized


def _encode(value, kind):
    if kind == "bool":
        return "1" if value else "0"
    return "" if value is None else str(value)


def get_settings(conn):
    """Return validated settings overlaid on safe defaults."""
    result = {key: default for key, (_, default) in SETTINGS_SCHEMA.items()}
    for row in conn.execute("SELECT key, value FROM settings"):
        key, raw = row[0], row[1]
        if key in SETTINGS_SCHEMA:
            kind, _ = SETTINGS_SCHEMA[key]
            coerced = _coerce(raw, kind, key)
            if coerced is not None:
                result[key] = coerced
    return result


def validate_updates(updates):
    """Return normalized updates or raise ValueError without partial writes."""
    if not isinstance(updates, dict):
        raise ValueError("Settings payload must be a JSON object")
    normalized = {}
    for key, value in updates.items():
        if key not in SETTINGS_SCHEMA:
            raise ValueError(f"Unknown setting: {key}")
        normalized[key] = _normalize_value(key, value)
    return normalized


def update_settings(conn, updates):
    """Validate all supplied values before atomically upserting any setting."""
    normalized = validate_updates(updates)
    written = []
    try:
        for key, value in normalized.items():
            kind, _ = SETTINGS_SCHEMA[key]
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _encode(value, kind)),
            )
            written.append(key)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written
