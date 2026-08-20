"""
Centralized configuration loading.
Precedence (lowest → highest): JSON file → environment variables (KD_*) → explicit overrides.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


REQUIRED_KEYS = ["BasePath", "ZipPath", "LicenseHost", "LicensePort", "ThisHost", "Components", "Ports"]


def _strip_quotes(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().strip('"').strip("'")
    return value


def resolve_default_config_path() -> Path:
    """
    Prefer config/my-config.json (exported by the dashboard) when present,
    otherwise fall back to config/default-config.json.
    """
    root = Path(__file__).resolve().parent.parent
    my_cfg = root / "config" / "my-config.json"
    if my_cfg.is_file():
        return my_cfg
    return root / "config" / "default-config.json"


def get_kd_config(
    config_path: Optional[str | Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Load configuration from JSON, apply env overrides, then explicit overrides.
    Returns a plain dict (mutable).

    When config_path is omitted, prefers config/my-config.json if it exists
    (so dashboard-exported ZipPath / paths are used), else default-config.json.
    """
    if config_path is None:
        config_path = resolve_default_config_path()
    else:
        config_path = Path(config_path)

    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse configuration file '{config_path}': {e}") from e

    config: Dict[str, Any] = dict(raw)

    # Environment variable overrides: KD_BASEPATH, KD_LICENSEHOST, etc.
    for key in list(config.keys()):
        env_name = f"KD_{key.upper()}"
        env_val = os.environ.get(env_name)
        if env_val is not None and env_val != "":
            # Try to keep type for simple cases
            if isinstance(config[key], bool):
                config[key] = env_val.lower() in ("1", "true", "yes", "on")
            elif isinstance(config[key], int):
                try:
                    config[key] = int(env_val)
                except ValueError:
                    config[key] = env_val
            else:
                config[key] = env_val

    # Explicit overrides take highest precedence
    if overrides:
        for k, v in overrides.items():
            config[k] = v

    # Defensive cleanup of string values (Explorer "Copy as path" quotes)
    for key, val in list(config.items()):
        config[key] = _strip_quotes(val)
        if isinstance(val, dict):
            config[key] = {k: _strip_quotes(v) for k, v in val.items()}

    return config


def test_kd_config_schema(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate that required top-level keys are present."""
    missing: List[str] = []
    for field in REQUIRED_KEYS:
        if field not in config:
            missing.append(field)
            continue
        val = config[field]
        if field not in ("Components", "Ports") and (val is None or (isinstance(val, str) and not val.strip())):
            missing.append(field)
    return {
        "IsValid": len(missing) == 0,
        "MissingKeys": missing,
    }


def save_kd_config(config: Dict[str, Any], path: str | Path) -> bool:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def update_config_key(path: str | Path, key: str, value: Any) -> bool:
    """
    Surgically update a single top-level key in an existing JSON config file.
    Preserves all other keys, order, and values exactly as they were on disk.
    Only the target key is added or replaced; the rest of the file is left intact.
    """
    try:
        path = Path(path)
        if not path.is_file():
            return False
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        data[key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except Exception:
        return False


def update_config_nested(path: str | Path, dotted_key: str, value: Any) -> bool:
    """
    Surgically update a nested key in an existing JSON config file.

    ``dotted_key`` uses dot notation, e.g. ``"NiFi.Username"`` or
    ``"NiFi.Password"``. Intermediate objects are created as dicts when missing.
    All other keys are preserved.
    """
    try:
        path = Path(path)
        if not path.is_file():
            return False
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False

        parts = [p for p in str(dotted_key).split(".") if p]
        if not parts:
            return False

        cursor: Any = data
        for part in parts[:-1]:
            nxt = cursor.get(part) if isinstance(cursor, dict) else None
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        if not isinstance(cursor, dict):
            return False
        cursor[parts[-1]] = value

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except Exception:
        return False


def update_nifi_credentials_in_config(
    path: str | Path,
    username: str,
    password: str,
) -> bool:
    """
    Persist NiFi UI credentials into config JSON under NiFi.Username / NiFi.Password.
    Returns True when both keys were written successfully.
    """
    ok_user = update_config_nested(path, "NiFi.Username", username)
    ok_pass = update_config_nested(path, "NiFi.Password", password)
    return bool(ok_user and ok_pass)
