"""FastAPI web application for orca-plugin-publisher.

Serves the SPA dashboard and provides API endpoints for the full
plugin publish pipeline: discover → link → build → auth → publish.

Key design: repos do NOT require plugin_manifest.json upfront.
The app auto-detects metadata from pyproject.toml + README.md + build_wheel.py
and auto-generates the manifest when needed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .auth import (
    AuthState,
    get_auth_state,
    start_login_flow,
    run_auth_callback_server,
    _CallbackHandler,
    LOCAL_HOST,
)
from .builder import build_wheel, find_existing_wheel
from .cloud_api import OrcaCloudAPI, OrcaCloudError, ORCA_TOKEN_KEY
from .config import (
    load_config,
    save_config,
    load_credential,
    store_credential,
    LOCAL_PORT,
    ORCA_CLOUD_URL,
)
from .manifest import (
    PluginManifest,
    CloudState,
    ChangelogEntry,
    load_manifest,
    save_manifest,
    MANIFEST_FILENAME,
)
from .github_api import GitHubAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# =============================================================================
# FastAPI app
# =============================================================================

app = FastAPI(
    title="Orca Plugin Publisher",
    version=__version__,
)

_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# =============================================================================
# Pydantic models
# =============================================================================

class LinkRepoRequest(BaseModel):
    path: str

class SetTokenRequest(BaseModel):
    access_token: str
    refresh_token: str | None = None
    email: str | None = None
    user_id: str | None = None

class PublishRequest(BaseModel):
    repo_path: str
    build_first: bool = True

class ScanRequest(BaseModel):
    directory: str


class RegisterRequest(BaseModel):
    full_name: str        # e.g. "dnevera/bambu-exhaust-enforcer"
    clone_url: str        # HTTPS clone URL

class CreateManifestRequest(BaseModel):
    repo_path: str
    name: str
    import_name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = "MIT"
    plugin_types: list[str] = ["script"]
    tags: list[str] = []
    compatible_orcaslicer_version: str | None = None


# =============================================================================
# Auto-detect plugin metadata from any repo
# =============================================================================

def _detect_repo_metadata(repo_path: Path) -> dict[str, Any]:
    """Auto-detect plugin metadata from pyproject.toml, README.md, build_wheel.py.

    Works with ANY plugin repo — no plugin_manifest.json required.
    """
    info: dict[str, Any] = {
        "path": str(repo_path),
        "exists": repo_path.exists(),
        "has_manifest": False,
        "has_build_script": False,
        "has_wheel": False,
        "is_published": False,
    }

    if not repo_path.exists():
        return info

    # Try plugin_manifest.json first (best source)
    manifest_path = repo_path / MANIFEST_FILENAME
    if manifest_path.exists():
        try:
            manifest = load_manifest(repo_path)
            info["has_manifest"] = True
            info["name"] = manifest.name
            info["import_name"] = manifest.import_name
            info["version"] = manifest.version
            info["description"] = manifest.description
            info["author"] = manifest.author
            info["is_published"] = manifest.is_published
            info["cloud_uuid"] = manifest.cloud.uuid
            info["cloud_url"] = manifest.cloud_url
            info["plugin_types"] = manifest.plugin_types
            info["tags"] = manifest.tags
        except Exception as exc:
            info["error"] = str(exc)

    # Fallback: pyproject.toml
    if not info.get("name"):
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8")
                # Simple TOML parsing for [project] section
                name_m = re.search(r'name\s*=\s*"([^"]+)"', text)
                ver_m = re.search(r'version\s*=\s*"([^"]+)"', text)
                desc_m = re.search(r'description\s*=\s*"([^"]+)"', text)
                if name_m:
                    info["name"] = name_m.group(1)
                    info["import_name"] = name_m.group(1).replace("-", "_")
                if ver_m:
                    info["version"] = ver_m.group(1)
                if desc_m:
                    info["description"] = desc_m.group(1)
            except Exception:
                pass

    # Fallback: README.md for description
    if not info.get("description"):
        readme = repo_path / "README.md"
        if readme.exists():
            try:
                lines = readme.read_text(encoding="utf-8").strip().split("\n")
                # Skip title line(s), grab first paragraph
                desc_lines = []
                past_title = False
                for line in lines:
                    if line.startswith("#"):
                        past_title = True
                        continue
                    if past_title and line.strip():
                        desc_lines.append(line.strip())
                    if past_title and not line.strip() and desc_lines:
                        break
                if desc_lines:
                    info["description"] = " ".join(desc_lines)[:200]
            except Exception:
                pass

    # Fallback: directory name
    if not info.get("name"):
        info["name"] = repo_path.name

    # Check build_wheel.py
    info["has_build_script"] = (repo_path / "build_wheel.py").exists()

    # Check existing wheel
    whl = find_existing_wheel(repo_path)
    info["has_wheel"] = whl is not None
    if whl:
        info["wheel_name"] = whl.name
        info["wheel_size"] = whl.stat().st_size

    # Git info
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            info["git_remote"] = result.stdout.strip()
    except Exception:
        pass

    return info


def _ensure_manifest(repo_path: Path) -> PluginManifest:
    """Load or auto-generate plugin_manifest.json from repo metadata."""
    try:
        return load_manifest(repo_path)
    except FileNotFoundError:
        pass

    # Auto-generate from detected metadata
    meta = _detect_repo_metadata(repo_path)
    name = meta.get("name", repo_path.name)
    import_name = meta.get("import_name", name.replace("-", "_"))

    manifest = PluginManifest(
        name=name,
        import_name=import_name,
        version=meta.get("version", "0.1.0"),
        description=meta.get("description", ""),
        author=meta.get("author", ""),
        plugin_types=meta.get("plugin_types", ["script"]),
        homepage=meta.get("git_remote", ""),
    )

    # Check README for author (git config)
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            manifest.author = result.stdout.strip()
    except Exception:
        pass

    save_manifest(repo_path, manifest)
    log.info("Auto-generated %s in %s", MANIFEST_FILENAME, repo_path)
    return manifest


# =============================================================================
# Root
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def root():
    index = _static_dir / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>orca-plugin-publisher</h1><p>Static files not found.</p>")


# =============================================================================
# Auth endpoints
# =============================================================================

@app.get("/api/auth/status")
async def auth_status():
    state = get_auth_state()
    return state.to_dict()


@app.post("/api/auth/login")
async def auth_login():
    """Start login flow and callback server in background."""
    # Start callback server in background thread
    def _run_callback():
        data = run_auth_callback_server(timeout=120.0)
        if data.get("access_token"):
            state = AuthState()
            state.access_token = data["access_token"]
            state.refresh_token = data.get("refresh_token")
            state.user_email = data.get("user_email", data.get("email"))
            state.user_id = data.get("user_id")
            state.save()
            log.info("Login callback received: %s", state.user_email or "unknown")

    thread = threading.Thread(target=_run_callback, daemon=True)
    thread.start()

    # Return login URL for frontend to open
    callback_port = LOCAL_PORT + 1
    from urllib.parse import urlencode
    redirect_uri = f"http://{LOCAL_HOST}:{callback_port}/auth/callback"
    login_url = f"{ORCA_CLOUD_URL}/orcaslicer-login?{urlencode({'redirect_to': redirect_uri})}"

    # Open browser server-side (pywebview blocks window.open)
    webbrowser.open(login_url)

    return {"login_url": login_url}


@app.post("/api/auth/token")
async def auth_set_token(req: SetTokenRequest):
    state = AuthState()
    state.access_token = req.access_token
    state.refresh_token = req.refresh_token
    state.user_email = req.email
    state.user_id = req.user_id
    state.save()
    return {"ok": True, "authenticated": True, "email": req.email}


@app.post("/api/auth/logout")
async def auth_logout():
    state = get_auth_state()
    state.clear()
    return {"ok": True, "authenticated": False}


# =============================================================================
# GitHub integration (Device Flow)
# =============================================================================

# Background polling thread reference — lets us check status from the frontend
_gh_device_flow: dict = {}

@app.get("/api/github/status")
async def github_status():
    """Check if GitHub token is configured and valid."""
    token = GitHubAPI.load_token()
    if not token:
        return {"authenticated": False, "login": None, "avatar_url": None}
    try:
        gh = GitHubAPI(token)
        user = gh.get_user()
        gh.close()
        return {"authenticated": True, "login": user["login"], "avatar_url": user.get("avatar_url")}
    except Exception:
        return {"authenticated": False, "login": None, "avatar_url": None}


@app.post("/api/github/login")
async def github_login():
    """Start GitHub Device Flow. Returns user_code + verification_uri for the user."""
    global _gh_device_flow
    try:
        flow = GitHubAPI.start_device_flow()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Start background polling thread
    _gh_device_flow = {"status": "pending", "device_code": flow["device_code"]}

    def _poll():
        global _gh_device_flow
        token = GitHubAPI.poll_device_flow(
            flow["device_code"],
            interval=flow.get("interval", 5),
            expires_in=flow.get("expires_in", 900),
        )
        if token:
            GitHubAPI.save_token(token)
            try:
                gh = GitHubAPI(token)
                user = gh.get_user()
                gh.close()
                _gh_device_flow = {"status": "done", "login": user["login"]}
            except Exception:
                _gh_device_flow = {"status": "done", "login": "unknown"}
        else:
            _gh_device_flow = {"status": "failed"}

    thread = threading.Thread(target=_poll, daemon=True)
    thread.start()

    # Open browser server-side (pywebview blocks window.open)
    webbrowser.open(flow["verification_uri"])

    return {
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "expires_in": flow.get("expires_in", 900),
    }


@app.get("/api/github/poll")
async def github_poll():
    """Check if Device Flow polling has completed."""
    return _gh_device_flow or {"status": "idle"}


@app.post("/api/github/logout")
async def github_logout():
    """Remove stored GitHub token."""
    GitHubAPI.delete_token()
    return {"ok": True}


@app.get("/api/github/repos")
async def github_repos():
    """List user's public repos that have build_wheel.py (OrcaSlicer plugins)."""
    token = GitHubAPI.load_token()
    if not token:
        raise HTTPException(status_code=401, detail="GitHub not connected")
    gh = GitHubAPI(token)
    try:
        plugins = gh.list_plugin_repos()
        gh.close()
        return {"repos": plugins}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/repos/register")
async def register_from_github(req: RegisterRequest):
    """Clone a GitHub repo locally and register it as a plugin."""
    name = req.full_name.split("/")[-1]
    try:
        local_path = GitHubAPI.clone_repo(req.clone_url, name)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Clone failed: {exc}")

    # Link in config
    config = load_config()
    repos = config.get("repos", [])
    existing_paths = {r["path"] for r in repos}
    if str(local_path) not in existing_paths:
        repos.append({"path": str(local_path)})
        config["repos"] = repos
        save_config(config)

    info = _detect_repo_metadata(local_path)
    return {"ok": True, **info}


# =============================================================================
# Repo management
# =============================================================================

@app.get("/api/repos")
async def list_repos():
    """List linked repos with auto-detected metadata."""
    config = load_config()
    repos = config.get("repos", [])
    result = []
    for repo_entry in repos:
        repo_path = Path(repo_entry["path"])
        info = _detect_repo_metadata(repo_path)
        result.append(info)
    return {"repos": result}


@app.post("/api/repos/link")
async def link_repo(req: LinkRepoRequest):
    repo_path = Path(req.path).expanduser().resolve()
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {repo_path}")

    config = load_config()
    repos = config.get("repos", [])
    existing_paths = {r["path"] for r in repos}
    if str(repo_path) not in existing_paths:
        repos.append({"path": str(repo_path)})
        config["repos"] = repos
        save_config(config)

    info = _detect_repo_metadata(repo_path)
    return {"ok": True, **info}


@app.post("/api/repos/unlink")
async def unlink_repo(req: LinkRepoRequest):
    config = load_config()
    repos = config.get("repos", [])
    config["repos"] = [r for r in repos if r["path"] != req.path]
    save_config(config)
    return {"ok": True}


@app.post("/api/repos/detect")
async def detect_metadata(req: LinkRepoRequest):
    """Detect metadata from a repo without linking. For pre-filling the manifest form."""
    repo_path = Path(req.path).expanduser().resolve()
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {repo_path}")
    info = _detect_repo_metadata(repo_path)
    return info


@app.post("/api/repos/init-manifest")
async def init_manifest(req: CreateManifestRequest):
    """Create plugin_manifest.json in a repo from form data."""
    repo_path = Path(req.repo_path).expanduser().resolve()
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail="Repo not found")

    manifest = PluginManifest(
        name=req.name,
        import_name=req.import_name,
        version=req.version,
        description=req.description,
        author=req.author,
        homepage=req.homepage,
        license=req.license,
        plugin_types=req.plugin_types,
        tags=req.tags,
        compatible_orcaslicer_version=req.compatible_orcaslicer_version,
    )
    save_manifest(repo_path, manifest)

    # Auto-link the repo
    config = load_config()
    repos = config.get("repos", [])
    existing_paths = {r["path"] for r in repos}
    if str(repo_path) not in existing_paths:
        repos.append({"path": str(repo_path)})
        config["repos"] = repos
        save_config(config)

    return {"ok": True, "path": str(repo_path), "name": req.name}


@app.post("/api/repos/scan")
async def scan_directory(req: ScanRequest):
    """Scan a directory for plugin repos (dirs with build_wheel.py)."""
    scan_path = Path(req.directory).expanduser().resolve()
    if not scan_path.exists():
        raise HTTPException(status_code=404, detail="Directory not found")

    found = []
    for child in sorted(scan_path.iterdir()):
        if child.is_dir() and (child / "build_wheel.py").exists():
            info = _detect_repo_metadata(child)
            found.append(info)

    return {"plugins": found}


# =============================================================================
# Build
# =============================================================================

@app.post("/api/build")
async def build_plugin(req: PublishRequest):
    repo_path = Path(req.repo_path)
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail="Repository not found")

    # Auto-generate manifest if missing
    manifest = _ensure_manifest(repo_path)

    try:
        whl = build_wheel(repo_path, manifest.build_script, manifest.dist_dir)
        return {
            "ok": True,
            "wheel": whl.name,
            "size": whl.stat().st_size,
            "path": str(whl),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# =============================================================================
# Publish / Update
# =============================================================================

@app.post("/api/publish")
async def publish_plugin(req: PublishRequest):
    repo_path = Path(req.repo_path)

    # Auto-generate manifest if missing
    manifest = _ensure_manifest(repo_path)

    errors = manifest.validate()
    if errors:
        raise HTTPException(status_code=400, detail=f"Manifest errors: {'; '.join(errors)}")

    # Build
    if req.build_first:
        try:
            whl_path = build_wheel(repo_path, manifest.build_script, manifest.dist_dir)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Build failed: {exc}")
    else:
        whl_path = find_existing_wheel(repo_path, manifest.dist_dir)
        if whl_path is None:
            raise HTTPException(status_code=400, detail="No .whl found. Build first.")

    # Auth
    state = get_auth_state()
    if not state.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    api = OrcaCloudAPI(state.access_token)
    metadata = manifest.to_cloud_metadata()

    try:
        if manifest.is_published:
            result = api.update_plugin(
                plugin_id=manifest.cloud.uuid,
                metadata=metadata,
                whl_files=[whl_path],
            )
            action = "updated"
        else:
            result = api.create_plugin(
                metadata=metadata,
                whl_files=[whl_path],
            )
            action = "created"
            manifest.cloud.uuid = result.get("id")
            manifest.cloud.sharing_token = result.get("sharing_token")
            save_manifest(repo_path, manifest)

        return {
            "ok": True,
            "action": action,
            "uuid": result.get("id"),
            "sharing_token": result.get("sharing_token"),
            "cloud_url": f"{ORCA_CLOUD_URL}/p/{result.get('sharing_token', '')}",
            "version": manifest.version,
        }
    except OrcaCloudError as exc:
        raise HTTPException(status_code=exc.status_code or 500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# =============================================================================
# Cloud status
# =============================================================================

@app.get("/api/cloud/my-plugins")
async def cloud_my_plugins():
    state = get_auth_state()
    if not state.is_authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    api = OrcaCloudAPI(state.access_token)
    try:
        plugins = api.get_my_plugins()
        return {"plugins": plugins}
    except OrcaCloudError as exc:
        raise HTTPException(status_code=exc.status_code or 500, detail=str(exc))
