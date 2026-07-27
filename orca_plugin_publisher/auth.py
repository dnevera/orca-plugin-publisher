"""Orca Cloud authentication via Supabase OAuth.

Flow:
  1. Open system browser to Supabase GoTrue authorize URL
  2. User authenticates via GitHub/Google/Apple/Discord
  3. Supabase redirects to http://127.0.0.1:{port}/callback#access_token=...
  4. One-shot handler captures tokens from URL fragment, shuts down
  5. Tokens stored in OS keyring
"""

from __future__ import annotations

import json
import logging
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

from .config import (
    SUPABASE_URL,
    SUPABASE_ANON_KEY,
    load_credential,
    store_credential,
    delete_credential,
)

log = logging.getLogger(__name__)

# Keyring keys for Orca Cloud tokens
_KEYS = {
    "access_token": "orca_cloud_access_token",
    "refresh_token": "orca_cloud_refresh_token",
    "user_email": "orca_cloud_user_email",
    "user_id": "orca_cloud_user_id",
}

# Fixed port for OAuth callback — registered in Supabase redirect allowlist.
# This server only lives for seconds during login.
OAUTH_CALLBACK_PORT = 19847
_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"


def auth_status() -> dict:
    """Check if user is authenticated to Orca Cloud."""
    token = load_credential(_KEYS["access_token"])
    if not token:
        return {"authenticated": False}
    email = load_credential(_KEYS["user_email"])
    return {"authenticated": True, "email": email}


def auth_logout() -> None:
    """Clear all stored Orca Cloud credentials."""
    for key in _KEYS.values():
        delete_credential(key)


def get_access_token() -> str | None:
    """Get stored Orca Cloud access token (for API calls)."""
    return load_credential(_KEYS["access_token"])


def oauth_login(orca_provider: str = "github") -> dict:
    """Run Supabase OAuth login. Blocking — call from a thread.

    Args:
        orca_provider: Supabase OAuth provider ('github', 'google', 'apple', 'discord')

    Returns:
        {"ok": True, ...} on success, {"ok": False, "error": "..."} on failure
    """
    params = {"provider": orca_provider, "redirect_to": _REDIRECT_URI}
    auth_url = f"{SUPABASE_URL}/auth/v1/authorize?{urlencode(params)}"

    # Shared state — populated by callback handler
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                # Supabase sends tokens in URL fragment (#access_token=...)
                # Fragments aren't sent to server — serve JS extractor page
                self._send_fragment_extractor()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/callback":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                result.update(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        def _send_fragment_extractor(self):
            """Serve HTML that reads URL fragment and POSTs tokens back."""
            html = """<!DOCTYPE html><html><body style="font-family:system-ui;text-align:center;padding:60px;background:#1a1a2e;color:#e0e0e0">
<h2>Authenticating...</h2><p id="s">Capturing tokens...</p>
<script>
(async()=>{
  const h=window.location.hash.substring(1);
  const p=new URLSearchParams(h);
  const d={access_token:p.get('access_token'),refresh_token:p.get('refresh_token'),
           email:p.get('email')||'',user_id:p.get('user_id')||''};
  if(!d.access_token){document.getElementById('s').textContent='No token found. Check the URL.';return;}
  try{
    await fetch('/callback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
    document.getElementById('s').innerHTML='<span style="color:#81c784">✅ Authenticated! You can close this tab.</span>';
  }catch(e){document.getElementById('s').textContent='Error: '+e.message;}
})();
</script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, *args):
            pass  # Suppress HTTP logs

    class _ReuseServer(HTTPServer):
        allow_reuse_address = True

    server = _ReuseServer(("127.0.0.1", OAUTH_CALLBACK_PORT), Handler)
    server.timeout = 1.0

    # Open browser
    webbrowser.open(auth_url)
    log.info("OAuth login: opened browser (port %d, provider=%s)", OAUTH_CALLBACK_PORT, orca_provider)

    # Wait for callback (max 120 seconds)
    deadline = time.monotonic() + 120
    while not result and time.monotonic() < deadline:
        server.handle_request()

    # Handle the POST response
    if result:
        server.handle_request()

    server.server_close()

    if not result.get("access_token"):
        return {"ok": False, "error": "Login timed out or failed"}

    # Save tokens to keyring
    for field, key in _KEYS.items():
        if result.get(field):
            store_credential(key, result[field])

    log.info("OAuth login successful")
    return {"ok": True, "email": result.get("email", "")}
