# Orca Plugin Publisher

Native desktop app to publish and manage [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) plugins on [Orca Cloud](https://cloud.orcaslicer.com).

Built with [pywebview](https://pywebview.flowrl.com/) — Python backend called directly from JS, no HTTP server needed.

## Features

- **Link local repos** — point to any git directory containing an OrcaSlicer plugin
- **Auto-detect metadata** — reads `plugin_manifest.json`, `pyproject.toml`, `build_wheel.py`
- **Build `.whl` artifacts** — runs each repo's `build_wheel.py` to produce wheel packages
- **Publish to Orca Cloud** — create or update plugins via the Cloud REST API
- **Smart updates** — metadata-only PATCH when version unchanged, full re-upload on version bump
- **README → Description** — full markdown README becomes the cloud plugin description
- **Image upload** — local `![alt](path.png)` in README are uploaded to cloud and URLs replaced
- **CHANGELOG → Release notes** — extracts version-specific section from `CHANGELOG.md`
- **OAuth authentication** — Supabase PKCE flow via system browser (GitHub, Google, Apple, Discord)

## Quick Start

```bash
# Clone
git clone git@github.com:dnevera/orca-plugin-publisher.git
cd orca-plugin-publisher

# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Run
python -m orca_plugin_publisher.desktop
```

A native window will open with the publisher dashboard.

## Plugin Repository Setup

Each plugin repo needs a `plugin_manifest.json` in its root:

```json
{
  "name": "My Plugin",
  "version": "1.0.0",
  "description": "Short description",
  "readme": "README.md",
  "image": "assets/screenshot.png",
  "build_script": "build_wheel.py",
  "dist_dir": "dist",
  "types": ["script"],
  "tags": ["bambu", "orca"]
}
```

The publisher will:
1. Run `build_wheel.py` → produce `dist/*.whl`
2. Read `README.md` → use as cloud description (with image upload)
3. Parse `CHANGELOG.md` → extract release notes for current version
4. Upload `image` → set as plugin thumbnail

## Architecture

```
┌─────────────────────────────────┐
│  pywebview (native window)      │
│  static/index.html (SPA)        │
└──────────┬──────────────────────┘
           │ window.pywebview.api.*
┌──────────▼──────────────────────┐
│  ConnectorAPI (api.py)          │
│  ├── manifest.py  — manifest IO │
│  ├── builder.py   — .whl build  │
│  ├── cloud_api.py — REST client  │
│  ├── auth.py      — OAuth PKCE  │
│  ├── github_api.py — GitHub API  │
│  └── config.py    — settings     │
└─────────────────────────────────┘
```

## Publish Pipeline

```
plugin_manifest.json
    → Build .whl (build_wheel.py)
    → Read README.md → upload images → cloud URLs
    → Parse CHANGELOG.md → version section
    → Check cloud version:
        same version → metadata-only PATCH
        new version  → multipart PATCH with .whl
    → Update plugin on Orca Cloud
```

## Requirements

- Python ≥ 3.12
- macOS / Windows / Linux (pywebview)
- Dependencies: `httpx`, `pywebview`, `keyring`, `python-dotenv`

## License

MIT
