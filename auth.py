"""
auth.py — One-time Strava OAuth authorization flow.
Gets a token with activity:read_all scope and saves it to .env.

Usage:
    python auth.py
"""

import os
import re
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
REDIRECT_URI  = "http://localhost:8000/callback"
SCOPE         = "activity:read_all"

auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>Authorization failed. No code received.</h2>")

    def log_message(self, format, *args):
        pass  # suppress server logs


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        return

    # Build authorization URL
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={SCOPE}"
    )

    print("Opening Strava authorization page in your browser...")
    print(f"\nIf it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    # Start local server to catch the callback
    print("Waiting for Strava callback on http://localhost:8000 ...")
    server = HTTPServer(("localhost", 8000), CallbackHandler)
    server.handle_request()  # handles one request then stops

    if not auth_code:
        print("ERROR: No authorization code received.")
        return

    # Exchange code for tokens
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code":          auth_code,
        "grant_type":    "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()

    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]
    scope         = data.get("scope", "")
    athlete       = data.get("athlete", {})

    print(f"\nAuthorized as: {athlete.get('firstname')} {athlete.get('lastname')}")
    print(f"Scope granted: {scope}")

    # Write tokens back to .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "r") as f:
        content = f.read()
    content = re.sub(r"STRAVA_ACCESS_TOKEN=.*",  f"STRAVA_ACCESS_TOKEN={access_token}",  content)
    content = re.sub(r"STRAVA_REFRESH_TOKEN=.*", f"STRAVA_REFRESH_TOKEN={refresh_token}", content)
    with open(env_path, "w") as f:
        f.write(content)

    print(f"\nTokens saved to .env.")
    print(f"You can now run: python calibrate_peloton.py")


if __name__ == "__main__":
    main()
