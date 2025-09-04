# config_handler.py

import os
import json
from typing import Dict, Any

try:
    import yaml
except ImportError:
    yaml = None

# --- Constants ---
CONFIG_JSON = "config.json"
CONFIG_YAML = "config.yaml"
TOKENS_FILE = "tokens.json"
DEFAULT_REDIRECT_URI = "http://localhost:8000/oauth/callback"
DEFAULT_MIN_SPEED_MPS = 0.5

# --- Private Helpers ---
def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def _write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _read_yaml(path: str) -> Dict[str, Any]:
    if not os.path.exists(path) or not yaml:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

# --- Public Configuration Functions ---
def load_config() -> Dict[str, Any]:
    """
    Loads config by merging sources in order: defaults, environment variables,
    legacy config.yaml, and finally config.json (which takes precedence).
    """
    cfg = {
        "STRAVA_CLIENT_ID": os.getenv("STRAVA_CLIENT_ID", ""),
        "STRAVA_CLIENT_SECRET": os.getenv("STRAVA_CLIENT_SECRET", ""),
        "STRAVA_REDIRECT_URI": os.getenv("STRAVA_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        "MAPBOX_TOKEN": os.getenv("MAPBOX_TOKEN", ""),
        "APPLY_MOVING_FILTER": os.getenv("APPLY_MOVING_FILTER", "true").lower() != "false",
        "MIN_MOVING_SPEED_MPS": float(os.getenv("MIN_MOVING_SPEED_MPS", str(DEFAULT_MIN_SPEED_MPS))),
        "APP_USERNAME": os.getenv("APP_USERNAME", ""),
        "APP_PASSWORD": os.getenv("APP_PASSWORD", ""),
    }

    # Legacy YAML
    legacy = _read_yaml(CONFIG_YAML)
    if isinstance(legacy, dict):
        cfg["APP_USERNAME"] = str(legacy.get("app_username", cfg["APP_USERNAME"]))
        cfg["APP_PASSWORD"] = str(legacy.get("app_password", cfg["APP_PASSWORD"]))
        s = legacy.get("strava") or {}
        if isinstance(s, dict):
            if s.get("client_id"): cfg["STRAVA_CLIENT_ID"] = str(s["client_id"])
            if s.get("client_secret"): cfg["STRAVA_CLIENT_SECRET"] = str(s["client_secret"])
        if legacy.get("mapbox_token"): cfg["MAPBOX_TOKEN"] = str(legacy["mapbox_token"])

    # JSON overrides (written by the app)
    js = _read_json(CONFIG_JSON)
    if isinstance(js, dict):
        for key in cfg:
            if key in js and js[key] is not None:
                cfg[key] = js[key]

    # Coerce types
    cfg["APPLY_MOVING_FILTER"] = str(cfg.get("APPLY_MOVING_FILTER", True)).lower() in ('true', '1', 'yes')
    try:
        cfg["MIN_MOVING_SPEED_MPS"] = float(cfg.get("MIN_MOVING_SPEED_MPS", DEFAULT_MIN_SPEED_MPS))
    except (ValueError, TypeError):
        cfg["MIN_MOVING_SPEED_MPS"] = DEFAULT_MIN_SPEED_MPS
        
    return cfg

def save_config(cfg: Dict[str, Any]) -> None:
    """Writes the flat config structure to config.json."""
    minimal_cfg = {
        "STRAVA_CLIENT_ID": cfg.get("STRAVA_CLIENT_ID", ""),
        "STRAVA_CLIENT_SECRET": cfg.get("STRAVA_CLIENT_SECRET", ""),
        "STRAVA_REDIRECT_URI": cfg.get("STRAVA_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        "MAPBOX_TOKEN": cfg.get("MAPBOX_TOKEN", ""),
        "APPLY_MOVING_FILTER": bool(cfg.get("APPLY_MOVING_FILTER", True)),
        "MIN_MOVING_SPEED_MPS": float(cfg.get("MIN_MOVING_SPEED_MPS", DEFAULT_MIN_SPEED_MPS)),
        "APP_USERNAME": cfg.get("APP_USERNAME", ""),
        "APP_PASSWORD": cfg.get("APP_PASSWORD", ""),
    }
    _write_json(CONFIG_JSON, minimal_cfg)

# --- Token Management ---
def load_tokens() -> Dict[str, Any]:
    return _read_json(TOKENS_FILE)

def save_tokens(tokens: Dict[str, Any]) -> None:
    _write_json(TOKENS_FILE, tokens)

def migrate_legacy_tokens_if_needed() -> None:
    """If tokens.json is empty but legacy YAML has tokens, migrate them once."""
    if os.path.exists(TOKENS_FILE) and os.path.getsize(TOKENS_FILE) > 0:
        return
        
    legacy = _read_yaml(CONFIG_YAML)
    s = legacy.get("strava") if isinstance(legacy, dict) else {}
    if not isinstance(s, dict):
        return

    if s.get("access_token") or s.get("refresh_token"):
        save_tokens({
            "access_token": s.get("access_token", ""),
            "refresh_token": s.get("refresh_token", ""),
            "expires_at": int(s.get("expires_at", 0)),
        })
        print("Migrated legacy tokens from config.yaml to tokens.json")