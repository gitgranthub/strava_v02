# Strava Dynamic Dashboard

This is a self-hosted web application for connecting to your Strava account to view and analyze your activities. The app uses the official Strava API to fetch your data, allowing you to visualize your routes on a map and explore interactive charts for heart rate, speed, elevation, power, and more.

A key feature is the ability to apply a "moving time" filter, which recalculates your stats to exclude periods when you were stationary (e.g., waiting at a stoplight), giving you a more accurate picture of your performance.

## Features

*   **Secure Strava Connection:** Uses the official OAuth2 flow to securely connect to your Strava account.
*   **Dynamic Activity Dashboard:** Select any of your recent activities from a dropdown to instantly load its data.
*   **Interactive Map:** View your activity route on a map, with options to color the path by speed, heart rate, and other metrics. (Supports Mapbox for enhanced visuals).
*   **Detailed Charts:** Analyze your performance with time-series charts for speed, heart rate, cadence, elevation, and power.
*   **Moving Time Filter:** A simple switch allows you to remove stops from the analysis for more accurate speed and distance metrics.
*   **GPX Upload:** Optionally, you can upload a local GPX file for analysis without connecting to Strava.

## Prerequisites

You will need **Python 3.10 or newer**. This project uses modern Python features and will not work on older versions.

#### How to Check Your Python Version

1.  Open your terminal or command prompt.
    *   On **macOS**, you can open the Terminal app.
    *   On **Windows**, you can open PowerShell or Command Prompt.
2.  Type one of the following commands and press Enter:
    ```bash
    python3 --version
    ```
    or if that doesn't work:
    ```bash
    python --version
    ```
3.  If the version number is `3.10.x` or higher, you are ready. If not, you will need to install a newer version.

#### How to Install Python

The easiest way to install Python is to download it from the official website:
*   **[python.org/downloads](https://www.python.org/downloads/)**

Download the installer for your operating system (Windows or macOS) and run it. **Important:** On Windows, make sure to check the box that says "Add Python to PATH" during installation.

## Setup Instructions

Follow these steps to get the application running on your local machine.

#### 1. Download the Code

Download the project files from GitHub. The simplest way is to download the ZIP file:
1.  Go to the main page of the repository.
2.  Click the green `<> Code` button.
3.  Select **Download ZIP**.
4.  Unzip the downloaded file to a location you can easily find, like your Desktop.

#### 2. Install Dependencies

1.  **Open a terminal** and navigate into the project folder you just unzipped. For example, if it's on your desktop, you might use a command like this:
    ```bash
    # On macOS or Linux
    cd ~/Desktop/strava-dashboard-main

    # On Windows
    cd C:\Users\YourUser\Desktop\strava-dashboard-main
    ```

2.  It is highly recommended to use a virtual environment to keep the project's dependencies separate from your system. Run the following commands to create and activate one:
    ```bash
    # Create the virtual environment
    python3 -m venv venv

    # Activate it
    # On macOS or Linux:
    source venv/bin/activate
    # On Windows:
    venv\Scripts\activate
    ```
    You will know it's active because your terminal prompt will change to show `(venv)`.

3.  Install all the required Python packages using the `requirements.txt` file:
    ```bash
    pip install -r requirements.txt
    ```

#### 3. Configure the Application

You need to tell the app your personal Strava API credentials.

1.  **Create your Strava API Application:**
    *   Log in to your Strava account.
    *   Go to **[strava.com/settings/api](https://www.strava.com/settings/api)** and create a new app.
    *   Fill in the details. For the **Authorization Callback Domain**, you **must** enter `localhost`.
    *   After creating the app, you will see your **Client ID** and **Client Secret**. Keep this page open.

2.  **Edit the `config.yaml` file:**
    *   In the project folder, open the `config.yaml` file with a text editor (like VS Code, Notepad, or TextEdit).
    *   Fill in the following fields:
        ```yaml
        # Optional: Set a username and password to protect access to the app itself.
        # Leave them blank to disable the login screen.
        app_username: 'user@email.com'
        app_password: 'your_secure_password'

        strava:
          # Copy these from your Strava API page
          client_id: 'YOUR_CLIENT_ID'
          client_secret: 'YOUR_CLIENT_SECRET'
        
        # Optional: For better maps, create a free account at mapbox.com and paste your token here.
        mapbox_token: 'YOUR_OPTIONAL_MAPBOX_TOKEN'
        ```
    *   **Do not** add `refresh_token` or `access_token` here. The application will handle those for you automatically after you log in for the first time.
    *   Save and close the file.

#### 4. Run the Application

1.  Make sure you are still in the project directory in your terminal and that your virtual environment is active (you should see `(venv)`).

2.  Start the web server with this command:
    ```bash
    python app.py
    ```

3.  Your terminal will show a message like `🚀 Starting Strava Dashboard at http://127.0.0.1:8000`. Your default web browser should open to this address automatically.

## How to Use

1.  If you set an `app_username` and `app_password`, you will be prompted to log in first.
2.  Click the **"Connect with Strava"** button. This will take you to a Strava page to authorize the application.
3.  After authorizing, you will be redirected back to the dashboard.
4.  Click **"Sync Last 30 Days"**. This will fetch your recent activities and populate the dropdown menu.
5.  Select an activity from the dropdown to load its data and view the map and charts.
6.  Use the "Analysis Options" to toggle the moving-time filter or change how the map route is colored.

Enjoy analyzing your rides and runs!