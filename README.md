# Orca Plugin Publisher

Local web app to publish OrcaSlicer plugins from GitHub to Orca Cloud.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m uvicorn orca_plugin_publisher.app:app --port 8420
```

Open http://localhost:8420 in your browser.
