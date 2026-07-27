"""pywebview js_api — Python backend called directly from JS.

All methods are callable from JavaScript via:
    const result = await window.pywebview.api.method_name(args);

Methods run in a background thread (pywebview handles this),
so blocking I/O is fine.
"""

from __future__ import annotations

import hashlib
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


def _file_md5(path: Path) -> str:
    """Compute MD5 hex digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _bytes_md5(data: bytes) -> str:
    """Compute MD5 hex digest of raw bytes."""
    return hashlib.md5(data).hexdigest()


def _collect_readme_image_urls(cloud_description: str) -> dict[str, str]:
    """Extract {alt_text: cloud_url} from cloud description markdown.

    Cloud rewrites local paths like rc/compare.png to
    https://api.orcaslicer.com/api/v1/bundles/media/{uuid}/content.
    This returns a mapping from alt text to cloud URL for matching.
    """
    urls: dict[str, str] = {}
    for _, alt_text, img_url in _MD_IMAGE_RE.findall(cloud_description):
        if img_url.startswith(("http://", "https://")):
            urls[alt_text] = img_url
    return urls


def _collect_local_images(
    repo_path: Path, manifest: "PluginManifest"
) -> dict[str, tuple[str, str]]:
    """Collect local README images as {alt_text: (relative_path, md5_hex)}.

    Scans README for local ![alt](path) references. Returns mapping
    from alt text to (relative_path, hash) for matching with cloud images.
    """
    images: dict[str, tuple[str, str]] = {}
    readme_path = repo_path / manifest.readme
    if readme_path.exists():
        try:
            text = readme_path.read_text(encoding="utf-8")
            for _, alt_text, img_path in _MD_IMAGE_RE.findall(text):
                if img_path.startswith(("http://", "https://", "//")):
                    continue
                local_file = (repo_path / img_path).resolve()
                if local_file.exists() and local_file.suffix.lower() in _IMAGE_EXTENSIONS:
                    images[alt_text] = (str(Path(img_path)), _file_md5(local_file))
        except Exception:
            pass
    return images


# Regex: ![alt text](path) — capture the full match, alt, and path
_MD_IMAGE_RE = re.compile(r"(!\[([^\]]*)\]\(([^)]+)\))")

# Regex: [![alt](badge_url)](link_url) — image-inside-link (badges)
_MD_BADGE_RE = re.compile(r"\[!\[([^\]]*)\]\([^)]+\)\]\(([^)]+)\)")


def _strip_md_badges(text: str) -> str:
    """Convert markdown badges [![alt](badge)](link) → [alt](link).

    Cloud markdown renderers typically can't fetch external images
    (e.g. shields.io badges). Replace them with plain text links.
    """
    return _MD_BADGE_RE.sub(r"[\1](\2)", text)


def _upload_readme_images(
    description: str, repo_path: Path, api: "OrcaCloudAPI"
) -> tuple[str, list[str], dict[Path, str]]:
    """Upload local images referenced in markdown and replace paths with cloud URLs.

    Finds all ![alt](local/path.png) patterns where the path is a local file
    (not http/https). Uploads each to Orca Cloud media and replaces the path
    with the permanent content URL.

    The returned attachment_ids MUST be included in the plugin's metadata
    (attachment_ids field) to bind them to the plugin — otherwise the unsigned
    content URLs will return 404.

    Args:
        description: Markdown text (README content).
        repo_path: Root of the plugin repo (for resolving relative paths).
        api: Authenticated OrcaCloudAPI instance.

    Returns:
        Tuple of (modified markdown, attachment_ids, uploaded_files).
        uploaded_files maps resolved Path → attachment_id for dedupe with main image.
    """
    matches = _MD_IMAGE_RE.findall(description)
    if not matches:
        return description, [], {}

    result = description
    uploaded: dict[str, str] = {}  # relative_path → cloud_url (dedupe)
    uploaded_files: dict[Path, str] = {}  # resolved_path → attachment_id (for external dedupe)
    attachment_ids: list[str] = []

    for full_match, alt_text, img_path in matches:
        # Skip external URLs
        if img_path.startswith(("http://", "https://", "//")):
            continue

        # Dedupe — same local path may appear multiple times
        if img_path in uploaded:
            cloud_url = uploaded[img_path]
            replacement = f"![{alt_text}]({cloud_url})"
            result = result.replace(full_match, replacement, 1)
            continue

        # Resolve relative to repo root
        local_file = (repo_path / img_path).resolve()
        if not local_file.exists():
            log.warning("README image not found, skipping: %s", local_file)
            continue

        if local_file.suffix.lower() not in _IMAGE_EXTENSIONS:
            log.warning("README image unsupported format, skipping: %s", local_file)
            continue

        try:
            attachment_id = api.upload_media_attachment(local_file)
            cloud_url = f"{api._base}/api/v1/bundles/media/{attachment_id}/content"
            uploaded[img_path] = cloud_url
            uploaded_files[local_file] = attachment_id
            attachment_ids.append(attachment_id)

            replacement = f"![{alt_text}]({cloud_url})"
            result = result.replace(full_match, replacement, 1)
            log.info("README image uploaded: %s → %s", img_path, attachment_id)
        except Exception as exc:
            log.warning("README image upload failed, keeping original: %s — %s",
                        img_path, exc)

    return result, attachment_ids, uploaded_files

# Regex for normalization only — replaces ![alt](any_url) with ![alt](IMAGE)
_MD_IMG_NORM_RE = re.compile(r'!\[([^\]]*)\]\([^)]+\)')


def _normalize_md_for_compare(text: str) -> str:
    """Normalize markdown for comparison, eliminating false diffs.

    Cloud rewrites local image paths (e.g. ``rc/dashboard.png``) to CDN URLs
    (e.g. ``https://api.orcaslicer.com/api/v1/bundles/media/.../content``).
    This replaces all ``![alt](url)`` with ``![alt](IMAGE)`` so only real
    content changes are detected.
    """
    if not text:
        return ""
    # Strip badges [![alt](badge_url)](link) → [alt](link) before comparing
    normalized = _strip_md_badges(text)
    normalized = _MD_IMG_NORM_RE.sub(r'![\1](IMAGE)', normalized)
    # Normalize trailing whitespace per line
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines())
    return normalized.strip()


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


# ===================== Cloud sync status =====================

def _normalize_plugin_name(name: str) -> str:
    """Normalize a plugin name for slug-based comparison.

    Handles: 'orca-slice-heating-inspector' ↔ 'Slice Heating Inspector'
    """
    s = name.strip().lower()
    s = s.replace("-", " ").replace("_", " ")
    for prefix in ("orca ", ):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return " ".join(s.split())


def _sync_cloud_status(repos: list[dict[str, Any]]) -> None:
    """Sync local repos with cloud: auto-link unlinked, compute diff status.

    For each repo:
    - If uuid=null but name matches a cloud plugin → auto-link (write uuid back)
    - If linked → compare local vs cloud version, description, etc.
    - Adds cloud_version, cloud_sync_status, cloud_changes to repo_info

    cloud_sync_status values:
    - "up_to_date" — local matches cloud
    - "version_ahead" — local version > cloud version
    - "metadata_changed" — same version but description/changelog differ
    - "new" — not yet published
    """
    if not any(r.get("has_manifest") for r in repos):
        return

    status = auth_status()
    if not status.get("authenticated"):
        return

    try:
        token = get_access_token()
        api = OrcaCloudAPI(token)
        cloud_plugins = api.get_my_plugins()
    except Exception as exc:
        log.warning("Cloud sync: failed to fetch cloud plugins: %s", exc)
        return

    # Build lookup: uuid → cloud_plugin, normalized_name → cloud_plugin
    cloud_by_id: dict[str, dict] = {}
    cloud_by_slug: dict[str, dict] = {}
    for cp in cloud_plugins:
        cloud_by_id[cp["id"]] = cp
        cp_name = cp.get("name", "")
        if cp_name:
            cloud_by_slug[_normalize_plugin_name(cp_name)] = cp

    for repo_info in repos:
        if not repo_info.get("has_manifest"):
            continue

        cloud_match = None

        if repo_info.get("is_published") and repo_info.get("cloud_uuid"):
            # Already linked — look up by UUID
            cloud_match = cloud_by_id.get(repo_info["cloud_uuid"])
        else:
            # Not linked — try name match for auto-link
            repo_name = repo_info.get("name", "")
            if repo_name:
                cloud_match = cloud_by_slug.get(_normalize_plugin_name(repo_name))

            if cloud_match:
                # Auto-link: write uuid + sharing_token back to manifest
                repo_path = Path(repo_info["path"])
                try:
                    manifest = load_manifest(repo_path)
                    manifest.cloud.uuid = cloud_match["id"]
                    manifest.cloud.sharing_token = cloud_match.get("sharing_token")
                    save_manifest(repo_path, manifest)
                    repo_info["is_published"] = True
                    repo_info["cloud_uuid"] = cloud_match["id"]
                    repo_info["cloud_url"] = (
                        f"{ORCA_CLOUD_URL}/p/{cloud_match.get('sharing_token', '')}"
                    )
                    log.info("Auto-linked '%s' → %s", repo_name, cloud_match["id"])
                except Exception as exc:
                    log.warning("Auto-link failed for %s: %s", repo_name, exc)

        if not cloud_match:
            repo_info["cloud_sync_status"] = "new"
            continue

        # Compare local vs cloud
        cloud_ver = cloud_match.get("version", "")
        local_ver = repo_info.get("version", "")
        repo_info["cloud_version"] = cloud_ver

        if local_ver and cloud_ver and local_ver != cloud_ver:
            repo_info["cloud_sync_status"] = "version_ahead"
            repo_info["cloud_changes"] = [f"Version: {cloud_ver} → {local_ver}"]
        else:
            # Same version — check metadata differences
            changes = []

            # Compare README content (normalized to ignore image URL rewrites)
            repo_path = Path(repo_info["path"])
            manifest_obj = None
            try:
                manifest_obj = load_manifest(repo_path)
            except Exception:
                pass
            readme_file = repo_path / (manifest_obj.readme if manifest_obj else "README.md")
            if readme_file.exists():
                try:
                    local_readme = readme_file.read_text(encoding="utf-8").strip()
                    cloud_desc = str(cloud_match.get("description") or "").strip()
                    if _normalize_md_for_compare(local_readme) != _normalize_md_for_compare(cloud_desc):
                        changes.append("README updated")
                except Exception:
                    pass

            if changes:
                repo_info["cloud_sync_status"] = "metadata_changed"
                repo_info["cloud_changes"] = changes
            else:
                repo_info["cloud_sync_status"] = "up_to_date"


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
        """List linked repos with detected metadata and cloud sync status.

        Auto-links unlinked repos and computes diff vs cloud.
        """
        config = load_config()
        repos = [_detect_repo_metadata(Path(r["path"])) for r in config.get("repos", [])]
        _sync_cloud_status(repos)
        return {"repos": repos}

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

    def get_manifest(self, repo_path: str) -> dict:
        """Read current manifest fields for editing."""
        rp = Path(repo_path).expanduser().resolve()
        try:
            m = _ensure_manifest(rp)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True, "path": str(rp),
            "name": m.name, "import_name": m.import_name, "version": m.version,
            "description": m.description, "author": m.author,
            "homepage": m.homepage, "license": m.license,
        }

    def update_manifest(self, repo_path: str, updates: dict) -> dict:
        """Update specific fields in an existing plugin_manifest.json.

        Args:
            repo_path: Path to the plugin repo.
            updates: Dict of field names → new values to patch.
                     Supported: name, version, description, author, homepage, license.
        """
        rp = Path(repo_path).expanduser().resolve()
        try:
            manifest = _ensure_manifest(rp)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        allowed = {"name", "version", "description", "author", "homepage", "license", "import_name"}
        for key, val in updates.items():
            if key in allowed and isinstance(val, str):
                setattr(manifest, key, val.strip())
        save_manifest(rp, manifest)
        errors = manifest.validate()
        return {"ok": True, "name": manifest.name, "errors": errors}

    # ---- Diff ----

    def get_plugin_diff(self, repo_path: str) -> dict:
        """Get detailed diff between local plugin and cloud version.

        Returns a list of diff items, each with:
        - field: human-readable field name
        - local: local value
        - cloud: cloud value
        - changed: bool
        """
        rp = Path(repo_path)
        if not rp.exists():
            return {"ok": False, "error": "Repo not found"}

        try:
            manifest = load_manifest(rp)
        except Exception as exc:
            return {"ok": False, "error": f"Manifest error: {exc}"}

        if not manifest.is_published:
            return {"ok": True, "status": "new", "diffs": [
                {"field": "Status", "local": "New plugin", "cloud": "—", "changed": True}
            ]}

        status = auth_status()
        if not status.get("authenticated"):
            return {"ok": False, "error": "Not authenticated"}

        try:
            token = get_access_token()
            api = OrcaCloudAPI(token)
            cloud = api.get_plugin(manifest.cloud.uuid)
        except Exception as exc:
            return {"ok": False, "error": f"Cloud fetch failed: {exc}"}

        diffs = []
        cloud_ver = cloud.get("version", "")
        local_ver = manifest.version or ""
        diffs.append({
            "field": "Version",
            "local": local_ver,
            "cloud": cloud_ver,
            "changed": local_ver != cloud_ver,
        })

        # README / description
        local_readme = ""
        readme_path = rp / manifest.readme
        if readme_path.exists():
            try:
                local_readme = readme_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        raw_desc = cloud.get("description") or ""
        cloud_desc = str(raw_desc).strip() if not isinstance(raw_desc, (list, dict)) else ""
        # Normalize before comparing: cloud rewrites local image paths to cloud URLs,
        # which causes false diffs. Replace ![alt](any_url) → ![alt](IMAGE) on both sides.
        readme_changed = _normalize_md_for_compare(local_readme) != _normalize_md_for_compare(cloud_desc)
        # Show length comparison for readability
        diff_entry = {
            "field": "Description (README)",
            "local": f"{len(local_readme)} chars" if local_readme else "—",
            "cloud": f"{len(cloud_desc)} chars" if cloud_desc else "—",
            "changed": readme_changed,
            "detail": "Content differs" if readme_changed and local_readme and cloud_desc else None,
        }
        if readme_changed and (local_readme or cloud_desc):
            diff_entry["local_content"] = local_readme[:5000]
            diff_entry["cloud_content"] = cloud_desc[:5000]
        diffs.append(diff_entry)

        # Changelog — cloud returns list of {id, version, changelog: str} dicts
        local_changelog = ""
        if manifest.version:
            local_changelog = _parse_changelog_for_version(rp, manifest.version) or ""
        raw_cl = cloud.get("changelog")
        cloud_changelog = ""
        if isinstance(raw_cl, list) and raw_cl:
            # Extract text from latest changelog entry
            latest = raw_cl[0]
            cloud_changelog = (latest.get("changelog", "") if isinstance(latest, dict) else str(latest)).strip()
        elif isinstance(raw_cl, str):
            cloud_changelog = raw_cl.strip()
        cl_changed = local_changelog != cloud_changelog
        cl_entry = {
            "field": "Changelog",
            "local": f"{len(local_changelog)} chars" if local_changelog else "—",
            "cloud": f"{len(cloud_changelog)} chars" if cloud_changelog else "—",
            "changed": cl_changed,
        }
        if cl_changed and (local_changelog or cloud_changelog):
            cl_entry["local_content"] = local_changelog[:3000]
            cl_entry["cloud_content"] = cloud_changelog[:3000]
        diffs.append(cl_entry)

        # Images — live hash comparison (download cloud images and compare)
        image_path = _find_plugin_image(rp, manifest)
        cloud_main_image = cloud.get("main_image")
        cloud_has_image = bool(cloud_main_image)
        images_changed = False
        changed_images: list[str] = []

        # Main plugin image: compare hashes by downloading cloud version
        if image_path and cloud_has_image:
            try:
                # cloud_main_image may be a dict with 'id' or a string UUID
                main_img_id = (cloud_main_image.get("id")
                               if isinstance(cloud_main_image, dict)
                               else str(cloud_main_image))
                if main_img_id:
                    cloud_bytes = api.download_media_content(main_img_id)
                    local_hash = _file_md5(image_path)
                    cloud_hash = _bytes_md5(cloud_bytes)
                    if local_hash != cloud_hash:
                        images_changed = True
                        changed_images.append(image_path.name)
            except Exception as exc:
                log.warning("Main image hash compare failed: %s", exc)
        elif bool(image_path) != cloud_has_image:
            images_changed = True

        # README inline images: match by alt text, download cloud, compare hashes
        cloud_desc = str(cloud.get("description") or "")
        cloud_img_urls = _collect_readme_image_urls(cloud_desc)
        local_images = _collect_local_images(rp, manifest)
        for alt_text, (rel_path, local_hash) in local_images.items():
            cloud_url = cloud_img_urls.get(alt_text)
            if not cloud_url:
                continue
            try:
                # Extract attachment_id from URL: .../media/{uuid}/content
                parts = cloud_url.rstrip("/").split("/")
                content_idx = parts.index("content")
                att_id = parts[content_idx - 1]
                cloud_bytes = api.download_media_content(att_id)
                if local_hash != _bytes_md5(cloud_bytes):
                    images_changed = True
                    changed_images.append(rel_path)
            except Exception as exc:
                log.warning("Inline image hash compare failed for %s: %s", rel_path, exc)

        img_detail = None
        if changed_images:
            img_detail = f"Changed: {', '.join(changed_images)}"
        diffs.append({
            "field": "Images",
            "local": image_path.name if image_path else "—",
            "cloud": "✓" if cloud_has_image else "—",
            "changed": images_changed,
            "detail": img_detail,
        })

        # Wheel file
        whl = find_existing_wheel(rp, manifest.dist_dir)
        cloud_artifacts = cloud.get("artifacts", [])
        cloud_whl_names = [a.get("filename", "") for a in cloud_artifacts
                           if a.get("filename", "").endswith(".whl")]
        diffs.append({
            "field": "Wheel (.whl)",
            "local": whl.name if whl else "—",
            "cloud": cloud_whl_names[0] if cloud_whl_names else "—",
            "changed": (whl.name if whl else "") != (cloud_whl_names[0] if cloud_whl_names else ""),
        })

        overall_status = "up_to_date"
        if local_ver != cloud_ver:
            overall_status = "version_ahead"
        elif any(d["changed"] for d in diffs):
            overall_status = "metadata_changed"

        return {"ok": True, "status": overall_status, "diffs": diffs}

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
        readme_uploaded_files: dict[Path, str] = {}  # for main image dedupe
        if readme_description:
            # Strip badges ([![alt](badge_url)](link)) → plain links
            readme_description = _strip_md_badges(readme_description)
            # Upload local images and replace paths with cloud URLs
            readme_description, img_attachment_ids, readme_uploaded_files = (
                _upload_readme_images(readme_description, rp, api)
            )
            metadata["description"] = readme_description
            if img_attachment_ids:
                metadata.setdefault("attachment_ids", []).extend(img_attachment_ids)
                log.info("Bound %d README images to plugin", len(img_attachment_ids))
            log.info("Using README.md as description (%d chars)", len(readme_description))

        # Enrich changelog from CHANGELOG.md (section for current version)
        if manifest.version:
            changelog_text = _parse_changelog_for_version(rp, manifest.version)
            if changelog_text:
                metadata["changelog"] = changelog_text
                log.info("Using CHANGELOG.md for version %s (%d chars)",
                         manifest.version, len(changelog_text))

        # Upload main image if available (dedupe with README images)
        image_path = _find_plugin_image(rp, manifest)
        if image_path:
            resolved = image_path.resolve()
            if resolved in readme_uploaded_files:
                # Already uploaded as README inline image — reuse attachment_id
                metadata["main_image_attachment_id"] = readme_uploaded_files[resolved]
                log.info("Main image deduped with README: %s → %s",
                         image_path.name, readme_uploaded_files[resolved])
            else:
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
