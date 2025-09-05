#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# app.py

import base64
import os
from datetime import datetime, timedelta, timezone, date
from io import StringIO
import webbrowser
import threading

import numpy as np
import pandas as pd
from dash import Dash, dcc, html, Input, Output, State, no_update, ctx
from flask import request, redirect, session

# --- Local Imports ---
import config_handler as cfg
import strava_api as strava
import data_processing as dp
import ui_components as ui

# -------------------------
# App Initialization
# -------------------------
app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server
server.secret_key = os.getenv("APP_SECRET_KEY", os.urandom(24).hex())

app.title = "Strava Dashboard"
app.head = [html.Link(rel='icon', href='/assets/favicon.svg', type='image/svg+xml')]

cfg.migrate_legacy_tokens_if_needed()

# -------------------------
# App Login Gate & OAuth Routes
# -------------------------
@server.before_request
def gate():
    app_config = cfg.load_config()
    if not app_config.get("APP_USERNAME"):
        return
    public_paths = ("/app-login", "/logout-app", "/_dash", "/assets")
    if request.path.startswith(public_paths) or session.get("logged_in"):
        return
    return redirect("/app-login")

@server.route("/app-login", methods=["GET", "POST"])
def app_login():
    if request.method == "POST":
        app_config = cfg.load_config()
        if (request.form.get("username") == app_config.get("APP_USERNAME") and
            request.form.get("password") == app_config.get("APP_PASSWORD")):
            session["logged_in"] = True
            return redirect("/")
        return redirect("/app-login?error=1")
    error_message = "<p class='error-message'>Invalid credentials. Please try again.</p>" if request.args.get("error") else ""
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login - Strava Dashboard</title>
        <link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
        <link rel="stylesheet" href="/assets/style.css">
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    </head>
    <body class="login-body">
        <div class="login-container">
            <h2>Dashboard Login</h2>
            <form method="post">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required>
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
                <button type="submit">Login</button>
                {error_message}
            </form>
        </div>
    </body>
    </html>
    """

@server.route("/logout-app")
def app_logout():
    session.clear()
    return redirect("/app-login")

@server.route("/login")
def login_redirect():
    return redirect(strava.get_authorization_url())

@server.route("/logout")
def logout_strava():
    cfg.save_tokens({})
    return redirect("/")

@server.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    if not code:
        return "OAuth failed: No code provided.", 400
    try:
        strava.exchange_code_for_tokens(code)
    except Exception as e:
        return f"OAuth failed: {e}", 500
    return redirect("/")

# -------------------------
# Dash App Layout
# -------------------------
app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dcc.Store(id="store-config", data=cfg.load_config()),
    dcc.Store(id='store-unit-system', storage_type='local', data='imperial'), # NEW
    dcc.Store(id='store-activity-list', storage_type='memory'),
    dcc.Store(id='store-selected-activity-id', storage_type='memory'),
    dcc.Store(id="store-activity-df", storage_type="memory"),

    html.Header([
        html.H1("Strava Dynamic Dashboard"),
        html.Div(id="oauth-status-container")
    ]),

    html.Main(className="container", children=[
        html.Div(className="grid-row", children=[
            html.Div(className="card", children=[
                html.H3("Configuration"),
                html.Label("Client ID"), dcc.Input(id="cfg-client-id", type="text", debounce=True),
                html.Label("Client Secret"), dcc.Input(id="cfg-client-secret", type="password", debounce=True),
                html.Label("Mapbox Token (Optional)"), dcc.Input(id="cfg-mapbox", type="password", debounce=True),
                html.Button("Save Config", id="btn-save-config", n_clicks=0),
                html.Span(id="cfg-save-msg", className="status-msg")
            ]),

            dcc.Loading(
                id="loading-activity-selection",
                type="default",
                children=html.Div(className="card activity-selection-card", children=[
                    html.H3("Activity Selection"),
                    dcc.Dropdown(id="dropdown-activity", placeholder="Syncing recent activities..."),
                    dcc.DatePickerRange(
                        id='date-picker-range',
                        min_date_allowed=date(2008, 1, 1),
                        max_date_allowed=date.today(),
                        initial_visible_month=date.today(),
                        start_date_placeholder_text="Start Date",
                        end_date_placeholder_text="End Date",
                        end_date=date.today()
                    ),
                    html.Div(className="action-button-group", children=[
                        html.Button("Search by Date", id="btn-search-dates"),
                        html.Button("Sync Last 30 Days", id="btn-sync-activities", className="button-secondary"),
                    ]),
                    dcc.Upload(id="uploader-gpx", children=html.Div(["Drag & Drop or ", html.A("Select GPX File")]), className="gpx-uploader"),
                    html.Span(id="activity-load-msg", className="status-msg")
                ])
            ),

            html.Div(className="card", children=[
                html.H3("Analysis Options"),
                # NEW: UNIT SYSTEM SWITCH
                html.Label("Unit System"),
                dcc.RadioItems(
                    id='unit-system-switch',
                    options=[
                        {'label': 'Imperial', 'value': 'imperial'},
                        {'label': 'Metric', 'value': 'metric'}
                    ],
                    value='imperial',
                    labelStyle={'display': 'inline-block', 'margin-right': '1rem'}
                ),
                html.Div(className="tooltip-container", children=[
                    dcc.Checklist(id="cfg-apply-filter", options=[{"label": "Exclude stationary time", "value": "apply"}], value=["apply"]),
                    html.Span("?", className="tooltip-icon"),
                    html.P("Removes all data points where your speed was below the 'Min moving speed' threshold.", className="tooltip-text")
                ]),
                # NEW: DYNAMIC LABEL
                html.Label(id='label-min-speed'),
                dcc.Input(id="cfg-min-speed", type="number", min=0, step=0.1, debounce=True),
                html.Label("Color route by:"),
                dcc.Dropdown(id="dd-color-by", options=[
                    {"label": "Speed", "value": "speed_mps"},
                    {"label": "Heart Rate", "value": "heartrate"},
                    {"label": "Cadence", "value": "cadence"},
                    {"label": "Elevation", "value": "altitude"},
                    {"label": "Power", "value": "watts"},
                ], value="speed_mps", clearable=False),
            ]),
        ]),

        html.Div(id="kpi-banner", className="grid-kpi", children=[
            html.Div(className="kpi-card", children=[html.H4("Ride Time"), html.P(id="kpi-ride-time", children="---")]),
            html.Div(className="kpi-card", children=[html.H4(id='kpi-distance-label'), html.P(id="kpi-distance", children="---")]),
            html.Div(className="kpi-card", children=[html.H4(id='kpi-avg-speed-label'), html.P(id="kpi-avg-speed", children="---")]),
            html.Div(className="kpi-card", children=[html.H4(id='kpi-max-speed-label'), html.P(id="kpi-max-speed", children="---")]),
            html.Div(className="kpi-card", children=[html.H4(id='kpi-elevation-label'), html.P(id="kpi-elevation", children="---")]),
            html.Div(className="kpi-card", children=[html.H4("Avg Heart Rate"), html.P(id="kpi-avg-hr", children="---")]),
            html.Div(className="kpi-card", children=[html.H4("Avg Cadence"), html.P(id="kpi-avg-cadence", children="---")]),
        ]),

        html.Div(className="card map-card", children=[dcc.Loading(dcc.Graph(id="fig-map", config={'scrollZoom': True}), type="dot")]),
        html.Div(className="grid-vitals", children=[
            dcc.Loading(dcc.Graph(id="fig-hr"), type="dot"),
            dcc.Loading(dcc.Graph(id="fig-cad"), type="dot"),
            dcc.Loading(dcc.Graph(id="fig-speed"), type="dot"),
            dcc.Loading(dcc.Graph(id="fig-elev"), type="dot"),
            dcc.Loading(dcc.Graph(id="fig-watts"), type="dot"),
            dcc.Loading(dcc.Graph(id="fig-dist"), type="dot"),
        ]),
    ])
])

# -------------------------
# Callbacks
# -------------------------

@app.callback(
    Output("store-config", "data"), Output("cfg-save-msg", "children"), Input("btn-save-config", "n_clicks"),
    State("cfg-client-id", "value"), State("cfg-client-secret", "value"), State("cfg-mapbox", "value"), prevent_initial_call=True)
def save_config_values(n_clicks, client_id, client_secret, mapbox_token):
    current_cfg = cfg.load_config()
    if client_secret and client_secret != "********":
        current_cfg["STRAVA_CLIENT_SECRET"] = client_secret
    if mapbox_token and mapbox_token != "********":
        current_cfg["MAPBOX_TOKEN"] = mapbox_token
    current_cfg["STRAVA_CLIENT_ID"] = client_id
    cfg.save_config(current_cfg)
    return current_cfg, "Saved!"

@app.callback(
    Output("cfg-client-id", "value"), Output("cfg-client-secret", "value"), Output("cfg-mapbox", "value"),
    Output("cfg-apply-filter", "value"), Input("store-config", "data"))
def hydrate_config_inputs(config_data):
    return (
        config_data.get("STRAVA_CLIENT_ID", ""),
        "********" if config_data.get("STRAVA_CLIENT_SECRET") else "",
        "********" if config_data.get("MAPBOX_TOKEN") else "",
        ["apply"] if config_data.get("APPLY_MOVING_FILTER", True) else [],
    )

@app.callback(
    Output("oauth-status-container", "children"),
    Input("store-config", "data"))
def update_oauth_status(_):
    tokens = cfg.load_tokens()
    if tokens.get("access_token"):
        try:
            athlete = strava.get_athlete()
            name = f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip()
            return html.Div([
                html.Span(f"Connected as {name}"),
                html.Div(className="tooltip-container header-tooltip", children=[
                    html.Button("Refresh Token", id="btn-refresh-token", className="button-secondary"),
                    html.Span("?", className="tooltip-icon"),
                    html.P("Manually requests a new access token from Strava.", className="tooltip-text")
                ]),
                html.A("Logout", href="/logout", className="button-logout")
            ])
        except Exception as e:
            print(f"Error fetching athlete: {e}")
            cfg.save_tokens({})
            return html.A("Connect with Strava", href="/login", className="button-login")
    return html.A("Connect with Strava", href="/login", className="button-login")

@app.callback(
    Output("activity-load-msg", "children", allow_duplicate=True),
    Input("btn-refresh-token", "n_clicks"),
    prevent_initial_call=True)
def handle_token_refresh(n_clicks):
    if not n_clicks:
        return no_update
    try:
        strava.force_refresh_tokens()
        return "Token refreshed successfully!"
    except Exception as e:
        return f"Refresh failed: {e}"

# --- NEW: CALLBACK TO MANAGE UNIT SYSTEM AND MIN SPEED INPUT ---
@app.callback(
    Output('store-unit-system', 'data'),
    Output('label-min-speed', 'children'),
    Output('cfg-min-speed', 'value'),
    Input('unit-system-switch', 'value'),
    State('cfg-min-speed', 'value'),
    State('store-unit-system', 'data'),
    State('store-config', 'data'))
def update_unit_system(new_system, current_speed_val, old_system, config_data):
    if new_system == 'imperial':
        label = "Min moving speed (mph)"
        if old_system == 'metric' and current_speed_val is not None:
            new_speed = round(current_speed_val * dp.MPS_TO_MPH, 1)
        else:
            base_metric_speed = config_data.get("MIN_MOVING_SPEED_MPS", cfg.DEFAULT_MIN_SPEED_MPS)
            new_speed = round(base_metric_speed * dp.MPS_TO_MPH, 1)
    else: # new_system is 'metric'
        label = "Min moving speed (m/s)"
        if old_system == 'imperial' and current_speed_val is not None:
            new_speed = round(current_speed_val / dp.MPS_TO_MPH, 1)
        else:
            new_speed = config_data.get("MIN_MOVING_SPEED_MPS", cfg.DEFAULT_MIN_SPEED_MPS)
            
    return new_system, label, new_speed

@app.callback(
    Output('store-activity-list', 'data'),
    Output("activity-load-msg", "children", allow_duplicate=True),
    Input('url', 'pathname'),
    Input("btn-sync-activities", "n_clicks"),
    Input("btn-search-dates", "n_clicks"),
    State("date-picker-range", "start_date"), State("date-picker-range", "end_date"),
    prevent_initial_call=True)
def sync_activities(_, n_sync, n_search, start_date_str, end_date_str):
    trigger_id = ctx.triggered_id
    try:
        if trigger_id == 'btn-search-dates':
            if not start_date_str or not end_date_str:
                return no_update, "Please select a start and end date."
            start_date = datetime.fromisoformat(start_date_str)
            end_date = datetime.fromisoformat(end_date_str) + timedelta(days=1)
            activities = strava.get_activities(after_epoch=int(start_date.timestamp()), before_epoch=int(end_date.timestamp()))
            start_formatted = start_date.strftime('%b %d, %Y')
            end_formatted = datetime.fromisoformat(end_date_str).strftime('%b %d, %Y')
            msg = f"Found {len(activities)} activities from {start_formatted} to {end_formatted}."
        else:
            after_date = datetime.now(timezone.utc) - timedelta(days=30)
            activities = strava.get_activities(after_epoch=int(after_date.timestamp()))
            msg = f"Synced {len(activities)} recent activities."
        if not activities:
            return [], "No activities found for the selected period."
        return activities, msg
    except Exception as e:
        return no_update, f"Sync failed: {e}"

@app.callback(
    Output('dropdown-activity', 'options'),
    Output('store-selected-activity-id', 'data'),
    Input('store-activity-list', 'data'),
    prevent_initial_call=True)
def update_dropdown_and_select_latest(activities):
    if not activities:
        return [], no_update
    options = [
        {"label": f"{a['name']} - {datetime.fromisoformat(a['start_date_local'].replace('Z', '')).strftime('%b %d, %Y')}", "value": a['id']}
        for a in activities
    ]
    latest_activity_id = activities[0]['id']
    return options, latest_activity_id

@app.callback(
    Output('dropdown-activity', 'value'),
    Input('store-selected-activity-id', 'data'))
def set_dropdown_value(activity_id):
    return activity_id

@app.callback(
    Output("store-activity-df", "data"),
    Output("activity-load-msg", "children"),
    Input("dropdown-activity", "value"),
    Input("uploader-gpx", "contents"),
    prevent_initial_call=True
)
def load_activity_data(selected_activity_id, gpx_contents):
    trigger_id = ctx.triggered_id
    if trigger_id == 'dropdown-activity' and selected_activity_id:
        try:
            streams = strava.get_activity_streams(selected_activity_id)
            df = dp.streams_to_df(streams)
            return df.to_json(orient="split"), f"Loaded activity #{selected_activity_id}"
        except Exception as e:
            return no_update, f"Error: {e}"
    if trigger_id == 'uploader-gpx' and gpx_contents:
        try:
            _, content_string = gpx_contents.split(',')
            decoded = base64.b64decode(content_string)
            df = dp.parse_gpx_bytes(decoded)
            if df.empty:
                return no_update, "GPX parsing failed."
            return df.to_json(orient="split"), "Loaded GPX file."
        except Exception as e:
            return no_update, f"Error loading GPX: {e}"
    return no_update, ""

@app.callback(
    Output("fig-map", "figure"), Output("fig-hr", "figure"), Output("fig-cad", "figure"), Output("fig-speed", "figure"),
    Output("fig-elev", "figure"), Output("fig-watts", "figure"), Output("fig-dist", "figure"),
    Output("kpi-ride-time", "children"), Output("kpi-distance", "children"), Output("kpi-avg-speed", "children"),
    Output("kpi-max-speed", "children"), Output("kpi-elevation", "children"), Output("kpi-avg-hr", "children"),
    Output("kpi-avg-cadence", "children"),
    Output('kpi-distance-label', 'children'), Output('kpi-avg-speed-label', 'children'),
    Output('kpi-max-speed-label', 'children'), Output('kpi-elevation-label', 'children'),
    Input("store-activity-df", "data"), Input("dd-color-by", "value"), Input("cfg-apply-filter", "value"),
    Input("cfg-min-speed", "value"), Input('store-unit-system', 'data'), State("store-config", "data"))
def update_all_figures_and_kpis(df_json, color_by, apply_filter_val, min_speed_display, unit_system, config_data):
    if not df_json:
        blank_fig = go.Figure()
        no_data_str = "---"
        no_data_labels = ("Total Distance", "Avg Speed", "Max Speed", "Elevation Gain")
        return (blank_fig,) * 7 + (no_data_str,) * 7 + no_data_labels

    unfiltered_df = pd.read_json(StringIO(df_json), orient="split")
    
    if min_speed_display is not None:
        min_speed_mps = min_speed_display / dp.MPS_TO_MPH if unit_system == 'imperial' else min_speed_display
    else:
        min_speed_mps = config_data.get("MIN_MOVING_SPEED_MPS", cfg.DEFAULT_MIN_SPEED_MPS)

    if 'apply' in (apply_filter_val or []):
        df = dp.apply_moving_filter(unfiltered_df, min_speed_mps)
    else:
        df = unfiltered_df

    if unit_system == 'imperial':
        dist_unit, speed_unit, elev_unit = "mi", "mph", "ft"
        dist_conv, speed_conv, elev_conv = dp.METERS_TO_MILES, dp.MPS_TO_MPH, dp.METERS_TO_FEET
        kpi_dist_label, kpi_avg_speed_label, kpi_max_speed_label, kpi_elev_label = "Total Distance (mi)", "Avg Speed (mph)", "Max Speed (mph)", "Elevation Gain (ft)"
    else:
        dist_unit, speed_unit, elev_unit = "km", "km/h", "m"
        dist_conv, speed_conv, elev_conv = 0.001, 3.6, 1
        kpi_dist_label, kpi_avg_speed_label, kpi_max_speed_label, kpi_elev_label = "Total Distance (km)", "Avg Speed (km/h)", "Max Speed (km/h)", "Elevation Gain (m)"

    if not df.empty:
        df['distance_display'] = df['distance'] * dist_conv
        df['speed_display'] = df['speed_mps'] * speed_conv
        df['altitude_display'] = df['altitude'] * elev_conv

    if 'moving_t_rel_sec' in df and not df.empty: total_seconds = df['moving_t_rel_sec'].max()
    elif 't_rel_sec' in df and not df.empty: total_seconds = df['t_rel_sec'].max()
    else: total_seconds = 0
    kpi_ride_time = str(timedelta(seconds=round(total_seconds))) if total_seconds > 0 else "---"

    kpi_dist = f"{df['distance_display'].max():.2f}" if 'distance_display' in df and not df.empty else "---"
    kpi_avg_speed = f"{df['speed_display'].mean():.1f}" if 'speed_display' in df and not df.empty else "---"
    kpi_max_speed = f"{df['speed_display'].max():.1f}" if 'speed_display' in df and not df.empty else "---"
    kpi_ele = f"{df['altitude_display'].diff().clip(lower=0).sum():.0f}" if 'altitude_display' in df and not df.empty else "---"
    kpi_avg_hr = f"{df['heartrate'].mean():.0f}" if 'heartrate' in df and not df.empty else "---"
    kpi_avg_cad = f"{df['cadence'].mean():.0f}" if 'cadence' in df and not df.empty else "---"

    map_fig = ui.build_map(df, color_by, config_data.get("MAPBOX_TOKEN", ""), unit_system, 'speed_display', 'altitude_display')
    hr_fig = ui.build_series_figure(df, "heartrate", "Heart Rate", "BPM")
    cad_fig = ui.build_series_figure(df, "cadence", "Cadence", "RPM")
    speed_fig = ui.build_series_figure(df, "speed_display", f"Speed ({speed_unit})", speed_unit.upper())
    elev_fig = ui.build_series_figure(df, "altitude_display", f"Elevation ({elev_unit})", elev_unit.upper())
    watts_fig = ui.build_series_figure(df, "watts", "Power", "W")
    dist_fig = ui.build_series_figure(df, "distance_display", f"Distance ({dist_unit})", dist_unit.upper())

    return (map_fig, hr_fig, cad_fig, speed_fig, elev_fig, watts_fig, dist_fig,
        kpi_ride_time, kpi_dist, kpi_avg_speed, kpi_max_speed, kpi_ele, kpi_avg_hr, kpi_avg_cad,
        kpi_dist_label, kpi_avg_speed_label, kpi_max_speed_label, kpi_elev_label)

# -------------------------
# Main Execution
# -------------------------
if __name__ == "__main__":
    import argparse, webbrowser, threading
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    url = f"http://{args.host}:{args.port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"🚀 Starting Strava Dashboard at {url}")
    app.run(host=args.host, port=args.port, debug=args.debug)