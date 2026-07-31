"""Canonical content-addressing for immutable data generations."""

import hashlib
from collections.abc import Mapping


def generation_id(outputs: Mapping[str, str | bytes]) -> str:
    """Hash sorted filename/NUL/raw-content tuples and return the published ID."""
    digest = hashlib.sha256()
    for name in sorted(outputs):
        content = outputs[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8") if isinstance(content, str) else content)
    return digest.hexdigest()[:24]
