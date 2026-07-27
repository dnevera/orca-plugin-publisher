#!/usr/bin/env python3
"""Orca Cloud Connector — desktop app launcher.

Opens a native window via pywebview with the JS API
exposed directly to the frontend. No HTTP server needed.
"""

import logging
from pathlib import Path

import webview

from .api import ConnectorAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

_STATIC = Path(__file__).parent.parent / "static"
_ICON = _STATIC / "icon-256.png"


def main():
    api = ConnectorAPI()

    # Build window kwargs — icon is optional (pywebview ignores if unsupported)
    win_kwargs = dict(
        title="Orca Cloud Connector",
        url=str(_STATIC / "index.html"),
        js_api=api,
        width=900,
        height=700,
        min_size=(600, 400),
    )

    window = webview.create_window(**win_kwargs)
    # Give API access to window for native dialogs (folder picker)
    api.set_window(window)
    webview.start(icon=str(_ICON) if _ICON.exists() else None)


if __name__ == "__main__":
    main()
