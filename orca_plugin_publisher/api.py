"""pywebview js_api — Python backend called directly from JS.

All methods are callable from JavaScript via:
    const result = await window.pywebview.api.method_name(args);

Methods run in a background thread (pywebview handles this),
so blocking I/O is fine.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

from .auth import auth_status, auth_logout, oauth_login, get_access_token
from .builder import build_wheel, find_existing_wheel
from .cloud_api import OrcaCloudAPI, OrcaCloudError
from .config import (
    ORCA_CLOUD_URL,
    load_config,
    save_config,
)
from .manifest import (
    PluginManifest,
    load_manifest,
    save_manifest,
    MANIFEST_FILENAME,
)

log = logging.getLogger(__name__)


# ===================== Repo metadata detection =====================

def _detect_repo_metadata(repo_path: Path) -> dict[str, Any]:
    """Auto-detect plugin metadata from pyproject.toml, README.md, build_wheel.py."""
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

    # plugin_manifest.json — best source
    manifest_path = repo_path / MANIFEST_FILENAME
    if manifest_path.exists():
        try:
            m = load_manifest(repo_path)
            info.update(has_manifest=True, name=m.name, import_name=m.import_name,
                        version=m.version, description=m.description, author=m.author,
                        is_published=m.is_published, cloud_uuid=m.cloud.uuid,
                        cloud_url=m.cloud_url, plugin_types=m.plugin_types, tags=m.tags)
        except Exception as exc:
            info["error"] = str(exc)

    # Fallback: pyproject.toml
    if not info.get("name"):
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                text = pyproject.read_text(encoding="utf-8")
                for field, key in [("name", "name"), ("version", "version"), ("description", "description")]:
                    m = re.search(rf'{field}\s*=\s*"([^"]+)"', text)
                    if m:
                        info[key] = m.group(1)
                        if key == "name":
                            info["import_name"] = m.group(1).replace("-", "_")
            except Exception:
                pass

    # Fallback: README.md first paragraph
    if not info.get("description"):
        readme = repo_path / "README.md"
        if readme.exists():
            try:
                lines, desc, past_title = readme.read_text(encoding="utf-8").strip().split("\n"), [], False
                for line in lines:
                    if line.startswith("#"):
                        past_title = True; continue
                    if past_title and line.strip():
                        desc.append(line.strip())
                    if past_title and not line.strip() and desc:
                        break
                if desc:
                    info["description"] = " ".join(desc)[:200]
            except Exception:
                pass

    if not info.get("name"):
        info["name"] = repo_path.name

    info["has_build_script"] = (repo_path / "build_wheel.py").exists()
    whl = find_existing_wheel(repo_path)
    info["has_wheel"] = whl is not None
    if whl:
        info["wheel_name"] = whl.name
        info["wheel_size"] = whl.stat().st_size

    # Git remote
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"],
                           cwd=str(repo_path), capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            info["git_remote"] = r.stdout.strip()
    except Exception:
        pass

    return info


def _ensure_manifest(repo_path: Path) -> PluginManifest:
    """Load or auto-generate plugin_manifest.json."""
    try:
        return load_manifest(repo_path)
    except FileNotFoundError:
        pass
    meta = _detect_repo_metadata(repo_path)
    name = meta.get("name", repo_path.name)
    manifest = PluginManifest(
        name=name, import_name=meta.get("import_name", name.replace("-", "_")),
        version=meta.get("version", "0.1.0"), description=meta.get("description", ""),
        author=meta.get("author", ""), plugin_types=meta.get("plugin_types", ["script"]),
        homepage=meta.get("git_remote", ""),
    )
    try:
        r = subprocess.run(["git", "config", "user.name"],
                           cwd=str(repo_path), capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            manifest.author = r.stdout.strip()
    except Exception:
        pass
    save_manifest(repo_path, manifest)
    return manifest


def _link_repo_path(repo_path: Path) -> None:
    """Add repo path to config if not already linked."""
    config = load_config()
    repos = config.get("repos", [])
    if str(repo_path) not in {r["path"] for r in repos}:
        repos.append({"path": str(repo_path)})
        config["repos"] = repos
        save_config(config)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_IMAGE_SEARCH_DIRS = ["rc", "assets", "images", "screenshots", "."]
_IMAGE_SEARCH_NAMES = ["dashboard", "screenshot", "thumbnail", "cover", "main", "preview"]


def _find_plugin_image(repo_path: Path, manifest) -> Path | None:
    """Find the main image for a plugin repo.

    Search order:
      1. manifest.screenshot field (explicit path)
      2. Common directories (rc/, assets/, images/) with common names
      3. First image file found in root
    """
    # 1. Explicit from manifest
    if manifest.screenshot:
        p = repo_path / manifest.screenshot
        if p.exists():
            return p

    # 2. Common locations + names
    for dir_name in _IMAGE_SEARCH_DIRS:
        d = repo_path / dir_name if dir_name != "." else repo_path
        if not d.is_dir():
            continue
        for name in _IMAGE_SEARCH_NAMES:
            for ext in _IMAGE_EXTENSIONS:
                p = d / f"{name}{ext}"
                if p.exists():
                    return p

    # 3. First image in root
    for f in sorted(repo_path.iterdir()):
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTENSIONS:
            return f

    return None


def _read_readme_description(repo_path: Path, manifest) -> str | None:
    """Read README.md content for use as cloud description.

    Orca Cloud description field supports markdown. We read the full README
    and use it as-is — the Cloud UI renders it with a markdown renderer.

    Falls back to None if README is missing or empty.
    """
    readme_path = repo_path / manifest.readme
    if not readme_path.exists():
        return None
    try:
        content = readme_path.read_text(encoding="utf-8").strip()
        return content if content else None
    except Exception as exc:
        log.warning("Failed to read %s: %s", readme_path, exc)
        return None


def _parse_changelog_for_version(repo_path: Path, version: str) -> str | None:
    """Parse CHANGELOG.md and extract the section for a specific version.

    Expects Keep-a-Changelog format:
        ## 0.2.0 — 2026-07-26
        ### Changed
        - Something changed

    Returns the full markdown text for that version's section,
    or None if not found / file missing.
    """
    changelog_path = repo_path / "CHANGELOG.md"
    if not changelog_path.exists():
        return None

    try:
        content = changelog_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to read CHANGELOG.md: %s", exc)
        return None

    # Find the section for the requested version
    lines = content.splitlines()
    section_lines: list[str] = []
    in_section = False

    for line in lines:
        # Match ## X.Y.Z (with optional date/suffix)
        if line.startswith("## "):
            if in_section:
                # Hit the next version header — stop
                break
            # Check if this is the version we want
            header_text = line[3:].strip()
            if header_text.startswith(version):
                in_section = True
                # Don't include the version header itself — Cloud shows version separately
                continue
        elif in_section:
            section_lines.append(line)

    if not section_lines:
        return None

    text = "\n".join(section_lines).strip()
    return text if text else None


# ===================== JS API =====================

class ConnectorAPI:
    """pywebview js_api: all methods callable from JavaScript."""

    def __init__(self):
        self._window = None

    def set_window(self, window):
        """Set pywebview window reference for native dialogs."""
        self._window = window

    # ---- Native dialogs ----

    def pick_folder(self) -> str | None:
        """Open native folder picker. Returns selected path or None."""
        import webview
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            return result[0]
        return None

    # ---- Orca Cloud Auth ----

    def get_auth_status(self) -> dict:
        """Get Orca Cloud auth status."""
        return auth_status()

    def login(self) -> dict:
        """Start Orca Cloud OAuth login via Supabase. Blocks until complete."""
        return oauth_login()

    def logout(self) -> dict:
        """Logout from Orca Cloud."""
        auth_logout()
        return {"ok": True}

    # ---- Local repos ----

    def get_repos(self) -> dict:
        """List linked repos with detected metadata."""
        config = load_config()
        return {"repos": [_detect_repo_metadata(Path(r["path"])) for r in config.get("repos", [])]}

    def link_repo(self, path: str) -> dict:
        """Link a local directory as a plugin repo."""
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.is_dir():
            return {"ok": False, "error": f"Not a directory: {repo_path}"}
        _link_repo_path(repo_path)
        return {"ok": True, **_detect_repo_metadata(repo_path)}

    def unlink_repo(self, path: str) -> dict:
        """Unlink a repo from config."""
        config = load_config()
        config["repos"] = [r for r in config.get("repos", []) if r["path"] != path]
        save_config(config)
        return {"ok": True}

    def detect_metadata(self, path: str) -> dict:
        """Detect metadata from a repo path (for manifest form pre-fill)."""
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.exists():
            return {"error": f"Path not found: {repo_path}"}
        return _detect_repo_metadata(repo_path)

    def init_manifest(self, repo_path: str, name: str, import_name: str,
                      version: str = "0.1.0", description: str = "",
                      author: str = "", homepage: str = "", license: str = "MIT",
                      plugin_types: list[str] | None = None, tags: list[str] | None = None) -> dict:
        """Create plugin_manifest.json in a repo."""
        rp = Path(repo_path).expanduser().resolve()
        if not rp.exists():
            return {"ok": False, "error": "Repo not found"}
        manifest = PluginManifest(
            name=name, import_name=import_name, version=version,
            description=description, author=author, homepage=homepage,
            license=license, plugin_types=plugin_types or ["script"], tags=tags or [],
        )
        save_manifest(rp, manifest)
        _link_repo_path(rp)
        return {"ok": True, "path": str(rp), "name": name}

    # ---- Build ----

    def build(self, repo_path: str) -> dict:
        """Build .whl from a plugin repo."""
        rp = Path(repo_path)
        if not rp.exists():
            return {"ok": False, "error": "Repo not found"}
        manifest = _ensure_manifest(rp)
        try:
            whl = build_wheel(rp, manifest.build_script, manifest.dist_dir)
            return {"ok": True, "wheel": whl.name, "size": whl.stat().st_size, "path": str(whl)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---- Publish ----

    def publish(self, repo_path: str, build_first: bool = True) -> dict:
        """Build and publish a plugin to Orca Cloud."""
        rp = Path(repo_path)
        manifest = _ensure_manifest(rp)
        errors = manifest.validate()
        if errors:
            return {"ok": False, "error": f"Manifest errors: {'; '.join(errors)}"}

        # Build
        if build_first:
            try:
                whl_path = build_wheel(rp, manifest.build_script, manifest.dist_dir)
            except Exception as exc:
                return {"ok": False, "error": f"Build failed: {exc}"}
        else:
            whl_path = find_existing_wheel(rp, manifest.dist_dir)
            if not whl_path:
                return {"ok": False, "error": "No .whl found. Build first."}

        # Auth check
        status = auth_status()
        if not status["authenticated"]:
            return {"ok": False, "error": "Not authenticated to Orca Cloud"}

        token = get_access_token()
        api = OrcaCloudAPI(token)
        metadata = manifest.to_cloud_metadata()

        # Enrich description from README.md (full markdown)
        readme_description = _read_readme_description(rp, manifest)
        if readme_description:
            metadata["description"] = readme_description
            log.info("Using README.md as description (%d chars)", len(readme_description))

        # Enrich changelog from CHANGELOG.md (section for current version)
        if manifest.version:
            changelog_text = _parse_changelog_for_version(rp, manifest.version)
            if changelog_text:
                metadata["changelog"] = changelog_text
                log.info("Using CHANGELOG.md for version %s (%d chars)",
                         manifest.version, len(changelog_text))

        # Upload main image if available
        image_path = _find_plugin_image(rp, manifest)
        if image_path:
            try:
                attachment_id = api.upload_media_attachment(image_path)
                metadata["main_image_attachment_id"] = attachment_id
                log.info("Uploaded main image: %s → %s", image_path.name, attachment_id)
            except Exception as exc:
                log.warning("Image upload failed (continuing without image): %s", exc)

        try:
            if manifest.is_published:
                # Check if version changed — Cloud requires version bump for file updates
                cloud_plugin = api.get_plugin(manifest.cloud.uuid)
                cloud_version = cloud_plugin.get("version", "")
                version_changed = cloud_version != manifest.version

                if version_changed:
                    # New version → multipart update with wheel (replace all artifacts)
                    log.info("Version changed (%s → %s), uploading new wheel",
                             cloud_version, manifest.version)
                    result = api.update_plugin(manifest.cloud.uuid, metadata, [whl_path],
                                               keep_artifact_ids=[])
                else:
                    # Same version → metadata-only update (description, changelog, image)
                    log.info("Version unchanged (%s), metadata-only update", manifest.version)
                    result = api.update_plugin(manifest.cloud.uuid, metadata)
                action = "updated"
            else:
                result = api.create_plugin(metadata, [whl_path])
                action = "created"
                manifest.cloud.uuid = result.get("id")
                manifest.cloud.sharing_token = result.get("sharing_token")
                save_manifest(rp, manifest)

            return {"ok": True, "action": action, "uuid": result.get("id"),
                    "sharing_token": result.get("sharing_token"),
                    "cloud_url": f"{ORCA_CLOUD_URL}/p/{result.get('sharing_token', '')}",
                    "version": manifest.version}
        except OrcaCloudError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---- Cloud ----

    def get_cloud_plugins(self) -> dict:
        """List user's published plugins on Orca Cloud."""
        status = auth_status()
        if not status["authenticated"]:
            return {"error": "Not authenticated", "plugins": []}
        token = get_access_token()
        try:
            api = OrcaCloudAPI(token)
            return {"plugins": api.get_my_plugins()}
        except Exception as exc:
            return {"error": str(exc), "plugins": []}
