"""Plugin manifest parser and validator.

This module handles ``plugin_manifest.json`` — the single source of truth
for all plugin metadata that lives in the Git repository alongside the code.

Design Philosophy
-----------------
The manifest file is designed to be:
  1. Human-readable and editable (JSON with clear field names)
  2. Complete — contains everything needed to publish to Orca Cloud
  3. Bidirectional — cloud identifiers (uuid, sharing_token) are written
     back after first publish, enabling subsequent updates

Manifest Schema (plugin_manifest.json)
--------------------------------------
::

    {
      "schema_version": 1,                    # Schema version for future compat

      # === Required Fields ===
      "name": "Bambu Exhaust Enforcer",        # Display name on Orca Cloud
      "import_name": "bambu_exhaust_enforcer", # Python import name (package name)
      "version": "0.2.0",                     # SemVer string
      "description": "Force exhaust fan ON...",# Short description for the listing
      "author": "dnevera",                     # Author name/handle

      # === Optional Fields ===
      "license": "MIT",                        # License identifier
      "homepage": "https://github.com/...",    # GitHub/homepage URL
      "requires_python": ">=3.12",             # Python version constraint
      "platforms": ["py3-none-any"],            # Wheel platform tags
      "type": ["script"],                      # Plugin types for Orca Cloud
                                               #   Valid: "script", "slicing_pipeline",
                                               #          "printer_agent"
      "tags": ["exhaust", "safety", "H2C"],    # Searchable tags
      "compatible_orcaslicer_version": "2.4.2", # Min OrcaSlicer version

      # === Cloud State (written back after first publish) ===
      "cloud": {
        "uuid": "abc123-...",                  # Cloud plugin UUID (set by API)
        "sharing_token": "xyz789",             # Public sharing link token
        "visibility": "public"                 # "public" or "private"
      },

      # === Assets (relative paths within the repo) ===
      "assets": {
        "screenshot": "assets/screenshot.png", # Main plugin image
        "icon": "assets/icon.png"              # Plugin icon (optional)
      },

      "readme": "README.md",                  # README file path (for description sync)

      # === Build Configuration ===
      "build_script": "build_wheel.py",        # Script that builds the .whl
      "dist_dir": "dist",                      # Output directory for .whl files

      # === Changelog (newest first) ===
      "changelog": [
        {
          "version": "0.2.0",
          "date": "2026-07-26",
          "changes": [
            "Added temperature monitoring",
            "Fixed M106 timing issue"
          ]
        }
      ]
    }

Data Flow
---------
::

    plugin_manifest.json
           │
           ▼
    load_manifest(repo_path)         # Parse JSON → PluginManifest dataclass
           │
           ▼
    manifest.validate()              # Check required fields
           │
           ▼
    manifest.to_cloud_metadata()     # Convert to Orca Cloud API format
           │                          #   {"name", "description", "public",
           │                          #    "version", "types", "changelog",
           │                          #    "tags", "compatible_orcaslicer_version"}
           ▼
    cloud_api.create_plugin(metadata, [whl_file])
           │
           ▼
    Response: {"id": "uuid", "sharing_token": "abc"}
           │
           ▼
    save_manifest(repo_path, manifest)   # Write cloud.uuid + sharing_token back

TODO: Add sync for README.md content → cloud description field
      When README changes in git, detect and update cloud description.
TODO: Add sync for assets/screenshot.png → cloud main_image_attachment_id
      Requires discovering the media upload endpoint (not yet found in JS bundle).
TODO: Add version auto-bump helper (semver patch/minor/major)
TODO: Add git tag integration (auto-tag on publish)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The filename we look for in every linked repository
MANIFEST_FILENAME = "plugin_manifest.json"


# =============================================================================
# Data classes — represent the manifest structure in memory
# =============================================================================

@dataclass
class CloudState:
    """Cloud-side identifiers, populated after first publish.

    These fields are written back to plugin_manifest.json after the first
    successful ``POST /api/v1/plugins`` call. They enable subsequent
    ``PATCH`` calls to update the same plugin without creating duplicates.

    Fields:
        uuid:           The plugin's UUID on Orca Cloud (from API response "id").
        sharing_token:  The public sharing link token (from API response "sharing_token").
                        Used to construct: https://cloud.orcaslicer.com/p/{sharing_token}
        visibility:     "public" (discoverable in catalog) or "private" (sharing link only).
    """

    uuid: str | None = None
    sharing_token: str | None = None
    visibility: str = "public"  # "public" | "private"

    @property
    def is_published(self) -> bool:
        """Check if the plugin has been published to Orca Cloud at least once."""
        return self.uuid is not None and len(self.uuid) > 0


@dataclass
class ChangelogEntry:
    """A single changelog entry for one version release.

    Fields:
        version:  SemVer string for this release (e.g., "0.2.0").
        date:     ISO date string (e.g., "2026-07-26").
        changes:  List of human-readable change descriptions.
    """

    version: str
    date: str
    changes: list[str] = field(default_factory=list)


@dataclass
class PluginManifest:
    """Full plugin manifest — everything needed to publish to Orca Cloud.

    This dataclass mirrors the plugin_manifest.json schema exactly.
    All fields map 1:1 to JSON keys (with minor naming differences
    handled in _parse_manifest/_serialize_manifest).

    Usage:
        manifest = load_manifest(Path("/path/to/repo"))
        errors = manifest.validate()
        if not errors:
            metadata = manifest.to_cloud_metadata()
            api.create_plugin(metadata, [whl_file])
    """

    # ---- Required fields ----
    # These MUST be filled in for a valid manifest

    name: str = ""
    """Display name on Orca Cloud (e.g., "Bambu Exhaust Enforcer")."""

    import_name: str = ""
    """Python import name / package name (e.g., "bambu_exhaust_enforcer").
    Used by build_wheel.py to construct the .whl filename."""

    version: str = "0.1.0"
    """SemVer version string. Must be bumped for each cloud update that
    includes new .whl files."""

    description: str = ""
    """Short description shown in the Orca Cloud plugin listing.
    TODO: Support syncing from README.md for longer descriptions."""

    author: str = ""
    """Author name or GitHub handle."""

    # ---- Optional fields ----

    license: str = "MIT"
    """SPDX license identifier."""

    homepage: str = ""
    """URL to the plugin's homepage or GitHub repository.
    TODO: Auto-populate from git remote URL if empty."""

    requires_python: str = ">=3.12"
    """Python version constraint (PEP 440 format)."""

    platforms: list[str] = field(default_factory=lambda: ["py3-none-any"])
    """Wheel platform tags. Pure Python plugins use "py3-none-any"."""

    plugin_types: list[str] = field(default_factory=list)
    """Plugin capability types for Orca Cloud categorization.
    Valid values (from Orca Cloud JS bundle):
      - "script"            — Script capability
      - "slicing_pipeline"  — SlicingPipeline capability
      - "printer_agent"     — PrinterAgent capability
    TODO: Validate against known types in validate()."""

    tags: list[str] = field(default_factory=list)
    """Searchable tags for the Orca Cloud catalog."""

    compatible_orcaslicer_version: str | None = None
    """Minimum compatible OrcaSlicer version (e.g., "2.4.2").
    Orca Cloud UI shows this as "{version} and later".
    Valid values from JS bundle: 1.8.0, 1.8.1, 1.9.0, ..., 2.4.2."""

    # ---- Cloud state (written back after first publish) ----

    cloud: CloudState = field(default_factory=CloudState)
    """Cloud-side identifiers. Auto-populated after first publish."""

    # ---- Assets (relative paths within the repo) ----

    screenshot: str | None = None
    """Relative path to the main screenshot image.
    TODO: Upload this as main_image_attachment_id on Orca Cloud.
    The upload mechanism hasn't been fully reverse-engineered yet —
    may require Supabase Storage direct upload or a separate endpoint."""

    icon: str | None = None
    """Relative path to the plugin icon.
    TODO: Determine if Orca Cloud supports separate icon uploads."""

    readme: str = "README.md"
    """Path to README file (relative to repo root).
    TODO: Sync README content to cloud description on publish/update."""

    # ---- Changelog ----

    changelog: list[ChangelogEntry] = field(default_factory=list)
    """Version changelog, newest entries first.
    The latest entry's changes are sent as the "changelog" field
    to the Orca Cloud API on publish."""

    # ---- Build configuration ----

    build_script: str = "build_wheel.py"
    """Name of the build script to run (relative to repo root).
    This script should produce a .whl file in dist_dir."""

    dist_dir: str = "dist"
    """Output directory for built .whl files (relative to repo root)."""

    # ---- Computed properties ----

    @property
    def is_published(self) -> bool:
        """Check if the plugin has been published to Orca Cloud."""
        return self.cloud.is_published

    @property
    def cloud_url(self) -> str | None:
        """Get the full Orca Cloud sharing URL (if published).

        Format: https://cloud.orcaslicer.com/p/{sharing_token}
        """
        if self.cloud.sharing_token:
            from .config import ORCA_CLOUD_URL
            return f"{ORCA_CLOUD_URL}/p/{self.cloud.sharing_token}"
        return None

    @property
    def latest_changelog_text(self) -> str:
        """Format the latest changelog entry for Orca Cloud API.

        Returns a markdown-formatted list of changes, e.g.:
            "- Added temperature monitoring\\n- Fixed M106 timing issue"

        The Orca Cloud API accepts this as the "changelog" string field.
        If you bump the version AND change files, the cloud creates a new release.
        If you keep the same version, it edits the existing release note.
        """
        if not self.changelog:
            return ""
        entry = self.changelog[0]
        return "\n".join(f"- {change}" for change in entry.changes)

    # ---- Conversion to Orca Cloud format ----

    def to_cloud_metadata(self, *, include_files: bool = False) -> dict[str, Any]:
        """Convert manifest to the JSON metadata format expected by Orca Cloud API.

        This produces the "metadata" JSON blob that goes into the FormData
        when creating or updating a plugin.

        Orca Cloud create/update API expects:
          {
            "name": str,                          # required
            "description": str,                   # required
            "public": bool,                       # required
            "version": str,                       # optional but recommended
            "types": list[str],                   # optional
            "changelog": str,                     # optional — latest changes text
            "tags": list[str],                    # optional
            "compatible_orcaslicer_version": str,  # optional
            "main_image_attachment_id": str,       # optional — uploaded image UUID
            "attachment_ids": list[str],           # optional — additional media UUIDs
            "keep_artifact_ids": list[str],       # optional — (update only) keep these files
          }

        TODO: Add main_image_attachment_id when we implement screenshot upload
        TODO: Add attachment_ids when we implement media sync
        """
        meta: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "public": self.cloud.visibility == "public",
        }
        if self.version:
            meta["version"] = self.version
        if self.plugin_types:
            meta["types"] = self.plugin_types
        if self.latest_changelog_text:
            meta["changelog"] = self.latest_changelog_text
        if self.tags:
            meta["tags"] = self.tags
        if self.compatible_orcaslicer_version:
            meta["compatible_orcaslicer_version"] = self.compatible_orcaslicer_version
        return meta

    # ---- Validation ----

    def validate(self) -> list[str]:
        """Validate the manifest and return a list of errors.

        Returns:
            Empty list if valid, otherwise list of error message strings.

        TODO: Add validation for plugin_types (must be from known set)
        TODO: Add validation for version format (must be valid SemVer)
        TODO: Warn if changelog is empty (recommended but not required)
        """
        errors: list[str] = []
        if not self.name.strip():
            errors.append("'name' is required")
        if not self.import_name.strip():
            errors.append("'import_name' is required")
        if not self.version.strip():
            errors.append("'version' is required")
        if not self.description.strip():
            errors.append("'description' is required")
        if not self.author.strip():
            errors.append("'author' is required")
        return errors


# =============================================================================
# File I/O — load and save plugin_manifest.json
# =============================================================================

def load_manifest(repo_path: Path) -> PluginManifest:
    """Load a plugin manifest from a repository directory.

    Looks for ``plugin_manifest.json`` in the root of the given directory.

    Args:
        repo_path: Absolute path to the plugin git repository.

    Returns:
        Parsed PluginManifest instance.

    Raises:
        FileNotFoundError: If plugin_manifest.json doesn't exist.
    """
    manifest_path = repo_path / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"No {MANIFEST_FILENAME} found in {repo_path}")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return _parse_manifest(data)


def save_manifest(repo_path: Path, manifest: PluginManifest) -> None:
    """Save a plugin manifest back to its repository.

    This is called after first publish to write back cloud identifiers
    (uuid, sharing_token) so subsequent publishes become updates.

    Args:
        repo_path: Absolute path to the plugin git repository.
        manifest: The manifest to serialize and save.
    """
    manifest_path = repo_path / MANIFEST_FILENAME
    data = _serialize_manifest(manifest)
    manifest_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Saved manifest to %s", manifest_path)


# =============================================================================
# JSON ↔ PluginManifest conversion
# =============================================================================

def _parse_manifest(data: dict[str, Any]) -> PluginManifest:
    """Parse a raw JSON dict into a PluginManifest dataclass.

    Handles the following naming quirks:
      - "type" in JSON → plugin_types in Python (avoids shadowing builtin)
      - "assets.screenshot" in JSON → screenshot in Python (flattened)
      - "cloud" in JSON → CloudState dataclass
      - "changelog" in JSON → list of ChangelogEntry dataclasses
    """
    # Parse cloud state sub-object
    cloud_data = data.get("cloud", {})
    cloud = CloudState(
        uuid=cloud_data.get("uuid"),
        sharing_token=cloud_data.get("sharing_token"),
        visibility=cloud_data.get("visibility", "public"),
    )

    # Parse changelog entries (list of dicts → list of ChangelogEntry)
    changelog_entries = []
    for entry in data.get("changelog", []):
        changelog_entries.append(ChangelogEntry(
            version=entry.get("version", ""),
            date=entry.get("date", ""),
            changes=entry.get("changes", []),
        ))

    # Construct the manifest, using JSON field names with defaults
    return PluginManifest(
        name=data.get("name", ""),
        import_name=data.get("import_name", ""),
        version=data.get("version", "0.1.0"),
        description=data.get("description", ""),
        author=data.get("author", ""),
        license=data.get("license", "MIT"),
        homepage=data.get("homepage", ""),
        requires_python=data.get("requires_python", ">=3.12"),
        platforms=data.get("platforms", ["py3-none-any"]),
        # NOTE: JSON uses "type" (Orca Cloud convention), Python uses plugin_types
        plugin_types=data.get("type", data.get("plugin_types", [])),
        tags=data.get("tags", []),
        compatible_orcaslicer_version=data.get("compatible_orcaslicer_version"),
        cloud=cloud,
        # Assets are nested in JSON, flattened in Python
        screenshot=data.get("assets", {}).get("screenshot"),
        icon=data.get("assets", {}).get("icon"),
        readme=data.get("readme", "README.md"),
        changelog=changelog_entries,
        build_script=data.get("build_script", "build_wheel.py"),
        dist_dir=data.get("dist_dir", "dist"),
    )


def _serialize_manifest(manifest: PluginManifest) -> dict[str, Any]:
    """Serialize a PluginManifest to a JSON-friendly dict.

    Produces the canonical plugin_manifest.json structure with:
      - schema_version: 1 (for future compatibility)
      - Nested "cloud" and "assets" sub-objects
      - "type" key (not "plugin_types") for Orca Cloud compatibility
      - Changelog entries as plain dicts
    """
    data: dict[str, Any] = {
        "schema_version": 1,
        "name": manifest.name,
        "import_name": manifest.import_name,
        "version": manifest.version,
        "description": manifest.description,
        "author": manifest.author,
        "license": manifest.license,
        "homepage": manifest.homepage,
        "requires_python": manifest.requires_python,
        "platforms": manifest.platforms,
        # Use "type" in JSON (matches Orca Cloud API convention)
        "type": manifest.plugin_types,
        "tags": manifest.tags,
    }

    if manifest.compatible_orcaslicer_version:
        data["compatible_orcaslicer_version"] = manifest.compatible_orcaslicer_version

    # Cloud state — always present (may have null values before first publish)
    data["cloud"] = {
        "uuid": manifest.cloud.uuid,
        "sharing_token": manifest.cloud.sharing_token,
        "visibility": manifest.cloud.visibility,
    }

    # Assets — only include if at least one asset path is set
    assets: dict[str, str] = {}
    if manifest.screenshot:
        assets["screenshot"] = manifest.screenshot
    if manifest.icon:
        assets["icon"] = manifest.icon
    if assets:
        data["assets"] = assets

    data["readme"] = manifest.readme
    data["build_script"] = manifest.build_script
    data["dist_dir"] = manifest.dist_dir

    # Changelog — serialize ChangelogEntry dataclasses to plain dicts
    data["changelog"] = [
        {
            "version": entry.version,
            "date": entry.date,
            "changes": entry.changes,
        }
        for entry in manifest.changelog
    ]

    return data
