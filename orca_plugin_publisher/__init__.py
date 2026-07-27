"""orca-plugin-publisher — Publish OrcaSlicer plugins from GitHub to Orca Cloud.

This package provides a local web application (localhost:8420) that acts as a
bridge between Git repositories containing OrcaSlicer plugins and the Orca Cloud
plugin marketplace (cloud.orcaslicer.com).

Architecture Overview
---------------------
The tool follows a "GitHub as source of truth" philosophy:

    GitHub Repo                          Orca Cloud
    ├── plugin_manifest.json   ──sync──► Plugin listing
    ├── source code            ──build─► .whl artifact
    ├── README.md              ──sync──► Description
    └── assets/screenshot.png  ──sync──► Plugin media

Modules
-------
- config.py     — Paths, constants, secure credential storage (keyring)
- manifest.py   — plugin_manifest.json parser and serializer
- auth.py       — Browser-based OAuth to Orca Cloud (Supabase PKCE)
- cloud_api.py  — REST API client for Orca Cloud (create/update/delete plugins)
- builder.py    — .whl artifact builder (runs build_wheel.py)
- app.py        — FastAPI web server with UI and API endpoints

API Endpoints (reverse-engineered from cloud.orcaslicer.com JS bundle)
----------------------------------------------------------------------
- POST   /api/v1/plugins              — Create plugin (multipart FormData)
- PATCH  /api/v1/plugins/{uuid}       — Update plugin (multipart or JSON)
- DELETE /api/v1/plugins?ids={uuid}   — Delete plugin
- GET    /api/v1/plugins/mine         — List user's own plugins
- GET    /api/v1/plugins/share/{token} — Get plugin by sharing link

TODO: Implement sync for README.md content → cloud description
TODO: Implement sync for screenshots/media → cloud attachments
TODO: Add GitHub webhook/action integration for auto-publish on tag push
TODO: Implement cli.py for headless usage (CI/CD pipelines)
"""

__version__ = "0.1.0"
