"""
One-time local authorisation. Run this on your laptop, not in CI.

    python scripts/authorize.py whoop
    python scripts/authorize.py google

Each command opens a browser, catches the redirect on localhost, and prints a
refresh token to paste into GitHub Actions secrets. After this you never run it
again unless a token is revoked.

WHOOP quirks handled here:
  * the `state` parameter is mandatory and must be at least 8 characters
  * `offline` must be in the scope list or no refresh token is issued at all
  * the redirect URI must match the one registered in the WHOOP dashboard
    exactly, including the trailing path
"""

from __future__ import annotations

import http.server
import os
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.whoop import AUTH_URL, SCOPES, exchange_code  # noqa: E402

PORT = 8080
REDIRECT_URI = f"http://localhost:{PORT}/callback"

_received: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _received.update({k: v[0] for k, v in params.items()})

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in _received
        body = (
            "<h2>Authorised.</h2><p>You can close this tab and return to the terminal.</p>"
            if ok
            else f"<h2>Authorisation failed.</h2><pre>{_received}</pre>"
        )
        self.wfile.write(f"<html><body style='font-family:system-ui'>{body}</body></html>".encode())

    def log_message(self, *args):  # silence the default access log
        pass


def _capture_redirect() -> dict[str, str]:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), _Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"Listening on {REDIRECT_URI} ...")
        while "code" not in _received and "error" not in _received:
            pass
        httpd.shutdown()
    return _received


def authorize_whoop() -> None:
    client_id = os.environ.get("WHOOP_CLIENT_ID") or input("WHOOP client ID: ").strip()
    client_secret = (
        os.environ.get("WHOOP_CLIENT_SECRET") or input("WHOOP client secret: ").strip()
    )

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        # WHOOP rejects a state shorter than 8 characters with an opaque error.
        "state": secrets.token_urlsafe(16),
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("\nOpening browser. If it does not open, paste this:\n")
    print(url, "\n")
    webbrowser.open(url)

    result = _capture_redirect()
    if "code" not in result:
        raise SystemExit(f"Authorisation failed: {result}")

    token = exchange_code(client_id, client_secret, result["code"], REDIRECT_URI)
    print("\n" + "=" * 68)
    print("WHOOP_REFRESH_TOKEN")
    print("=" * 68)
    print(token.refresh_token)
    print("=" * 68)
    print(
        "\nAdd this as a GitHub Actions secret named WHOOP_REFRESH_TOKEN.\n"
        "It is single use: the first sync spends it and stores the replacement\n"
        "in _state.json in your Drive folder. That is expected.\n"
    )


def authorize_google() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    from src.store import DRIVE_SCOPES

    client_id = os.environ.get("GOOGLE_CLIENT_ID") or input("Google client ID: ").strip()
    client_secret = (
        os.environ.get("GOOGLE_CLIENT_SECRET") or input("Google client secret: ").strip()
    )

    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }
    flow = InstalledAppFlow.from_client_config(config, DRIVE_SCOPES)
    # access_type=offline + prompt=consent forces a refresh token even if this
    # account has authorised the app before.
    creds = flow.run_local_server(
        port=PORT, access_type="offline", prompt="consent"
    )

    print("\n" + "=" * 68)
    print("GOOGLE_REFRESH_TOKEN")
    print("=" * 68)
    print(creds.refresh_token)
    print("=" * 68)
    print(
        "\nAdd this as a GitHub Actions secret named GOOGLE_REFRESH_TOKEN.\n"
        "IMPORTANT: if your OAuth consent screen is still in 'Testing' status,\n"
        "Google expires this token after 7 days. Publish the app to 'In production'\n"
        "or the workflow will break every week.\n"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("whoop", "google"):
        raise SystemExit("Usage: python scripts/authorize.py [whoop|google]")
    {"whoop": authorize_whoop, "google": authorize_google}[sys.argv[1]]()
