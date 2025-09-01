#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strava GPX Viewer – Material-inspired UI + Full Strava OAuth/Sync
Upload GPX or sync with Strava API, visualize activities, and view stats.

Key features:
- Login
- Upload GPX (stop removal < 1.0 m/s)
- Auto-refresh charts on dropdown change
- Distance shown in km (2 decimals), Avg speed in km/h (2 decimals), moving-only
- Strava panel (masked config, expiry), buttons:
    * Connect / Authorize with Strava  -> /oauth/start -> /oauth/callback
    * Refresh Strava Token             -> /strava/refresh  (rotates refresh token, saves to config.yaml)
    * Sync last 30 days                -> /strava/sync     (activities + GPS streams)
No external web framework; stdlib + plotly + (optional) PyYAML for config.
"""

import cgi
import datetime as _dt
import http.server
import json
import math
import os
import secrets
import socketserver
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from html import escape as _escape
from http import cookies
from urllib.parse import parse_qs, urlparse

import plotly.graph_objects as go
import plotly.offline as pyo

try:
    import yaml  # optional; used to read/write config.yaml
except ImportError:
    yaml = None

# ----------------------- App constants -----------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.yaml")
GLOBAL_PORT = 8000

SESSION_COOKIE = "strava_session"
STOP_SPEED_THRESHOLD = 1.0  # m/s (treat slower as "stopped" for moving stats)

# ----------------------- Config load/save -----------------------

def _load_config() -> dict:
    base = {
        "app_username": os.environ.get("STRAVA_APP_USERNAME", "admin"),
        "app_password": os.environ.get("STRAVA_APP_PASSWORD", "password"),
        "strava": {
            "client_id": os.environ.get("STRAVA_CLIENT_ID", ""),
            "client_secret": os.environ.get("STRAVA_CLIENT_SECRET", ""),
            "refresh_token": os.environ.get("STRAVA_REFRESH_TOKEN", ""),
            "access_token": os.environ.get("STRAVA_ACCESS_TOKEN", ""),
            "expires_at": int(os.environ.get("STRAVA_EXPIRES_AT", "0") or 0),
        },
    }
    if os.path.exists(CONFIG_PATH):
        try:
            if yaml:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                # permissive fallback: attempt JSON
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.loads(f.read())
            if isinstance(cfg, dict):
                base.update({k: v for k, v in cfg.items() if k != "strava"})
                if "strava" in cfg and isinstance(cfg["strava"], dict):
                    base["strava"].update(cfg["strava"])
        except Exception as exc:
            print(f"Warning: failed to read config.yaml: {exc}")
    return base

def _save_config(cfg: dict) -> None:
    out = dict(cfg)
    try:
        if yaml:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(out, f, sort_keys=False)
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps(out, indent=2))
    except Exception as exc:
        print(f"Warning: failed to save config.yaml: {exc}")

_CONFIG = _load_config()
USERNAME = _CONFIG.get("app_username", "admin")
PASSWORD = _CONFIG.get("app_password", "password")
STRAVA_CFG = _CONFIG.get("strava", {})

# Token cache for this run
_TOKEN_CACHE = {
    "access_token": STRAVA_CFG.get("access_token", ""),
    "expires_at": int(STRAVA_CFG.get("expires_at", 0) or 0),
}

# ----------------------- Utility helpers -----------------------

def _mask(s: str, tail: int = 4) -> str:
    s = str(s or "")
    if not s:
        return "(empty)"
    return ("*" * max(0, len(s) - tail)) + s[-tail:]

def _human_expiry(expires_at: int) -> str:
    if not expires_at:
        return "unknown"
    now = int(time.time())
    delta = expires_at - now
    if delta <= 0:
        return f"expired {abs(delta)}s ago"
    mins = delta // 60
    if mins < 180:
        return f"in {mins} min"
    return _dt.datetime.utcfromtimestamp(expires_at).isoformat() + "Z"

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two lat/lon points."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def parse_gpx(file_content: bytes) -> dict:
    """Parse GPX and compute moving-only series (stop threshold).
    Returns:
      {
        "start_time": datetime | None,
        "data": [ { "t_rel": <moving seconds>, "distance": <m>, "speed": <m/s> }, ... ],
        "raw_points": [ (datetime, lat, lon), ... ]
      }
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(file_content)
    except ET.ParseError as exc:
        # Keep the shape consistent even on parse error
        raise ValueError(f"Failed to parse GPX: {exc}")

    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    pts = root.findall(".//gpx:trkpt", ns)

    raw: list[tuple[_dt.datetime, float, float]] = []
    for pt in pts:
        lat_s, lon_s = pt.get("lat"), pt.get("lon")
        time_el = pt.find("gpx:time", ns)
        if not lat_s or not lon_s or time_el is None or not (time_el.text and time_el.text.strip()):
            continue
        lat, lon = float(lat_s), float(lon_s)
        t = time_el.text.strip()
        # Normalize 'Z' to ISO with timezone
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        try:
            ts = _dt.datetime.fromisoformat(t)
        except Exception:
            continue
        raw.append((ts, lat, lon))

    if len(raw) < 2:
        return {"start_time": None, "data": [], "raw_points": raw}

    raw.sort(key=lambda x: x[0])
    start_time = raw[0][0]

    # --- Moving-time accumulation ---
    data: list[dict] = []
    moving_elapsed = 0.0  # seconds of moving time only
    cumulative_distance = 0.0

    last_time = raw[0][0]
    last_lat = raw[0][1]
    last_lon = raw[0][2]

    for t_curr, lat, lon in raw[1:]:
        dt_s = (t_curr - last_time).total_seconds()
        if dt_s <= 0:
            # Always advance the cursor
            last_time, last_lat, last_lon = t_curr, lat, lon
            continue

        dist = _haversine(last_lat, last_lon, lat, lon)
        speed = dist / dt_s

        # Advance the cursor no matter what
        last_time, last_lat, last_lon = t_curr, lat, lon

        # Only accumulate when above threshold (i.e., moving)
        if speed >= STOP_SPEED_THRESHOLD:
            moving_elapsed += dt_s
            cumulative_distance += dist
            data.append({
                "t_rel": moving_elapsed,        # moving time seconds
                "distance": cumulative_distance, # metres
                "speed": speed                   # m/s for this interval
            })

    return {"start_time": start_time, "data": data, "raw_points": raw}


# ----------------------- Strava HTTP / OAuth helpers -----------------------

def _http_json(method: str, url: str, headers: dict | None = None, payload: dict | None = None, form: bool = False) -> dict:
    """Small helper to call HTTP endpoints and parse JSON with error surfacing."""
    import urllib.error
    data = None
    headers = headers or {}
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} {url} :: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error {url} :: {e}") from e

def _strava_authorize_url(client_id: str, redirect_uri: str, scopes: str, state: str) -> str:
    params = {
        "client_id": str(int(str(client_id).strip())),  # ensure digits-only
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "approval_prompt": "auto",
        "scope": scopes,
        "state": state,
    }
    return "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode(params)

def _exchange_code_for_tokens(code: str) -> dict:
    cid = STRAVA_CFG.get("client_id") or ""
    csec = STRAVA_CFG.get("client_secret") or ""
    if not cid or not str(cid).strip().isdigit():
        raise RuntimeError(f"Client ID looks invalid: '{cid}'")
    if not csec:
        raise RuntimeError("Missing client_secret")
    url = "https://www.strava.com/oauth/token"
    payload = {"client_id": str(int(str(cid).strip())), "client_secret": csec, "code": code, "grant_type": "authorization_code"}
    return _http_json("POST", url, payload=payload, form=True)

def _refresh_access_token() -> str:
    cid = STRAVA_CFG.get("client_id") or ""
    csec = STRAVA_CFG.get("client_secret") or ""
    rtok = STRAVA_CFG.get("refresh_token") or ""
    if not cid or not str(cid).strip().isdigit():
        raise RuntimeError(f"Client ID looks invalid: '{cid}'")
    if not csec or not rtok:
        raise RuntimeError("Missing client_secret and/or refresh_token")
    url = "https://www.strava.com/oauth/token"
    payload = {"client_id": str(int(str(cid).strip())), "client_secret": csec, "grant_type": "refresh_token", "refresh_token": rtok}
    out = _http_json("POST", url, payload=payload, form=True)
    _TOKEN_CACHE["access_token"] = out.get("access_token", "")
    _TOKEN_CACHE["expires_at"] = int(out.get("expires_at", 0) or 0)
    STRAVA_CFG["access_token"] = _TOKEN_CACHE["access_token"]
    STRAVA_CFG["expires_at"] = _TOKEN_CACHE["expires_at"]
    # rotate refresh token if provided
    new_rt = out.get("refresh_token")
    if new_rt:
        STRAVA_CFG["refresh_token"] = new_rt
    _CONFIG["strava"] = STRAVA_CFG
    _save_config(_CONFIG)
    return _TOKEN_CACHE["access_token"]

def _access_token() -> str:
    now = int(time.time())
    tok = _TOKEN_CACHE.get("access_token") or STRAVA_CFG.get("access_token", "")
    exp = int(_TOKEN_CACHE.get("expires_at") or STRAVA_CFG.get("expires_at") or 0)
    if not tok or now >= (exp - 60):
        tok = _refresh_access_token()
    return tok

def _strava_get(path: str, params: dict | None = None) -> dict | list:
    base = "https://www.strava.com/api/v3"
    q = "?" + urllib.parse.urlencode(params or {})
    url = base + path + q
    headers = {"Authorization": f"Bearer {_access_token()}"}
    try:
        return _http_json("GET", url, headers=headers)
    except RuntimeError as e:
        if "HTTP 401" in str(e):
            headers = {"Authorization": f"Bearer {_refresh_access_token()}"}
            return _http_json("GET", url, headers=headers)
        raise

def _fetch_activities(per_page: int = 50, after_epoch: int | None = None) -> list[dict]:
    params = {"per_page": per_page, "page": 1}
    if after_epoch:
        params["after"] = after_epoch
    out = _strava_get("/athlete/activities", params)
    return out if isinstance(out, list) else []

def _fetch_streams(activity_id: int) -> dict:
    params = {"keys": "time,latlng,altitude", "key_by_type": "true"}
    out = _strava_get(f"/activities/{activity_id}/streams", params)
    if isinstance(out, list):
        d = {}
        for s in out:
            d[s.get("type")] = s
        return d
    return out if isinstance(out, dict) else {}

def _streams_to_parsed(streams: dict, start_dt: _dt.datetime) -> dict:
    """Convert Strava streams to moving-time series.
    Args:
      streams: dict with keys "time", "latlng", etc. (when key_by_type=true)
      start_dt: datetime of activity start (UTC or local ISO already handled upstream)
    Returns:
      {
        "start_time": datetime,
        "data": [ { "t_rel": <moving seconds>, "distance": <m>, "speed": <m/s> }, ... ],
        "raw_points": [ (datetime, lat, lon), ... ]
      }
    """
    time_s = (streams.get("time", {}) or {}).get("data") or []
    latlng = (streams.get("latlng", {}) or {}).get("data") or []

    if not time_s or not latlng or len(time_s) != len(latlng):
        return {"start_time": start_dt, "data": [], "raw_points": []}

    # Build raw timeline of absolute points
    raw: list[tuple[_dt.datetime, float, float]] = []
    for t_sec, pair in zip(time_s, latlng):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        lat, lon = float(pair[0]), float(pair[1])
        try:
            pt_time = start_dt + _dt.timedelta(seconds=float(t_sec))
        except Exception:
            continue
        raw.append((pt_time, lat, lon))

    if len(raw) < 2:
        return {"start_time": start_dt, "data": [], "raw_points": raw}

    raw.sort(key=lambda x: x[0])
    start_time = raw[0][0]

    # --- Moving-time accumulation ---
    data: list[dict] = []
    moving_elapsed = 0.0  # seconds of moving time only
    cumulative_distance = 0.0

    last_time = raw[0][0]
    last_lat = raw[0][1]
    last_lon = raw[0][2]

    for t_curr, lat, lon in raw[1:]:
        dt_s = (t_curr - last_time).total_seconds()
        if dt_s <= 0:
            # Always advance the cursor
            last_time, last_lat, last_lon = t_curr, lat, lon
            continue

        dist = _haversine(last_lat, last_lon, lat, lon)
        speed = dist / dt_s

        # Advance the cursor no matter what
        last_time, last_lat, last_lon = t_curr, lat, lon

        # Only accumulate when above threshold (i.e., moving)
        if speed >= STOP_SPEED_THRESHOLD:
            moving_elapsed += dt_s
            cumulative_distance += dist
            data.append({
                "t_rel": moving_elapsed,         # moving time seconds
                "distance": cumulative_distance,  # metres
                "speed": speed                    # m/s for this interval
            })

    return {"start_time": start_time, "data": data, "raw_points": raw}


# ----------------------- HTTP server -----------------------

SESSIONS: dict[str, dict] = {}

class StravaApp(http.server.SimpleHTTPRequestHandler):
    # ------ session helpers ------
    def _sess(self):
        ck = self.headers.get("Cookie")
        if not ck:
            return None
        c = cookies.SimpleCookie(ck)
        sid = c.get(SESSION_COOKIE)
        return SESSIONS.get(sid.value) if sid else None

    def _create_sess(self):
        sid = uuid.uuid4().hex
        SESSIONS[sid] = {"activities": [], "strava_ids": set(), "flash": "", "oauth_state": ""}
        return sid, SESSIONS[sid]

    def _redirect(self, loc: str):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    def _html(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    # ------ routing ------
    def do_GET(self):
        p = urlparse(self.path)
        path = p.path.rstrip("/") or "/"
        if path == "/":
            self._redirect("/app" if self._sess() else "/login"); return
        if path == "/login":
            self._login_get(); return
        if path == "/logout":
            self._logout(); return
        if path == "/app":
            self._app_get(parse_qs(p.query)); return
        if path == "/oauth/start":
            self._oauth_start(); return
        if path == "/oauth/callback":
            self._oauth_callback(); return
        if path == "/strava/refresh":
            self._strava_refresh(); return
        if path == "/strava/sync":
            self._strava_sync(); return
        super().do_GET()

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path.rstrip("/") or "/"
        if path == "/login":
            self._login_post(); return
        if path == "/upload":
            self._upload(); return
        self.send_error(404, "Not found")

    # ------ pages ------
    def _login_get(self, err: str | None = None):
        body = f"""
        <html><head><title>Login</title><style>
        body{{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f0f2f5;margin:0}}
        .card{{max-width:360px;margin:80px auto;background:white;border-radius:14px;padding:24px;
              box-shadow:0 8px 24px rgba(0,0,0,.08)}}
        input{{width:100%;padding:12px 10px;margin:8px 0;border:1px solid #d0d7de;border-radius:8px}}
        button{{width:100%;padding:12px;background:#007aff;color:white;border:none;border-radius:8px;cursor:pointer}}
        button:hover{{background:#0063d1}}
        .err{{color:#b00020;text-align:center;margin-top:8px}}
        h2{{margin:0 0 10px 0}}
        </style></head><body>
          <div class="card">
            <h2>Sign in</h2>
            <form method="post" action="/login">
              <input name="username" placeholder="Username" required>
              <input type="password" name="password" placeholder="Password" required>
              <button>Login</button>
            </form>
            {f"<div class='err'>{_escape(err)}</div>" if err else ""}
          </div>
        </body></html>
        """
        self._html(200, body)

    def _login_post(self):
        if not self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            self.send_error(400, "Unsupported form encoding"); return
        length = int(self.headers.get("Content-Length", 0))
        params = parse_qs(self.rfile.read(length).decode("utf-8"))
        if params.get("username", [""])[0] == USERNAME and params.get("password", [""])[0] == PASSWORD:
            sid, _ = self._create_sess()
            ck = cookies.SimpleCookie()
            ck[SESSION_COOKIE] = sid
            ck[SESSION_COOKIE]["path"] = "/"
            self.send_response(302)
            self.send_header("Location", "/app")
            self.send_header("Set-Cookie", ck.output(header=""))
            self.end_headers()
        else:
            self._login_get("Invalid username or password.")

    def _logout(self):
        # best-effort clear
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def _app_get(self, q: dict):
        s = self._sess()
        if not s:
            self._redirect("/login"); return

        act_idx = 0
        try:
            if "activity" in q:
                act_idx = int(q["activity"][0])
        except Exception:
            act_idx = 0

        acts = s.get("activities", [])
        opts = "".join(
            f"<option value='{i}'{' selected' if i==act_idx else ''}>{_escape(a['filename'])}</option>"
            for i, a in enumerate(acts)
        )
        # auto-submit when selecting an activity
        onchange = "this.form.submit()"

        chart_html = ""
        stats_html = ""
        if acts:
            act = acts[act_idx] if 0 <= act_idx < len(acts) else acts[0]
            chart_html = self._charts(act)
            stats_html = self._stats(act)

        flash = s.get("flash") or ""
        s["flash"] = ""

        # Build Strava panel
        env_overrides = any(os.environ.get(k) for k in (
            "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN", "STRAVA_ACCESS_TOKEN", "STRAVA_EXPIRES_AT"
        ))
        cid = STRAVA_CFG.get("client_id", "")
        rt = STRAVA_CFG.get("refresh_token", "")
        at = _TOKEN_CACHE.get("access_token") or STRAVA_CFG.get("access_token", "")
        exp = int(_TOKEN_CACHE.get("expires_at") or STRAVA_CFG.get("expires_at") or 0)
        env_note = "<li><strong>Env overrides (STRAVA_*) are active.</strong></li>" if env_overrides else ""

        cfg_panel = f"""
        <div class="card">
          <div class="card-header">
            <h3>Strava Connection</h3>
            <div class="btn-row">
              <form method="get" action="/oauth/start"><button class="btn primary">Connect / Authorize</button></form>
              <form method="get" action="/strava/refresh"><button class="btn">Refresh Token</button></form>
              <form method="get" action="/strava/sync"><button class="btn success">Sync last 30 days</button></form>
            </div>
          </div>
          <ul class="cfg-list">
            <li>Client ID: {_escape(_mask(cid, 3))}</li>
            <li>Refresh token: {_escape(_mask(rt, 6))}</li>
            <li>Access token: {"present" if at else "(none)"}</li>
            <li>Access token expiry: {_escape(_human_expiry(exp))}</li>
            {env_note}
          </ul>
        </div>
        """

        # Main HTML
        body = f"""
        <html><head><title>Strava GPX Viewer</title>
        <script src="https://cdn.plot.ly/plotly-2.18.2.min.js"></script>
        <style>
          :root {{
            --bg:#f4f6f9; --card:#ffffff; --text:#172b4d; --muted:#6b7280;
            --primary:#007aff; --primary-dark:#0063d1; --success:#00b894; --success-dark:#00a07f;
            --border:#e5e7eb; --shadow:0 8px 24px rgba(0,0,0,.08);
          }}
          * {{ box-sizing: border-box; }}
          body {{ font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--text); margin:0; }}
          header {{ background:var(--primary); color:#fff; padding:16px 20px; font-size:18px; text-align:center; }}
          .container {{ max-width:1200px; margin:20px auto; padding:0 16px; }}
          .card {{ background:var(--card); border-radius:14px; box-shadow:var(--shadow); margin-bottom:20px; }}
          .card-header {{ display:flex; align-items:center; justify-content:space-between; padding:16px 16px 0 16px; }}
          .card h3 {{ margin:0; font-size:18px; }}
          .card .content {{ padding:16px; }}
          .cfg-list {{ margin:12px 16px 16px 32px; color:var(--muted); }}
          .btn-row {{ display:flex; gap:10px; }}
          .btn {{ background:#e9eefb; color:#173b7a; border:1px solid #cbd5e1; padding:10px 12px; border-radius:10px; cursor:pointer; }}
          .btn:hover {{ filter:brightness(0.98); }}
          .btn.primary {{ background:var(--primary); color:#fff; border:none; }}
          .btn.primary:hover {{ background:var(--primary-dark); }}
          .btn.success {{ background:var(--success); color:#fff; border:none; }}
          .btn.success:hover {{ background:var(--success-dark); }}
          .flash {{ margin:0 0 16px 0; padding:12px 14px; border-radius:10px; }}
          .flash.ok {{ background:#e6f4ea; color:#0f5132; border:1px solid #badbcc; }}
          .flash.err {{ background:#fde8e8; color:#842029; border:1px solid #f5c2c7; }}
          form.inline {{ display:flex; gap:10px; align-items:center; }}
          select, input[type=file] {{ padding:10px; border-radius:10px; border:1px solid var(--border); }}
          .upload-actions button {{ padding:10px 12px; border-radius:10px; background:var(--primary); color:#fff; border:none; cursor:pointer; }}
          .upload-actions button:hover {{ background:var(--primary-dark); }}
          table {{ width:100%; border-collapse:collapse; margin-top:14px; }}
          th, td {{ padding:10px; border-bottom:1px solid var(--border); text-align:left; }}
        </style>
        </head><body>
          <header>🚴‍♀️ Strava GPX Viewer</header>
          <div class="container">
            {"<div class='flash ok'>" + _escape(flash) + "</div>" if flash.startswith("OK:") else ( "<div class='flash err'>" + _escape(flash) + "</div>" if flash else "" )}
            {cfg_panel}
            <div class="card">
              <div class="card-header"><h3>Upload GPX</h3></div>
              <div class="content">
                <form class="inline" method="post" action="/upload" enctype="multipart/form-data">
                  <input type="file" name="gpxfile" accept=".gpx" multiple>
                  <div class="upload-actions"><button>Upload</button></div>
                </form>
              </div>
            </div>
            <div class="card">
              <div class="card-header"><h3>Visualisations</h3></div>
              <div class="content">
                <form method="get" action="/app" class="inline">
                  <label for="activity">Activity:</label>
                  <select id="activity" name="activity" onchange="{onchange}">{opts}</select>
                </form>
                {chart_html}
                {stats_html}
              </div>
            </div>
            <p><a href="/logout">Log out</a></p>
          </div>
        </body></html>
        """
        self._html(200, body)

    # charts & stats
    def _charts(self, act: dict) -> str:
        d = act["parsed"].get("data", [])
        if not d:
            return "<p>No data to plot.</p>"
        # x in minutes, y1 speed km/h, y2 km
        t_min = [pt["t_rel"] / 60.0 for pt in d]
        speed_kmh = [pt["speed"] * 3.6 for pt in d]
        dist_km = [pt["distance"] / 1000.0 for pt in d]

        fig1 = go.Figure(go.Scatter(x=t_min, y=speed_kmh, mode="lines", name="Speed (km/h)", line=dict(color="#007aff")))
        fig1.update_layout(
            template="simple_white",
            title="Speed vs Time",
            xaxis_title="Elapsed time (minutes)",
            yaxis_title="Speed (km/h)",
            height=380,
            margin=dict(l=40, r=20, t=40, b=40),
            hovermode="x unified",
        )
        fig1.update_yaxes(tickformat=".1f")

        fig2 = go.Figure(go.Scatter(x=t_min, y=dist_km, mode="lines", name="Distance (km)", line=dict(color="#00b894")))
        fig2.update_layout(
            template="simple_white",
            title="Distance vs Time",
            xaxis_title="Elapsed time (minutes)",
            yaxis_title="Distance (km)",
            height=380,
            margin=dict(l=40, r=20, t=40, b=40),
            hovermode="x unified",
        )
        fig2.update_yaxes(tickformat=".2f")

        return pyo.plot(fig1, include_plotlyjs=False, output_type="div") + pyo.plot(fig2, include_plotlyjs=False, output_type="div")

    def _stats(self, act: dict) -> str:
        d = act["parsed"].get("data", [])
        if not d:
            return ""
        dist_km = d[-1]["distance"] / 1000.0
        moving_sec = d[-1]["t_rel"]
        avg_kmh = (dist_km / (moving_sec / 3600.0)) if moving_sec > 0 else 0.0
        return (
            "<table>"
            "<tr><th>Date</th><th>Name</th><th>Distance (km)</th><th>Avg speed (km/h)</th></tr>"
            f"<tr><td>{act['parsed']['start_time'].date().isoformat()}</td>"
            f"<td>{_escape(act['filename'])}</td>"
            f"<td>{dist_km:.2f}</td><td>{avg_kmh:.2f}</td></tr>"
            "</table>"
        )

    # uploads
    def _upload(self):
        s = self._sess()
        if not s:
            self._redirect("/login"); return
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data"); return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
        files = form["gpxfile"]
        items = files if isinstance(files, list) else [files]
        added = 0
        for it in items:
            try:
                parsed = parse_gpx(it.file.read())
                if parsed.get("data"):
                    s["activities"].append({"filename": it.filename, "parsed": parsed})
                    added += 1
            except Exception as exc:
                print(f"Upload parse error {getattr(it, 'filename', '?')}: {exc}")
        s["flash"] = f"OK: Uploaded {added} file(s)." if added else "ERROR: No valid GPX files uploaded."
        self._redirect("/app")

    # Strava flows
    def _oauth_start(self):
        s = self._sess()
        if not s:
            self._redirect("/login"); return
        cid = STRAVA_CFG.get("client_id") or ""
        if not cid or not str(cid).strip().isdigit():
            s["flash"] = "ERROR: Missing/invalid client_id in config.yaml (or overridden by env)."
            self._redirect("/app"); return
        state = secrets.token_urlsafe(16)
        s["oauth_state"] = state
        redirect_uri = f"http://localhost:{GLOBAL_PORT}/oauth/callback"
        scopes = "read,activity:read_all,profile:read_all"
        self._redirect(_strava_authorize_url(cid, redirect_uri, scopes, state))

    def _oauth_callback(self):
        s = self._sess()
        if not s:
            self._redirect("/login"); return
        p = urlparse(self.path)
        q = parse_qs(p.query)
        if "error" in q:
            err_val = q.get("error", ["unknown"])[0]
            s["flash"] = "ERROR: OAuth denied — " + err_val
            self._redirect("/app"); return
        code = q.get("code", [None])[0]
        state = q.get("state", [None])[0]
        if not code or not state or state != s.get("oauth_state"):
            s["flash"] = "ERROR: OAuth state mismatch or missing code."
            self._redirect("/app"); return
        try:
            out = _exchange_code_for_tokens(code)
            access = out.get("access_token", "")
            expires_at = int(out.get("expires_at", 0) or 0)
            refresh = out.get("refresh_token", "")
            if not access or not refresh:
                raise RuntimeError(f"Missing tokens in response: {out}")

            _TOKEN_CACHE["access_token"] = access
            _TOKEN_CACHE["expires_at"] = expires_at
            STRAVA_CFG["access_token"] = access
            STRAVA_CFG["expires_at"] = expires_at
            STRAVA_CFG["refresh_token"] = refresh
            _CONFIG["strava"] = STRAVA_CFG
            _save_config(_CONFIG)

            s["flash"] = "OK: Connected to Strava. Tokens saved to config.yaml."
        except Exception as exc:
            s["flash"] = f"ERROR: OAuth exchange failed — {exc}"
        self._redirect("/app")

    def _strava_refresh(self):
        s = self._sess()
        if not s:
            self._redirect("/login"); return
        try:
            _refresh_access_token()
            s["flash"] = "OK: Token refreshed & saved to config.yaml."
        except Exception as exc:
            s["flash"] = f"ERROR: Token refresh failed — {exc}"
        self._redirect("/app")

    def _strava_sync(self):
        s = self._sess()
        if not s:
            self._redirect("/login"); return
        try:
            after = int((_dt.datetime.utcnow() - _dt.timedelta(days=30)).timestamp())
            lst = _fetch_activities(per_page=50, after_epoch=after)
            pulled = self._ingest_strava(s, lst)
            if pulled == 0:
                # fallback: latest page without 'after'
                lst2 = _fetch_activities(per_page=30, after_epoch=None)
                pulled = self._ingest_strava(s, lst2)
            s["flash"] = f"OK: Strava sync added {pulled} new activit{'y' if pulled==1 else 'ies'}." if pulled else "OK: No new Strava activities."
        except Exception as exc:
            s["flash"] = f"ERROR: Strava sync failed — {exc}"
        self._redirect("/app")

    def _ingest_strava(self, s: dict, activities: list[dict]) -> int:
        if not activities:
            return 0
        count = 0
        ids = s.setdefault("strava_ids", set())
        for a in activities:
            act_id = a.get("id")
            if not act_id or act_id in ids:
                continue
            # requires map (GPS)
            if not (a.get("map") and a["map"].get("id")):
                continue
            try:
                streams = _fetch_streams(int(act_id))
                start_iso = a.get("start_date") or a.get("start_date_local")
                start_dt = _dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else _dt.datetime.utcnow()
                parsed = _streams_to_parsed(streams, start_dt)
                if not parsed.get("data"):
                    continue
                label = f"{start_dt.date().isoformat()} - {a.get('name','Activity')} (#{act_id})"
                s["activities"].append({"filename": label, "parsed": parsed})
                ids.add(act_id)
                count += 1
            except Exception as exc:
                print(f"Strava ingest error for {act_id}: {exc}")
        return count

# ----------------------- Entrypoint -----------------------

def _open_browser(port: int) -> None:
    try:
        webbrowser.open(f"http://localhost:{port}/login")
    except Exception:
        pass

def run(port: int = 8000):
    global GLOBAL_PORT
    GLOBAL_PORT = port
    with socketserver.TCPServer(("", port), StravaApp) as httpd:
        print(f"Serving on http://localhost:{port}")
        _open_browser(port)
        httpd.serve_forever()

if __name__ == "__main__":
    run(8000)
