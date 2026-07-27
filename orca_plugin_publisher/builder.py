"""Plugin .whl builder — runs the repo's build_wheel.py to produce artifacts.

This module handles the "build" step of the publish pipeline:
  plugin_manifest.json → build_wheel.py → dist/*.whl → cloud API upload

Build Process
-------------
::

    1. Read build_script and dist_dir from plugin_manifest.json
       (defaults: build_script="build_wheel.py", dist_dir="dist")

    2. Clean old .whl files in dist_dir
       (prevents uploading stale artifacts)

    3. Run: python build_wheel.py
       in the repo root directory
       (the build script is repo-specific and knows how to package the plugin)

    4. Find the newly built .whl in dist_dir

    5. Return the Path to the .whl file for upload

Build Script Convention
-----------------------
Each plugin repository must contain a ``build_wheel.py`` script in its root.
The script is responsible for:
  - Creating the .whl file with correct metadata
  - Placing the .whl in the dist/ directory
  - Using the plugin's import_name and version for the .whl filename

Example build_wheel.py output:
  dist/bambu_exhaust_enforcer-0.2.0-py3-none-any.whl

The build_wheel.py scripts in our repos (e.g., bambu-exhaust-enforcer) use
the simple wheel format expected by OrcaSlicer's plugin system:
  - Python package as a zip with .whl extension
  - METADATA file with name, version, description
  - Top-level Python package directory

Error Handling
--------------
  - FileNotFoundError if build_wheel.py is missing
  - RuntimeError if the build subprocess fails (non-zero exit)
  - RuntimeError if no .whl is found after successful build

TODO: Add build output streaming to the web UI (currently captured)
TODO: Add build cache — skip rebuild if source hasn't changed
TODO: Support alternative build systems (setup.py, pyproject.toml build)
TODO: Validate the .whl contents (must have __init__.py, METADATA, etc.)
TODO: Add .whl size limit warning (Orca Cloud may reject very large files)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def build_wheel(repo_path: Path, build_script: str = "build_wheel.py", dist_dir: str = "dist") -> Path:
    """Build a .whl file from the plugin repository.

    Executes the build script using the current Python interpreter
    and returns the path to the resulting .whl file.

    Args:
        repo_path:     Absolute path to the plugin git repository root.
        build_script:  Name of the build script to run (relative to repo_path).
                       Default: "build_wheel.py" (from plugin_manifest.json).
        dist_dir:      Output directory for .whl files (relative to repo_path).
                       Default: "dist" (from plugin_manifest.json).

    Returns:
        Path to the built .whl file (absolute).

    Raises:
        FileNotFoundError: If the build script doesn't exist.
        RuntimeError: If the build subprocess fails or no .whl is produced.

    Example:
        >>> whl = build_wheel(Path("/path/to/bambu-exhaust-enforcer"))
        >>> print(whl)
        /path/to/bambu-exhaust-enforcer/dist/bambu_exhaust_enforcer-0.2.0-py3-none-any.whl
    """
    # Verify build script exists
    script_path = repo_path / build_script
    if not script_path.exists():
        raise FileNotFoundError(f"Build script not found: {script_path}")

    # Create dist directory if it doesn't exist
    dist_path = repo_path / dist_dir
    dist_path.mkdir(parents=True, exist_ok=True)

    # Clean old builds to prevent uploading stale artifacts.
    # We only keep the freshly built .whl.
    for old_whl in dist_path.glob("*.whl"):
        old_whl.unlink()
        log.debug("Removed old build: %s", old_whl.name)

    # Run the build script using the current Python interpreter.
    # This ensures the same Python environment is used for building.
    # capture_output=True captures stdout/stderr for error reporting.
    # timeout=60 prevents hanging builds from blocking indefinitely.
    log.info("Building .whl in %s ...", repo_path)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        timeout=60,  # 60-second build timeout
    )

    # Check for build failure
    if result.returncode != 0:
        raise RuntimeError(
            f"Build failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    # Find the built .whl file
    wheels = list(dist_path.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"Build succeeded but no .whl found in {dist_path}")

    # Return the first (should be only) .whl file
    whl = wheels[0]
    log.info("Built: %s (%d bytes)", whl.name, whl.stat().st_size)
    return whl


def find_existing_wheel(repo_path: Path, dist_dir: str = "dist") -> Path | None:
    """Find an existing .whl in the dist directory without building.

    Useful for the "publish without rebuild" workflow — when the user
    has already built the .whl and just wants to upload it.

    Returns the most recently modified .whl file, or None if no .whl exists.

    Args:
        repo_path: Absolute path to the plugin git repository root.
        dist_dir:  Output directory to search (relative to repo_path).

    Returns:
        Path to the most recent .whl, or None.
    """
    dist_path = repo_path / dist_dir
    if not dist_path.exists():
        return None

    # Sort by modification time, newest first
    wheels = sorted(dist_path.glob("*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return wheels[0] if wheels else None
