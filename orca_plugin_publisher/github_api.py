"""GitHub API client for orca-cloud-connector.

Provides OAuth Device Flow authentication and repo discovery via GitHub REST API v3.
Only public repos are relevant (Orca Cloud requires public source).

GitHub Device Flow (RFC 8628):
  1. App POSTs to /login/device/code → gets user_code + verification_uri
  2. User opens browser, enters user_code at github.com/login/device
  3. App polls /login/oauth/access_token until user approves
  4. Token stored in OS keyring

Usage:
    gh = GitHubAPI()
    flow = gh.start_device_flow()      # {"user_code": "ABCD-1234", ...}
    token = gh.poll_device_flow(flow)   # blocks until approved
    repos = gh.list_plugin_repos()
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

import httpx

from .config import (
    GITHUB_TOKEN_KEY,
    GITHUB_CLIENT_ID,
    REPOS_DIR,
    load_credential,
    store_credential,
    delete_credential,
)

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


class GitHubAPI:
    """GitHub REST API v3 client with Device Flow auth."""

    def __init__(self, token: str | None = None):
        self._token = token or load_credential(GITHUB_TOKEN_KEY)
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Lazy-init HTTP client (reusable)."""
        if self._client is None:
            headers = {"Accept": "application/vnd.github+json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.Client(
                base_url=GITHUB_API, headers=headers, timeout=15.0,
            )
        return self._client

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    # ---- Token storage (shared by all flows) ----

    @staticmethod
    def save_token(token: str) -> None:
        store_credential(GITHUB_TOKEN_KEY, token)

    @staticmethod
    def delete_token() -> None:
        delete_credential(GITHUB_TOKEN_KEY)

    @staticmethod
    def load_token() -> str | None:
        return load_credential(GITHUB_TOKEN_KEY)

    # ---- Device Flow ----

    @staticmethod
    def start_device_flow() -> dict:
        """Start GitHub Device Flow. Returns user_code, verification_uri, device_code, etc."""
        r = httpx.post(
            GITHUB_DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": "public_repo"},
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"GitHub Device Flow error: {data['error_description']}")
        return data  # device_code, user_code, verification_uri, expires_in, interval

    @staticmethod
    def poll_device_flow(device_code: str, interval: int = 5, expires_in: int = 900) -> str | None:
        """Poll GitHub until user approves. Returns access_token or None on timeout."""
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            time.sleep(interval)
            r = httpx.post(
                GITHUB_TOKEN_URL,
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
            data = r.json()
            if "access_token" in data:
                return data["access_token"]
            error = data.get("error")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval = data.get("interval", interval + 5)
            elif error in ("expired_token", "access_denied"):
                return None
            else:
                log.warning("GitHub poll unexpected: %s", data)
                return None
        return None

    # ---- User ----

    def get_user(self) -> dict:
        """GET /user — returns login, avatar_url, etc."""
        r = self._get_client().get("/user")
        r.raise_for_status()
        return r.json()

    # ---- Repos ----

    def _paginate(self, path: str, params: dict, per_page: int = 100) -> list[dict]:
        """Generic paginated GET."""
        results = []
        page = 1
        while True:
            r = self._get_client().get(path, params={**params, "per_page": per_page, "page": page})
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            results.extend(batch)
            page += 1
            if len(batch) < per_page:
                break
        return results

    def list_repos(self) -> list[dict]:
        """List all public repos for the authenticated user."""
        return self._paginate("/user/repos", {"visibility": "public", "sort": "updated"})

    def file_exists(self, owner: str, repo: str, path: str) -> bool:
        """Check if a file exists in a GitHub repo via Contents API."""
        r = self._get_client().get(f"/repos/{owner}/{repo}/contents/{path}")
        return r.status_code == 200

    def list_plugin_repos(self) -> list[dict]:
        """List repos that have build_wheel.py (OrcaSlicer plugins)."""
        plugins = []
        for repo in self.list_repos():
            owner, name = repo["owner"]["login"], repo["name"]
            if not self.file_exists(owner, name, "build_wheel.py"):
                continue
            plugins.append({
                "full_name": repo["full_name"],
                "name": name,
                "description": repo.get("description") or "",
                "clone_url": repo["clone_url"],
                "ssh_url": repo["ssh_url"],
                "html_url": repo["html_url"],
                "has_manifest": self.file_exists(owner, name, "plugin_manifest.json"),
                "stars": repo.get("stargazers_count", 0),
                "updated_at": repo.get("updated_at", ""),
            })
        return plugins

    # ---- Clone ----

    @staticmethod
    def clone_repo(clone_url: str, name: str) -> Path:
        """Clone a GitHub repo into REPOS_DIR/{name}. If exists, git pull."""
        REPOS_DIR.mkdir(parents=True, exist_ok=True)
        target = REPOS_DIR / name
        if target.exists() and (target / ".git").exists():
            log.info("Pulling latest for %s", name)
            subprocess.run(["git", "pull", "--ff-only"], cwd=str(target), capture_output=True, check=True)
        else:
            log.info("Cloning %s → %s", clone_url, target)
            subprocess.run(["git", "clone", clone_url, str(target)], capture_output=True, check=True)
        return target

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
