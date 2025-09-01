#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A lightweight web application for cleaning and visualising Strava GPX files.

This script implements a minimal HTTP server that provides the following
features without requiring any external web frameworks:

* Login page – users must authenticate with a username and password before
  accessing the application.  Credentials are read from environment
  variables ``STRAVA_APP_USERNAME`` and ``STRAVA_APP_PASSWORD`` (falling
  back to ``admin``/``password`` if unset).  Logged‑in state is
  maintained via an HTTP cookie.

* File upload – once logged in, users can upload one or more GPX files
  exported from Strava.  Each uploaded file is parsed to extract
  timestamped latitude/longitude points and elevation.  The server then
  computes distances and speeds between consecutive points, removes
  stationary periods (user configurable threshold) and stores the
  resulting series in memory for later visualisation.

* Data filtering – after files are processed, the main page presents a
  simple interface allowing the user to select a particular activity
  (GPX file) and a date range.  The server filters the stored data
  accordingly and generates interactive charts on the fly using Plotly.

* Visualisation – two charts are produced for each activity: (1) speed
  versus elapsed time (after stop removal) and (2) cumulative distance
  versus elapsed time.  Plotly's CDN is included so that the graphs
  render client–side with pan/zoom functionality.

The entire application relies only on Python’s standard library and
``plotly``, which is already installed in the environment.  There are no
external dependencies such as Flask or Django.  To run the server,
execute this script from the command line.  It will automatically
open your default web browser to the login page.  By default the
application listens on port 8000.
"""

import cgi
import datetime as _dt
import http.server
import json
import math
import os
import socketserver
import uuid
import webbrowser
from html import escape as _escape
from http import cookies
from urllib.parse import parse_qs, urlparse

import plotly.graph_objects as go
import plotly.offline as pyo

from typing import Optional, Dict

try:
    import yaml  # PyYAML is available in this environment
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Configuration

def _load_config() -> dict:
    """Load configuration from config.yaml if present.

    The configuration file allows you to centralise credentials and other
    parameters for both the web application and optional Strava API
    integration.  See ``config_example.yaml`` for the expected format.

    Returns a dictionary with keys ``app_username``, ``app_password`` and
    ``strava`` (sub‑keys ``client_id``, ``client_secret``, ``refresh_token``
    and ``access_token``).  If the file does not exist or cannot be
    parsed, environment variables and defaults are used instead.
    """
    config = {
        'app_username': os.environ.get('STRAVA_APP_USERNAME', 'admin'),
        'app_password': os.environ.get('STRAVA_APP_PASSWORD', 'password'),
        'strava': {
            'client_id': os.environ.get('STRAVA_CLIENT_ID', ''),
            'client_secret': os.environ.get('STRAVA_CLIENT_SECRET', ''),
            'refresh_token': os.environ.get('STRAVA_REFRESH_TOKEN', ''),
            'access_token': os.environ.get('STRAVA_ACCESS_TOKEN', ''),
        }
    }
    cfg_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    if yaml and os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r') as f:
                loaded = yaml.safe_load(f) or {}
            # Merge loaded config into defaults
            if isinstance(loaded, dict):
                config.update({k: v for k, v in loaded.items() if k != 'strava'})
                if 'strava' in loaded and isinstance(loaded['strava'], dict):
                    config['strava'].update(loaded['strava'])
        except Exception as exc:
            print(f"Warning: Failed to load config.yaml: {exc}")
    return config


_CONFIG: dict = _load_config()

# Authentication credentials.  These may be overridden by config.yaml or
# environment variables.  Do NOT commit real secrets to source control.
USERNAME = _CONFIG.get('app_username', 'admin')
PASSWORD = _CONFIG.get('app_password', 'password')

# Strava API credentials (unused in this example but loaded for future use).
STRAVA_CONFIG = _CONFIG.get('strava', {})

# Session cookie name.  You can change this if it collides with another
# cookie in your browser.
SESSION_COOKIE_NAME = "strava_session_id"

# Speed threshold in metres per second below which a segment is
# considered a stop.  Segments whose speed falls below this value will
# be removed when computing moving time and cumulative distance.  You
# may adjust this threshold to reflect your riding or running style.
STOP_SPEED_THRESHOLD = 1.0  # m/s (~3.6 km/h)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute the great‑circle distance between two points on Earth.

    Parameters
    ----------
    lat1, lon1, lat2, lon2 : float
        Coordinates in decimal degrees.

    Returns
    -------
    float
        Distance in metres between the two points.
    """
    # Convert decimal degrees to radians
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    # Haversine formula
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    # Earth radius (mean radius) in metres
    R = 6371000.0
    return R * c


def parse_gpx(file_content: bytes) -> dict:
    """Parse a GPX file into a dict containing time, distance and speed series.

    The parser reads latitude/longitude/time/elevation from the GPX
    trackpoints.  Distances between consecutive points are computed
    using the haversine formula.  Speeds (in m/s) are calculated as
    distance divided by elapsed time.  Any segments where the speed
    falls below ``STOP_SPEED_THRESHOLD`` are removed.  The remaining
    moving data are then used to compute cumulative distances and
    elapsed times.

    Parameters
    ----------
    file_content : bytes
        Raw GPX file content.

    Returns
    -------
    dict
        A dictionary with keys ``start_time`` (datetime), ``data`` (list of
        dicts with keys ``t_rel``, ``distance``, ``speed``) and
        ``raw_points`` (list of tuples of original data points).  ``t_rel``
        represents elapsed time in seconds since ``start_time``.
    """
    import xml.etree.ElementTree as ET

    # Parse the XML; GPX uses namespaces.  We'll define the default
    # namespace but ignore others as this file only needs position and time.
    try:
        root = ET.fromstring(file_content)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse GPX: {exc}")

    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    # Find all trackpoints
    trkpts = root.findall(".//gpx:trkpt", ns)
    if not trkpts:
        raise ValueError("No trackpoints found in GPX file.")

    raw_points = []
    for pt in trkpts:
        lat = float(pt.get("lat"))
        lon = float(pt.get("lon"))
        time_el = pt.find("gpx:time", ns)
        ele_el = pt.find("gpx:ele", ns)
        # Skip points lacking a timestamp
        if time_el is None or not time_el.text:
            continue
        # Parse ISO 8601 timestamps; GPX times are in UTC and end with 'Z'
        time_str = time_el.text.strip()
        # Remove trailing Z and parse as naive UTC
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(time_str)
        ele = float(ele_el.text) if (ele_el is not None and ele_el.text) else None
        raw_points.append((dt, lat, lon, ele))

    if len(raw_points) < 2:
        raise ValueError("Not enough valid points in GPX file.")

    # Sort points by time in case they are out of order
    raw_points.sort(key=lambda x: x[0])
    start_time = raw_points[0][0]

    # Build moving data, skipping stationary segments
    data = []
    cumulative_distance = 0.0
    last_valid_time = start_time
    last_valid_lat = raw_points[0][1]
    last_valid_lon = raw_points[0][2]
    for i in range(1, len(raw_points)):
        t_curr, lat, lon, _ele = raw_points[i]
        dt = (t_curr - last_valid_time).total_seconds()
        if dt <= 0:
            # Ignore non‑chronological or duplicate timestamps
            continue
        dist = _haversine(last_valid_lat, last_valid_lon, lat, lon)
        speed = dist / dt if dt > 0 else 0.0
        # If moving slower than threshold, treat as a stop and don't update
        if speed < STOP_SPEED_THRESHOLD:
            continue
        cumulative_distance += dist
        elapsed = (t_curr - start_time).total_seconds()
        data.append({
            "t_rel": elapsed,
            "distance": cumulative_distance,
            "speed": speed,
        })
        last_valid_time = t_curr
        last_valid_lat = lat
        last_valid_lon = lon

    return {
        "start_time": start_time,
        "data": data,
        "raw_points": raw_points,
    }


class StravaGPXServer(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP request handler for the Strava GPX app.

    This class extends ``SimpleHTTPRequestHandler`` to add routes for
    logging in, uploading GPX files, filtering data and displaying
    interactive charts.  Session management is performed via HTTP
    cookies.  All application state lives in the process memory; this
    server is not intended for multiple concurrent users or production
    deployments.
    """

    def _get_session(self):
        """Retrieve or create a session for the current request.

        Returns the session dict associated with the session cookie.
        If no valid session exists, returns ``None``.
        """
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        c = cookies.SimpleCookie(cookie_header)
        morsel = c.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        session_id = morsel.value
        return _SESSIONS.get(session_id)

    def _create_session(self):
        """Create a new session and return its ID and storage dict."""
        session_id = uuid.uuid4().hex
        _SESSIONS[session_id] = {"activities": []}
        return session_id, _SESSIONS[session_id]

    def _redirect(self, location: str) -> None:
        """Send a 302 redirect to the specified location."""
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests for the application routes."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # Handle public resources first
        if path == "/":
            # Root redirects to login or app depending on session state
            session = self._get_session()
            if session:
                self._redirect("/app")
            else:
                self._redirect("/login")
            return
        if path == "/login":
            self._handle_login_get()
            return
        if path == "/logout":
            self._handle_logout()
            return
        if path == "/app":
            self._handle_app_get(parsed.query)
            return
        # Fallback to static file serving for other files (e.g. CSS)
        super().do_GET()

    def do_POST(self) -> None:
        """Handle POST requests (login and file upload)."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/login":
            self._handle_login_post()
        elif path == "/upload":
            self._handle_upload()
        else:
            # Unsupported POST route
            self.send_error(404, "Not found")

    # ---------------------------------------------------------------------
    # Page handlers

    def _handle_login_get(self, error: str | None = None) -> None:
        """Serve the login page."""
        html = [
            "<html><head><title>Strava GPX Login</title>",
            "<style>body{font-family:Arial,Helvetica,sans-serif;background-color:#f4f4f4;}"\
            "form{max-width:300px;margin:auto;background:#fff;padding:20px;border-radius:8px;"\
            "box-shadow:0 2px 4px rgba(0,0,0,0.1);}input[type=text],input[type=password]{"\
            "width:100%;padding:8px;margin:8px 0;border:1px solid #ccc;border-radius:4px;}"\
            "input[type=submit]{background:#007acc;color:#fff;border:none;padding:10px;"\
            "border-radius:4px;cursor:pointer;}h2{text-align:center;color:#007acc;}"\
            "</style></head><body>",
            "<h2>Login to Strava GPX Viewer</h2>",
            f"<form method='post' action='/login'>",
            f"<input type='text' name='username' placeholder='Username' required>",
            f"<input type='password' name='password' placeholder='Password' required>",
            f"<input type='submit' value='Login'>",
            "</form>",
        ]
        if error:
            html.insert(-1, f"<p style='color:red;text-align:center;'>{_escape(error)}</p>")
        html.append("</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("".join(html).encode("utf-8"))

    def _handle_login_post(self) -> None:
        """Process login credentials and create session on success."""
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/x-www-form-urlencoded"):
            self.send_error(400, "Unsupported form encoding")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)
        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        if username == USERNAME and password == PASSWORD:
            session_id, session_store = self._create_session()
            # Set cookie and redirect to app
            self.send_response(302)
            self.send_header("Location", "/app")
            cookie = cookies.SimpleCookie()
            cookie[SESSION_COOKIE_NAME] = session_id
            # Cookie expires when browser closes
            cookie[SESSION_COOKIE_NAME]["path"] = "/"
            self.send_header("Set-Cookie", cookie.output(header=""))
            self.end_headers()
        else:
            # Invalid credentials
            self._handle_login_get(error="Invalid username or password.")

    def _handle_logout(self) -> None:
        """Invalidate the current session and redirect to login."""
        session = self._get_session()
        if session:
            # Remove session from global store
            cookie_header = self.headers.get("Cookie")
            if cookie_header:
                c = cookies.SimpleCookie(cookie_header)
                morsel = c.get(SESSION_COOKIE_NAME)
                if morsel:
                    session_id = morsel.value
                    _SESSIONS.pop(session_id, None)
        # Clear cookie by setting expiry in the past
        expired_cookie = cookies.SimpleCookie()
        expired_cookie[SESSION_COOKIE_NAME] = ""
        expired_cookie[SESSION_COOKIE_NAME]["path"] = "/"
        expired_cookie[SESSION_COOKIE_NAME]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        self.send_response(302)
        self.send_header("Set-Cookie", expired_cookie.output(header=""))
        self.send_header("Location", "/login")
        self.end_headers()

    def _handle_app_get(self, query: str) -> None:
        """Render the main application page with charts and upload form."""
        session = self._get_session()
        if not session:
            # Not logged in
            self._redirect("/login")
            return
        # Parse query parameters for filtering
        q = parse_qs(query)
        selected_idx = 0
        start_date = None
        end_date = None
        if "activity" in q:
            try:
                selected_idx = int(q["activity"][0])
            except (ValueError, IndexError):
                selected_idx = 0
        if "start" in q:
            try:
                start_date = _dt.datetime.fromisoformat(q["start"][0])
            except Exception:
                start_date = None
        if "end" in q:
            try:
                end_date = _dt.datetime.fromisoformat(q["end"][0])
            except Exception:
                end_date = None
        activities = session.get("activities", [])
        # Build HTML for activities drop‑down
        activity_options = []
        for idx, act in enumerate(activities):
            label = _escape(act["filename"])
            sel_attr = " selected" if idx == selected_idx else ""
            activity_options.append(f"<option value='{idx}'{sel_attr}>{label}</option>")
        # Determine chart HTML
        chart_html = ""
        if activities:
            try:
                act = activities[selected_idx]
            except IndexError:
                act = activities[0]
            chart_html = self._build_charts(act, start_date, end_date)
        # Determine date bounds for inputs
        min_date = max_date = ""
        if activities:
            all_dates = [act["parsed"]["start_time"].date() for act in activities]
            min_date = min(all_dates).isoformat()
            max_date = max(all_dates).isoformat()
        html_parts = [
            "<html><head><title>Strava GPX Viewer</title>",
            "<style>body{font-family:Arial,Helvetica,sans-serif;background-color:#fafafa;margin:0;}"\
            "header{background:#007acc;color:#fff;padding:10px 20px;}"\
            "h1{margin:0;font-size:1.5em;}"\
            ".container{max-width:1000px;margin:20px auto;padding:20px;background:#fff;"\
            "border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);}"\
            "label{display:block;margin-top:10px;}"\
            "select,input[type=date]{padding:6px;margin-top:4px;border:1px solid #ccc;border-radius:4px;}"\
            "input[type=submit]{background:#007acc;color:#fff;border:none;padding:10px 15px;border-radius:4px;"\
            "margin-top:10px;cursor:pointer;}"\
            "form.upload{margin-bottom:20px;}"\
            "table{width:100%;border-collapse:collapse;margin-top:20px;}"\
            "th,td{padding:8px;border-bottom:1px solid #eee;text-align:left;}"\
            "</style>",
            "<script src='https://cdn.plot.ly/plotly-2.18.2.min.js'></script>",
            "</head><body>",
            "<header><h1>Strava GPX Viewer</h1></header>",
            "<div class='container'>",
            "<h2>Upload GPX Files</h2>",
            "<form class='upload' method='post' action='/upload' enctype='multipart/form-data'>",
            "<input type='file' name='gpxfile' accept='.gpx' multiple required>",
            "<input type='submit' value='Upload'>",
            "</form>",
            "<h2>Visualisations</h2>",
        ]
        if not activities:
            html_parts.append("<p>No activities uploaded yet. Use the form above to upload your Strava GPX files.</p>")
        else:
            # Filter form
            html_parts.append(
                "<form method='get' action='/app'>"
                "<label for='activity'>Activity:</label>"
                f"<select name='activity' id='activity'>{''.join(activity_options)}</select>"
            )
            # Date inputs
            html_parts.append(
                f"<label for='start'>Start date (optional):</label>"
                f"<input type='date' id='start' name='start' min='{min_date}' max='{max_date}' value='{start_date.date().isoformat() if start_date else ''}'>"
            )
            html_parts.append(
                f"<label for='end'>End date (optional):</label>"
                f"<input type='date' id='end' name='end' min='{min_date}' max='{max_date}' value='{end_date.date().isoformat() if end_date else ''}'>"
            )
            html_parts.append("<input type='submit' value='Filter'>")
            html_parts.append("</form>")
            # Insert chart HTML
            html_parts.append(chart_html)
        html_parts.append("<p><a href='/logout'>Log out</a></p>")
        html_parts.append("</div></body></html>")
        body = "".join(html_parts)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _handle_upload(self) -> None:
        """Handle GPX file upload via multipart/form-data."""
        session = self._get_session()
        if not session:
            self._redirect("/login")
            return
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            self.send_error(400, "Expected multipart/form-data")
            return
        # Parse multipart form
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                'REQUEST_METHOD': 'POST',
                'CONTENT_TYPE': self.headers['Content-Type'],
            }
        )
        files = form['gpxfile']
        # Ensure files is iterable
        file_items = files if isinstance(files, list) else [files]
        added = 0
        for item in file_items:
            if not item.filename.lower().endswith('.gpx'):
                continue
            try:
                content = item.file.read()
                parsed = parse_gpx(content)
                session['activities'].append({
                    'filename': item.filename,
                    'parsed': parsed,
                })
                added += 1
            except Exception as exc:
                # On parse error, ignore this file and continue
                print(f"Error parsing {item.filename}: {exc}")
        # Redirect back to app page
        self.send_response(302)
        self.send_header("Location", "/app")
        self.end_headers()

    # ---------------------------------------------------------------------
    # Helper methods

    def _build_charts(self, act: dict, start_date: Optional[_dt.datetime], end_date: Optional[_dt.datetime]) -> str:
        """Generate HTML containing Plotly charts for an activity.

        Filters the activity data by the provided date range before
        constructing the charts.  Returns the concatenated HTML
        fragments for the charts.
        """
        parsed = act['parsed']
        data = parsed['data']
        # Filter by date range (dates are based on start_time of activity)
        activity_date = parsed['start_time']
        if start_date and activity_date.date() < start_date.date():
            return "<p>No data within selected range.</p>"
        if end_date and activity_date.date() > end_date.date():
            return "<p>No data within selected range.</p>"
        # Build series for plotting
        t_rel = [d['t_rel'] / 60.0 for d in data]  # minutes
        speed_kmh = [d['speed'] * 3.6 for d in data]  # convert m/s to km/h
        distance_km = [d['distance'] / 1000.0 for d in data]  # metres to km
        # Speed chart
        fig1 = go.Figure()
        fig1.add_trace(go.Scatter(x=t_rel, y=speed_kmh, mode='lines', name='Speed (km/h)', line=dict(color='blue')))
        fig1.update_layout(
            title=f"Speed vs Time (Activity: { _escape(act['filename']) })",
            xaxis_title="Elapsed time (minutes)",
            yaxis_title="Speed (km/h)",
            height=400,
            margin=dict(l=40, r=20, t=40, b=40),
        )
        # Distance chart
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=t_rel, y=distance_km, mode='lines', name='Distance (km)', line=dict(color='green')))
        fig2.update_layout(
            title=f"Distance vs Time (Activity: { _escape(act['filename']) })",
            xaxis_title="Elapsed time (minutes)",
            yaxis_title="Distance (km)",
            height=400,
            margin=dict(l=40, r=20, t=40, b=40),
        )
        # Generate divs without including plotly.js (already loaded via CDN)
        div_speed = pyo.plot(fig1, include_plotlyjs=False, output_type='div')
        div_distance = pyo.plot(fig2, include_plotlyjs=False, output_type='div')
        return div_speed + div_distance


# Global session storage.  In a more sophisticated application this would
# be persisted to disk or a database.  The keys are session IDs and
# values are dictionaries containing user state (activities, etc.).
_SESSIONS: dict[str, dict] = {}


def run_server(port: int = 8000) -> None:
    """Start the HTTP server and open the login page in a web browser."""
    handler = StravaGPXServer
    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"Serving Strava GPX viewer at http://localhost:{port}/")
        # Open default web browser to login page
        try:
            webbrowser.open(f"http://localhost:{port}/login")
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the Strava GPX viewer server.")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    args = parser.parse_args()
    run_server(args.port)