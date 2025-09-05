# data_processing.py

import math
import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple

import pandas as pd

# --- NEW: Unit Conversion Constants ---
METERS_TO_FEET = 3.28084
METERS_TO_MILES = 0.000621371
MPS_TO_MPH = 2.23694

# --- Utility ---
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the distance in meters between two lat/lon points."""
    R = 6371000.0  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# --- Strava Data Processing ---
def streams_to_df(streams: Dict[str, Any]) -> pd.DataFrame:
    """Converts the Strava streams dictionary into a pandas DataFrame."""
    if not streams:
        return pd.DataFrame()

    df = pd.DataFrame({k: s.get('data', []) for k, s in streams.items()})
    
    if 'time' not in df.columns or 'latlng' not in df.columns:
        return pd.DataFrame()

    df[['lat', 'lon']] = pd.DataFrame(df['latlng'].tolist(), index=df.index)
    df = df.drop(columns=['latlng'])

    df = df.rename(columns={'time': 't_rel_sec', 'velocity_smooth': 'speed_mps'})

    df['t_rel_min'] = df['t_rel_sec'] / 60.0
    
    return df

# --- GPX Data Processing ---
def parse_gpx_bytes(blob: bytes) -> pd.DataFrame:
    """Parses GPX file content into a DataFrame."""
    try:
        content = blob.decode('utf-8')
        root = ET.fromstring(content)
    except (ET.ParseError, UnicodeDecodeError):
        return pd.DataFrame()

    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    pts = root.findall(".//gpx:trkpt", ns)

    records: List[Dict[str, Any]] = []
    for pt in pts:
        time_el = pt.find("gpx:time", ns)
        if pt.get("lat") and pt.get("lon") and time_el is not None and time_el.text:
            records.append({
                "lat": float(pt.get("lat")),
                "lon": float(pt.get("lon")),
                "time_dt": datetime.fromisoformat(time_el.text.replace("Z", "+00:00"))
            })

    if len(records) < 2:
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values(by="time_dt").reset_index(drop=True)

    df['t_rel_sec'] = (df['time_dt'] - df['time_dt'].iloc[0]).dt.total_seconds()
    df['t_rel_min'] = df['t_rel_sec'] / 60.0
    
    distances = [0.0]
    for i in range(1, len(df)):
        dist = haversine_m(df.loc[i-1, 'lat'], df.loc[i-1, 'lon'], df.loc[i, 'lat'], df.loc[i, 'lon'])
        distances.append(dist)
    
    df['distance_segment'] = distances
    df['distance'] = df['distance_segment'].cumsum()

    time_diff = df['t_rel_sec'].diff().fillna(0)
    df['speed_mps'] = (df['distance_segment'] / time_diff).fillna(0)

    return df.drop(columns=['distance_segment'])

# --- Filtering ---
def apply_moving_filter(df: pd.DataFrame, min_speed_mps: float) -> pd.DataFrame:
    """
    Filters out stationary points and correctly recalculates moving time and distance.
    """
    if 'speed_mps' not in df.columns or df.empty:
        return df
    
    moving_df = df[df['speed_mps'] >= min_speed_mps].copy()
    
    if not moving_df.empty:
        time_diffs = moving_df['t_rel_sec'].diff().fillna(0)
        
        moving_df['moving_t_rel_sec'] = time_diffs.cumsum()
        moving_df['moving_t_rel_min'] = moving_df['moving_t_rel_sec'] / 60.0

        distances = [0.0]
        for i in range(1, len(moving_df)):
            prev = moving_df.iloc[i-1]
            curr = moving_df.iloc[i]
            dist = haversine_m(prev['lat'], prev['lon'], curr['lat'], curr['lon'])
            distances.append(dist)
        
        moving_df['moving_distance'] = pd.Series(distances, index=moving_df.index).cumsum()
        
        moving_df['t_rel_min'] = moving_df['moving_t_rel_min']
        moving_df['distance'] = moving_df['moving_distance']

    return moving_df