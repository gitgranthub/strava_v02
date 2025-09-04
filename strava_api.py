# strava_api.py

import time
from typing import Dict, Any, Optional, List
import requests
from config_handler import save_tokens, load_tokens, load_config

# --- Constants ---
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_SCOPE = "read,activity:read_all,profile:read_all"

# --- OAuth and Token Handling ---
def get_authorization_url() -> str:
    cfg = load_config()
    params = {"client_id": cfg["STRAVA_CLIENT_ID"], "redirect_uri": cfg["STRAVA_REDIRECT_URI"],
        "response_type": "code", "approval_prompt": "auto", "scope": STRAVA_SCOPE}
    q = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"{STRAVA_AUTH_URL}?{q}"

def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    cfg = load_config()
    resp = requests.post(STRAVA_TOKEN_URL, data={"client_id": cfg["STRAVA_CLIENT_ID"],
        "client_secret": cfg["STRAVA_CLIENT_SECRET"], "code": code, "grant_type": "authorization_code"}, timeout=30)
    resp.raise_for_status()
    tokens = resp.json()
    save_tokens(tokens)
    return tokens

def _refresh_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    if not tokens.get("refresh_token"): raise Exception("No refresh token available.")
    resp = requests.post(STRAVA_TOKEN_URL, data={"client_id": cfg["STRAVA_CLIENT_ID"],
        "client_secret": cfg["STRAVA_CLIENT_SECRET"], "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"]}, timeout=30)
    resp.raise_for_status()
    new_tokens = resp.json()
    save_tokens(new_tokens)
    return new_tokens

def force_refresh_tokens() -> Dict[str, Any]:
    tokens = load_tokens()
    return _refresh_tokens(tokens)

def get_valid_tokens() -> Optional[Dict[str, Any]]:
    tokens = load_tokens()
    if not tokens.get("access_token"): return None
    if int(time.time()) > tokens.get("expires_at", 0) - 60:
        try: return _refresh_tokens(tokens)
        except requests.HTTPError as e:
            print(f"Failed to refresh tokens: {e}")
            save_tokens({})
            return None
    return tokens

# --- API Request Wrapper ---
def _api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    tokens = get_valid_tokens()
    if not tokens: raise Exception("Not authenticated with Strava.")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    url = f"{STRAVA_API_BASE}{path}"
    response = requests.get(url, headers=headers, params=params or {}, timeout=45)
    response.raise_for_status()
    return response.json()

# --- API Endpoints ---
def get_athlete() -> Dict[str, Any]:
    return _api_get("/athlete")

def get_activities(before_epoch: Optional[int] = None, after_epoch: Optional[int] = None, per_page: int = 100) -> List[Dict[str, Any]]:
    """Fetches activities within a given time range."""
    params = {"per_page": per_page}
    if before_epoch:
        params["before"] = before_epoch
    if after_epoch:
        params["after"] = after_epoch
    return _api_get("/athlete/activities", params=params)

def get_activity_streams(activity_id: str) -> Dict[str, Any]:
    params = {"keys": "latlng,time,heartrate,cadence,distance,altitude,watts,velocity_smooth", "key_by_type": "true"}
    return _api_get(f"/activities/{activity_id}/streams", params=params)