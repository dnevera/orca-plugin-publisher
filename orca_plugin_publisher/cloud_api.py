"""Orca Cloud API client for plugin management.

This module wraps the Orca Cloud REST API with a typed Python interface.
All endpoints were reverse-engineered from the cloud.orcaslicer.com JS bundle
(index-CGdIfVG3.js, ~1.3MB).

Reverse Engineering Method
--------------------------
The API was discovered by:
  1. Downloading the JS bundle: curl -sL 'https://cloud.orcaslicer.com/assets/index-CGdIfVG3.js'
  2. Finding API base: ``ht=()=>{const e="https://api.orcaslicer.com"...}``
  3. Finding plugin path: ``Fn="/api/v1/plugins"``
  4. Finding the fetch wrapper: ``jn=async(e,t={})=>{...Authorization: Bearer ${n}...}``
  5. Finding create function: ``async function HG({name, description, files, ...})``
  6. Finding update function: ``async function $G({pluginId, name, ...})``

Complete Plugin API Map
-----------------------
::

    POST   /api/v1/plugins                          — Create plugin
    PATCH  /api/v1/plugins/{uuid}                   — Update plugin
    DELETE /api/v1/plugins?ids={uuid}               — Delete plugin
    GET    /api/v1/plugins/mine                     — List my plugins
    GET    /api/v1/plugins/explore                  — Public catalog
    GET    /api/v1/plugins/explore/filter-options   — Catalog filters
    GET    /api/v1/plugins/share/{sharing_token}    — Get by sharing link
    POST   /api/v1/plugins/subscriptions            — Subscribe to plugin
    DELETE /api/v1/plugins/subscriptions?ids=       — Unsubscribe
    GET    /api/v1/plugins/subscriptions/list       — List subscriptions
    POST   /api/v1/plugins/likes                    — Like a plugin
    DELETE /api/v1/plugins/likes                    — Unlike a plugin
    GET    /api/v1/plugins/{uuid}/whitelist-users   — Private plugin whitelist
    GET/POST /api/v1/plugins/share/{token}/comments — Plugin comments

Authentication
--------------
Every request requires a Bearer token in the Authorization header:
    Authorization: Bearer {supabase_access_token}

The token is a JWT obtained from Supabase authentication (via the auth module).
Tokens are short-lived (~1 hour) and should be refreshed using the refresh_token.

Create Plugin — Wire Format
----------------------------
::

    POST /api/v1/plugins
    Content-Type: multipart/form-data

    FormData fields:
      "metadata" → JSON string:
        {
          "name": "Plugin Name",           # required
          "description": "What it does",   # required
          "public": true,                  # required — visibility
          "version": "0.2.0",              # optional
          "types": ["script"],             # optional — plugin capabilities
          "changelog": "- Added X\\n- Fixed Y", # optional
          "tags": ["tag1", "tag2"],         # optional
          "compatible_orcaslicer_version": "2.4.2",  # optional
          "main_image_attachment_id": "uuid",        # optional
          "attachment_ids": ["uuid1", "uuid2"]       # optional
        }
      "files" → .whl file (can appear multiple times for multiple files)

    Response: {"id": "uuid-string", "sharing_token": "abc123", ...}

Update Plugin — Wire Format
----------------------------
::

    PATCH /api/v1/plugins/{uuid}

    Mode 1: With new files (multipart/form-data)
      FormData:
        "metadata" → JSON string (same as create, plus "keep_artifact_ids")
        "files" → new .whl file(s)

    Mode 2: Metadata only (application/json)
      JSON body: same metadata fields as FormData mode

    Special field for updates:
      "keep_artifact_ids": ["existing-uuid"]  — IDs of files to keep from previous version

    Response: {"id": "uuid", ...}

TODO: Implement subscribe/unsubscribe endpoints for plugin marketplace integration
TODO: Implement likes endpoint for marketplace analytics
TODO: Implement whitelist management for private plugin distribution
TODO: Implement comment posting for plugin pages
TODO: Add async httpx support (currently sync for simplicity)
TODO: Add retry logic with exponential backoff for transient failures
TODO: Add token refresh logic when 401 Unauthorized is received
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import ORCA_API_BASE, ORCA_PLUGINS_PATH, load_credential

log = logging.getLogger(__name__)

# =============================================================================
# Credential keys — must match the keys used in auth.py / config.py
# =============================================================================

# Key under which the Supabase access token is stored in keyring
ORCA_TOKEN_KEY = "orca_cloud_access_token"

# Key for the refresh token (used to obtain new access tokens)
ORCA_REFRESH_TOKEN_KEY = "orca_cloud_refresh_token"

# HTTP timeout for API calls (in seconds).
# Plugin uploads can be large .whl files, so we allow generous timeout.
REQUEST_TIMEOUT = 60.0


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class CloudPlugin:
    """Parsed cloud plugin descriptor.

    Represents a plugin as returned by the Orca Cloud API.
    Used for displaying plugin info in the dashboard.

    TODO: Parse all fields from API response (currently partial).
    """

    uuid: str
    """Plugin UUID on Orca Cloud."""

    name: str
    """Display name."""

    version: str
    """Current version string."""

    description: str
    """Short description."""

    sharing_token: str
    """Public sharing link token. URL: cloud.orcaslicer.com/p/{sharing_token}"""

    is_public: bool
    """Whether the plugin is publicly discoverable in the catalog."""

    author: str | None = None
    """Author name/handle (optional)."""

    types: list[str] | None = None
    """Plugin capability types (e.g., ["script", "slicing-pipeline"])."""

    tags: list[str] | None = None
    """Searchable tags."""


# =============================================================================
# Exception classes
# =============================================================================

class OrcaCloudError(Exception):
    """Raised when the Orca Cloud API returns an error.

    Attributes:
        status_code: HTTP status code (e.g., 401, 404, 500).
        body: Parsed response body (dict or string).
    """

    def __init__(self, message: str, status_code: int = 0, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def __str__(self) -> str:
        msg = super().__str__()
        if self.body:
            msg = f"{msg} | body={self.body}"
        return msg


# =============================================================================
# Orca Cloud API client
# =============================================================================

class OrcaCloudAPI:
    """HTTP client for Orca Cloud plugin API.

    Wraps all CRUD operations for plugins. Requires a valid Supabase
    access token obtained via the auth module (browser OAuth flow).

    Usage:
        api = OrcaCloudAPI(access_token)
        # Create a new plugin
        result = api.create_plugin(metadata, [Path("dist/plugin.whl")])
        # Update existing plugin
        result = api.update_plugin(uuid, metadata, [Path("dist/plugin.whl")])
        # List my plugins
        plugins = api.get_my_plugins()

    TODO: Add connection pooling (reuse httpx.Client across calls)
    TODO: Add automatic token refresh on 401 responses
    TODO: Add rate limiting to avoid API throttling
    """

    def __init__(self, access_token: str | None = None):
        """Initialize the API client.

        Args:
            access_token: Supabase JWT access token. If None, attempts to
                          load from keyring/credential store.
        """
        self._token = access_token or load_credential(ORCA_TOKEN_KEY)
        self._base = ORCA_API_BASE       # "https://api.orcaslicer.com"
        self._plugins = ORCA_PLUGINS_PATH # "/api/v1/plugins"

    @property
    def is_authenticated(self) -> bool:
        """Check if we have an access token (does NOT verify it's valid)."""
        return self._token is not None and len(self._token) > 0

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        """Build HTTP headers with auth and optional content type.

        The Orca Cloud API uses Bearer token authentication:
            Authorization: Bearer {supabase_access_token}

        For multipart/form-data requests (file uploads), do NOT set Content-Type
        — httpx sets it automatically with the correct boundary.
        For JSON-only requests, set Content-Type: application/json.
        """
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        """Construct full API URL from base + path.

        Example: _url("/api/v1/plugins/mine") → "https://api.orcaslicer.com/api/v1/plugins/mine"
        """
        return f"{self._base}{path}"

    def _check_response(self, resp: httpx.Response, context: str) -> Any:
        """Validate HTTP response and parse JSON body.

        Raises OrcaCloudError on any 4xx/5xx status code.
        The error includes the original response body for debugging.

        TODO: Handle 401 specifically → trigger token refresh
        TODO: Handle 429 (rate limited) → add retry with backoff
        """
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            log.warning("%s: HTTP %d — %s", context, resp.status_code, body)
            raise OrcaCloudError(
                f"{context}: HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )
        try:
            return resp.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Media attachment upload (for plugin images)
    # ------------------------------------------------------------------

    _media_path = "/api/v1/bundles/media"

    def upload_media_attachment(self, file_path: Path) -> str:
        """Upload a media file (image) and return its attachment UUID.

        Three-step flow (discovered from Orca Cloud frontend JS):
          1. POST /api/v1/bundles/media/upload-url → get pre-signed upload_url + attachment
          2. PUT upload_url → upload actual file bytes
          3. POST /api/v1/bundles/media/{id}/complete → finalize

        Args:
            file_path: Path to image file (PNG, JPG, WEBP, GIF).

        Returns:
            Attachment UUID string (use as main_image_attachment_id in metadata).

        Raises:
            OrcaCloudError: On any upload step failure.
        """
        # Detect MIME type
        suffix = file_path.suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif"}
        mime_type = mime_map.get(suffix, "application/octet-stream")
        file_bytes = file_path.read_bytes()

        # Step 1: Get pre-signed upload URL
        upload_url_endpoint = self._url(f"{self._media_path}/upload-url")
        payload = {
            "filename": file_path.name,
            "mime_type": mime_type,
            "byte_size": len(file_bytes),
            "storage_namespace": "plugin",
        }

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(
                upload_url_endpoint,
                headers=self._headers(content_type="application/json"),
                json=payload,
            )

        result = self._check_response(resp, "Media upload: get URL")
        upload_url = result.get("upload_url")
        attachment = result.get("attachment", {})
        attachment_id = attachment.get("id")

        if not upload_url or not attachment_id:
            raise OrcaCloudError("Media upload: invalid response (no upload_url or id)")

        log.info("Media upload step 1: got upload URL for %s (id=%s)", file_path.name, attachment_id)

        # Step 2: PUT file to pre-signed URL
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.put(
                upload_url,
                headers={"Content-Type": mime_type},
                content=file_bytes,
            )

        if resp.status_code >= 400:
            raise OrcaCloudError(f"Media upload: PUT failed with HTTP {resp.status_code}")

        log.info("Media upload step 2: uploaded %d bytes", len(file_bytes))

        # Step 3: Finalize
        complete_url = self._url(f"{self._media_path}/{attachment_id}/complete")
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(complete_url, headers=self._headers())

        self._check_response(resp, "Media upload: finalize")
        log.info("Media upload step 3: finalized attachment %s", attachment_id)

        return attachment_id

    def download_media_content(self, attachment_id: str) -> bytes:
        """Download media attachment content by ID.

        Endpoint: GET /api/v1/bundles/media/{attachment_id}/content

        Args:
            attachment_id: The media attachment UUID.

        Returns:
            Raw file bytes.

        Raises:
            OrcaCloudError: On download failure.
        """
        url = self._url(f"{self._media_path}/{attachment_id}/content")
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers=self._headers())
        if resp.status_code >= 400:
            raise OrcaCloudError(
                f"Media download failed: HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.content

    # ------------------------------------------------------------------
    # Plugin CRUD operations
    # ------------------------------------------------------------------

    def create_plugin(
        self,
        metadata: dict[str, Any],
        whl_files: list[Path],
    ) -> dict[str, Any]:
        """Create a new plugin on Orca Cloud.

        Sends a POST request with multipart/form-data containing:
          - "metadata" field: JSON string with plugin info
          - "files" field(s): one or more .whl files

        The Orca Cloud JS code (function HG) constructs this as:
            const b = new FormData;
            b.set("metadata", JSON.stringify(p));
            n.forEach(_ => { b.append("files", _) });
            fetch(url, {method: "POST", formBody: b})

        Args:
            metadata: Plugin metadata dict from PluginManifest.to_cloud_metadata().
            whl_files: List of .whl file paths to upload.

        Returns:
            API response dict with "id" (uuid) and "sharing_token".

        Raises:
            OrcaCloudError: On API error or invalid response.
        """
        # Build the multipart files list
        # Each .whl file is sent as a "files" field with its original filename
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for whl in whl_files:
            files.append(("files", (whl.name, whl.read_bytes(), "application/octet-stream")))

        # The "metadata" field is a JSON string (not a JSON body!)
        # This matches the FormData.set("metadata", JSON.stringify(p)) pattern
        data = {"metadata": json.dumps(metadata)}

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.post(
                self._url(self._plugins),
                headers=self._headers(),  # NOTE: no Content-Type — httpx sets multipart boundary
                data=data,
                files=files,
            )

        result = self._check_response(resp, "Create plugin")

        # Validate: the API must return a string "id" field
        if not isinstance(result.get("id"), str):
            raise OrcaCloudError("Plugin creation returned an invalid response (no id)")

        log.info("Created plugin: uuid=%s, sharing_token=%s", result.get("id"), result.get("sharing_token"))
        return result

    def update_plugin(
        self,
        plugin_id: str,
        metadata: dict[str, Any],
        whl_files: list[Path] | None = None,
        keep_artifact_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update an existing plugin on Orca Cloud.

        Two modes of operation (from JS function $G):

        Mode 1 — With new files (multipart/form-data PATCH):
          Used when uploading a new .whl or when keep_artifact_ids is specified.
          The JS code checks: ``A.length > 0 || x !== void 0``

        Mode 2 — Metadata only (JSON PATCH):
          Used when updating only description, tags, etc. without new files.

        The "keep_artifact_ids" field tells the server which existing files
        to preserve. Without it, uploading new files replaces ALL old files.

        Args:
            plugin_id: The plugin UUID to update.
            metadata: Updated metadata dict.
            whl_files: Optional new .whl files to upload.
            keep_artifact_ids: Optional list of existing artifact UUIDs to keep.

        Returns:
            Updated plugin data from API.

        Raises:
            OrcaCloudError: On API error.

        TODO: Extract existing artifact IDs from the current plugin state
              so we can intelligently decide what to keep vs replace.
        """
        if keep_artifact_ids is not None:
            metadata["keep_artifact_ids"] = keep_artifact_ids

        has_files = whl_files is not None and len(whl_files) > 0

        # Construct URL: /api/v1/plugins/{uuid}
        # Simple URL encoding — just use the uuid directly
        url = self._url(f"{self._plugins}/{httpx.URL(plugin_id).raw_path}" if "/" in plugin_id
                        else f"{self._plugins}/{plugin_id}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            if has_files:
                # Mode 1: Multipart FormData PATCH (new files + metadata)
                files: list[tuple[str, tuple[str, bytes, str]]] = []
                for whl in (whl_files or []):
                    files.append(("files", (whl.name, whl.read_bytes(), "application/octet-stream")))

                resp = client.patch(
                    url,
                    headers=self._headers(),  # No Content-Type for multipart
                    data={"metadata": json.dumps(metadata)},
                    files=files,
                )
            else:
                # Mode 2: JSON-only PATCH (metadata update without files)
                resp = client.patch(
                    url,
                    headers=self._headers(content_type="application/json"),
                    json=metadata,
                )

        result = self._check_response(resp, "Update plugin")
        log.info("Updated plugin %s to version %s", plugin_id, metadata.get("version", "?"))
        return result

    def get_my_plugins(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Fetch the current user's plugins from Orca Cloud.

        Endpoint: GET /api/v1/plugins/mine?limit={n}&offset={n}
        Pagination: Uses limit/offset (max 100 per page, from JS: xT=24).

        Args:
            limit: Maximum number of plugins to return (1-100).
            offset: Pagination offset.

        Returns:
            List of plugin dicts from API response "data" field.
        """
        url = self._url(f"{self._plugins}/mine?limit={limit}&offset={offset}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(url, headers=self._headers())

        result = self._check_response(resp, "Get my plugins")
        return result.get("data", [])

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        """Fetch a plugin by its UUID.

        Endpoint: GET /api/v1/plugins/{uuid}

        Args:
            plugin_id: The plugin UUID.

        Returns:
            Plugin data dict (includes version, name, artifacts, etc.).
        """
        url = self._url(f"{self._plugins}/{plugin_id}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(url, headers=self._headers())

        return self._check_response(resp, "Get plugin")

    def get_plugin_by_sharing_token(self, sharing_token: str) -> dict[str, Any]:
        """Fetch a plugin by its sharing token (public link).

        Endpoint: GET /api/v1/plugins/share/{sharing_token}
        No authentication required for public plugins.

        Args:
            sharing_token: The sharing link token from cloud.orcaslicer.com/p/{token}.

        Returns:
            Plugin data dict.
        """
        url = self._url(f"{self._plugins}/share/{sharing_token}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(url, headers=self._headers())

        return self._check_response(resp, "Get plugin by sharing token")

    def delete_plugin(self, plugin_id: str) -> None:
        """Delete a plugin from Orca Cloud.

        Endpoint: DELETE /api/v1/plugins?ids={uuid}

        WARNING: This permanently deletes the plugin and all its versions.
        There is no undo.

        Args:
            plugin_id: The plugin UUID to delete.

        Raises:
            OrcaCloudError: On API error.
        """
        url = self._url(f"{self._plugins}?ids={plugin_id}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.delete(url, headers=self._headers())

        self._check_response(resp, "Delete plugin")
        log.info("Deleted plugin %s", plugin_id)
