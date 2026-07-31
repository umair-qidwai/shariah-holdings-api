"""Vercel ASGI entry point that works directly from the repository checkout."""

from pathlib import Path
import sys

# Vercel imports this module from the project root without installing the package.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shariah_holdings.api import app  # noqa: E402

__all__ = ["app"]
