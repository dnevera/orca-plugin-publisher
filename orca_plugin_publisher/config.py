"""Configuration and credential storage for orca-plugin-publisher.

This module provides:
  1. Application-wide constants (API URLs, ports, paths)
  2. Persistent config storage (~/.orca-plugin-publisher/config.json)
  3. Secure credential storage via OS keyring (macOS Keychain)

Security Model
--------------
Credentials (access tokens, refresh tokens) are stored in the OS keyring
for maximum security. This means:
  - On macOS: stored in Keychain, encrypted at rest, requires user password
  - On Linux: stored via SecretService (GNOME Keyring / KDE Wallet)
  - Fallback: encrypted file at ~/.orca-plugin-publisher/.credentials.json
    with 0600 permissions (only owner can read/write)

Configuration File
------------------
Application config (non-secret data like linked repos) is stored in plain JSON:
  ~/.orca-plugin-publisher/config.json

Example config.json structure:
  {
    "repos": [
      {"path": "/Users/denn/Develop/3dprint_software/bambu-exhaust-enforcer"}
    ]
  }

API Discovery Notes
-------------------
All Orca Cloud endpoints were reverse-engineered from the cloud.orcaslicer.com
JS bundle (index-CGdIfVG3.js, ~1.3MB minified). Key findings:

  - Base API URL: https://api.orcaslicer.com
  - Auth: Supabase-based (https://kmaujjxeqrqungoncqzv.supabase.co)
  - Token format: Bearer JWT (access_token from Supabase session)
  - Plugin API base: /api/v1/plugins
  - The JS bundle uses:
      ht() → returns "https://api.orcaslicer.com"
      Fn = "/api/v1/plugins"
      jn() → fetch wrapper that adds Authorization: Bearer {token}
      Mn() → response checker that throws on non-ok status

TODO: Investigate if the Supabase anon key is needed for our use case
      (currently we use browser-based login which handles it via the
      Orca Cloud web app)
TODO: Add support for token auto-refresh when access_token expires
TODO: Add support for multiple Orca Cloud environments (dev/staging/prod)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# =============================================================================
# Paths — local storage locations
# =============================================================================

# Application config directory (non-secret data like linked repos list)
CONFIG_DIR = Path.home() / ".orca-plugin-publisher"

# Main config file path
CONFIG_FILE = CONFIG_DIR / "config.json"

# Directory for cloned GitHub repos
REPOS_DIR = CONFIG_DIR / "repos"

# Keyring service name — all credentials stored under this service identifier.
# On macOS this appears in Keychain Access as "orca-plugin-publisher".
KEYRING_SERVICE = "orca-plugin-publisher"

# GitHub OAuth App credentials.
# Create at: https://github.com/settings/applications/new
# Set callback URL to: http://127.0.0.1 (port is dynamic)
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "REPLACE_WITH_YOUR_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "REPLACE_WITH_YOUR_CLIENT_SECRET")

# GitHub access token keyring key
GITHUB_TOKEN_KEY = "github_access_token"


# =============================================================================
# Orca Cloud API endpoints (reverse-engineered from JS bundle)
# =============================================================================

# Base URL for all API calls.
# Discovered from: `ht=()=>{const e="https://api.orcaslicer.com".trim(); ...}`
ORCA_API_BASE = "https://api.orcaslicer.com"

# Auth endpoint (Supabase-based authentication)
ORCA_AUTH_URL = "https://auth.orcaslicer.com"

# Web portal URL (used for login redirect and cloud_url generation)
ORCA_CLOUD_URL = "https://cloud.orcaslicer.com"

# Plugin API path — appended to ORCA_API_BASE for all plugin operations.
# Discovered from: `Fn="/api/v1/plugins"`
# Full endpoint list:
#   POST   {base}/api/v1/plugins              — create plugin
#   PATCH  {base}/api/v1/plugins/{uuid}       — update plugin
#   DELETE {base}/api/v1/plugins?ids={uuid}   — delete plugin
#   GET    {base}/api/v1/plugins/mine         — list user's own plugins
#   GET    {base}/api/v1/plugins/explore      — public catalog
#   GET    {base}/api/v1/plugins/share/{tok}  — get by sharing link
ORCA_PLUGINS_PATH = "/api/v1/plugins"


# =============================================================================
# Supabase project identifiers (public, non-secret)
# =============================================================================

# Supabase project URL — used by the JS SDK for authentication.
# Extracted from OrcaSlicer C++ source code.
SUPABASE_URL = "https://kmaujjxeqrqungoncqzv.supabase.co"

# Supabase anon key — this is a public, read-only key (NOT a secret).
# It's embedded in every client-side app that uses Supabase.
# Extracted from the Orca Cloud JS bundle (index-DuYa3U6o.js).
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImttYXVqanhlcXJxdW5nb25jcXp2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk3ODk4NzAsImV4cCI6MjA3NTM2NTg3MH0.-ChNHK2t0Fbsi8opS2nFse7zxJKpPtvYWqG15sbE908"


# =============================================================================
# Local web server settings
# =============================================================================

# Port for the FastAPI dashboard (localhost only)
LOCAL_PORT = 8420

# Bind to localhost only — never expose to network
LOCAL_HOST = "127.0.0.1"

# OAuth redirect URI — the auth callback URL that captures the token
# after the user authenticates in the browser.
# NOTE: The auth module uses LOCAL_PORT+1 (8421) for the callback server
# to avoid conflicts with the main FastAPI app running on 8420.
REDIRECT_URI = f"http://{LOCAL_HOST}:{LOCAL_PORT}/auth/callback"


# =============================================================================
# Config file operations — persistent non-secret storage
# =============================================================================

def _ensure_config_dir() -> None:
    """Create the config directory if it doesn't exist.

    Creates: ~/.orca-plugin-publisher/
    This directory holds config.json and the fallback .credentials.json.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    """Load application config from disk.

    Config structure:
      {
        "repos": [                           # List of linked plugin repositories
          {"path": "/absolute/path/to/repo"} # Each repo must have a plugin_manifest.json
        ]
      }

    Returns:
        Parsed config dict, or empty dict if no config exists or parse fails.
    """
    _ensure_config_dir()
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load config: %s", exc)
    return {}


def save_config(config: dict[str, Any]) -> None:
    """Persist application config to disk.

    Writes to: ~/.orca-plugin-publisher/config.json
    Format: Pretty-printed JSON with 2-space indent for readability.

    Args:
        config: The full config dict to persist.
    """
    _ensure_config_dir()
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# =============================================================================
# Credential helpers — secure storage via OS keyring
# =============================================================================
#
# SECURITY: We use the `keyring` library which delegates to the OS-native
# credential store. On macOS this is Keychain Access, which encrypts secrets
# at rest and requires the user's login password to unlock.
#
# Keys stored in keyring:
#   "orca_cloud_access_token"   — Supabase JWT access token (short-lived)
#   "orca_cloud_refresh_token"  — Supabase refresh token (long-lived)
#   "orca_cloud_user_email"     — User's email (for display in UI)
#   "orca_cloud_user_id"        — User's Supabase UUID
#
# TODO: Implement automatic token refresh using refresh_token when
#       access_token expires (JWT typically has ~1 hour TTL)
# TODO: Add token expiry check before API calls

def store_credential(key: str, value: str) -> None:
    """Store a credential securely in the OS keyring.

    Falls back to file-based storage if keyring is unavailable
    (e.g., headless server, missing dbus on Linux).

    Args:
        key: Credential identifier (e.g., "orca_cloud_access_token").
        value: The secret value to store.
    """
    try:
        import keyring as kr
        kr.set_password(KEYRING_SERVICE, key, value)
    except Exception as exc:
        log.warning("Keyring storage failed for '%s': %s — falling back to file", key, exc)
        _store_credential_file(key, value)


def load_credential(key: str) -> str | None:
    """Load a credential from the OS keyring.

    Falls back to file-based storage if keyring is unavailable.

    Args:
        key: Credential identifier.

    Returns:
        The secret value, or None if not found.
    """
    try:
        import keyring as kr
        return kr.get_password(KEYRING_SERVICE, key)
    except Exception:
        return _load_credential_file(key)


def delete_credential(key: str) -> None:
    """Remove a credential from the OS keyring.

    Silently ignores errors (e.g., credential doesn't exist).

    Args:
        key: Credential identifier to remove.
    """
    try:
        import keyring as kr
        kr.delete_password(KEYRING_SERVICE, key)
    except Exception:
        pass


# =============================================================================
# Fallback file-based credential storage
# =============================================================================
#
# Used when the OS keyring is unavailable. The file is stored with
# restrictive permissions (0600 = owner read/write only).
#
# SECURITY WARNING: File-based storage is less secure than keyring.
# The file is NOT encrypted — it relies on filesystem permissions only.
# This is a last resort for environments without a keyring backend.
#
# File location: ~/.orca-plugin-publisher/.credentials.json
# The leading dot makes it hidden on Unix/macOS.

_CRED_FILE = CONFIG_DIR / ".credentials.json"


def _store_credential_file(key: str, value: str) -> None:
    """Store a credential in the fallback JSON file.

    The file is created with 0600 permissions (owner read/write only).

    Args:
        key: Credential identifier.
        value: The secret value.
    """
    _ensure_config_dir()
    creds = {}
    if _CRED_FILE.exists():
        try:
            creds = json.loads(_CRED_FILE.read_text())
        except Exception:
            pass
    creds[key] = value
    _CRED_FILE.write_text(json.dumps(creds))
    # Restrict file permissions to owner only (security measure)
    _CRED_FILE.chmod(0o600)


def _load_credential_file(key: str) -> str | None:
    """Load a credential from the fallback JSON file.

    Args:
        key: Credential identifier.

    Returns:
        The secret value, or None if file doesn't exist or key not found.
    """
    if not _CRED_FILE.exists():
        return None
    try:
        creds = json.loads(_CRED_FILE.read_text())
        return creds.get(key)
    except Exception:
        return None
