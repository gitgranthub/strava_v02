# ui_components.py

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# --- Theme Colors ---
COLOR_PRIMARY_TRACE = "#2563EB" # A nice blue for chart lines
COLOR_BACKGROUND = "#F8F9FA"
COLOR_GRID = "#E5E7EB"

def build_map(df: pd.DataFrame, color_by: str, mapbox_token: str) -> go.Figure:
    """Creates an interactive map figure from the activity data."""
    if df.empty or 'lat' not in df.columns or 'lon' not in df.columns:
        return go.Figure().update_layout(title="Map (No GPS Data)")

    hover_data = {
        "t_rel_min": ":.1f",
        "speed_mps": ":.1f",
        "lat": ":.4f",
        "lon": ":.4f",
    }
    if 'heartrate' in df.columns: hover_data['heartrate'] = True
    if 'cadence' in df.columns: hover_data['cadence'] = True

    color_by = color_by if color_by in df.columns else None
    
    map_style = "open-street-map"
    if mapbox_token:
        px.set_mapbox_access_token(mapbox_token)
        map_style = "mapbox://styles/mapbox/outdoors-v12"

    fig = px.scatter_mapbox(
        df,
        lat="lat",
        lon="lon",
        color=color_by,
        color_continuous_scale=px.colors.sequential.Turbo,
        hover_name=None,
        hover_data=hover_data,
        zoom=12,
    )
    fig.update_layout(
        mapbox_style=map_style,
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        height=550,
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        # This new section fixes the legend overlap
        coloraxis_colorbar=dict(
            yanchor="top", y=0.9, # Shift the colorbar down from the top
            xanchor="right", x=0.99,
            len=0.75 # Make it slightly shorter if needed
        )
    )
    return fig

def build_series_figure(df: pd.DataFrame, y_col: str, title: str, y_axis_title: str) -> go.Figure:
    """Creates a time-series line chart for a given metric with the new theme."""
    fig = go.Figure()
    
    if not df.empty and y_col in df.columns and pd.api.types.is_numeric_dtype(df[y_col]):
        fig.add_trace(go.Scatter(
            x=df["t_rel_min"],
            y=df[y_col],
            mode="lines",
            name=y_axis_title,
            line=dict(width=2, color=COLOR_PRIMARY_TRACE)
        ))
    
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis_title="Time (minutes)",
        yaxis_title=y_axis_title,
        height=320,
        margin=dict(l=50, r=20, t=50, b=40),
        paper_bgcolor='white',
        plot_bgcolor='white',
        font=dict(family="Inter, sans-serif", color="#374151"),
        xaxis=dict(gridcolor=COLOR_GRID),
        yaxis=dict(gridcolor=COLOR_GRID),
        hovermode="x unified",
    )
    return fig