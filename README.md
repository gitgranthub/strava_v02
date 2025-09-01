# Strava GPX Viewer

This repository contains a lightweight web application for cleaning and
visualising Strava GPX files.  The app uses modern Python syntax
(3.10+) and Plotly for interactive charts.  You can upload your GPX
activities, automatically remove stops from the track, and explore
speed and distance charts with date/activity filters.

## Prerequisites

* **Python 3.10 or newer** – the code uses modern type hints such as
  the union operator (`str | None`) and type‑annotated dicts.  You
  can install a newer Python alongside your system Python using
  [pyenv](https://github.com/pyenv/pyenv) or Homebrew:

  ```bash
  # Using Homebrew on macOS
  brew install python@3.11

  # Or using pyenv
  brew install pyenv
  pyenv install 3.11.6
  pyenv global 3.11.6
  ```

* **Virtual environment** – although optional, using a venv isolates
  dependencies from your system Python.  The steps below use the
  built‑in `venv` module.

## Setup

1. **Clone or copy** this repository to your machine and change into the
   project directory.

2. **Create and activate a virtual environment**:

   ```bash
   python3.11 -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   pip install --upgrade pip
   ```

3. **Install dependencies** from `requirements.txt`:

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure credentials**.  Copy `config.yaml` to the project
   directory (if it isn’t already there) and edit the fields:

   ```yaml
   # login credentials for the web app
   app_username: your_username
   app_password: your_password

   # optional Strava API credentials (obtained at https://www.strava.com/settings/api)
   strava:
     client_id: "your-client-id"
     client_secret: "your-client-secret"
     refresh_token: "your-refresh-token"
     access_token: "your-access-token"
   ```

   * To obtain **Client ID** and **Client Secret**, log in to Strava,
     navigate to your *Settings* → *API* page and create an app.  The
     “My API Application” page lists your client ID, client secret,
     authorisation token and refresh token【676409081986224†L61-L74】.
   * Access and refresh tokens are obtained via Strava’s OAuth flow;
     follow Strava’s guides to authorise your app and exchange the
     authorisation code for tokens【676409081986224†L93-L114】.

5. **Run the application**:

   ```bash
   python strava_app.py --port 8000
   ```

   The script starts a local HTTP server and opens your default web
   browser to the login page.  Use the credentials from your `config.yaml`
   to log in.  Upload one or more GPX files exported from Strava and
   explore your activities via the interactive charts.

## Notes

* The app only processes GPX files.  Strava’s GPX exports include
  GPS coordinates, timestamps and accessory data such as heart rate
  and cadence if recorded【469391514516135†L90-L101】.  Power data is
  present only when you recorded it with a power meter【469391514516135†L90-L101】.
* Currently the Strava API integration is not implemented in the
  server, but the configuration placeholders allow you to add it later.

Enjoy analysing your rides and runs!