"""Resolve one version for images, source checkouts and exported source."""

import os
from pathlib import Path
import subprocess


def get_version() -> str:
    """Prefer the baked image version, then describe this source checkout."""
    version = os.environ.get("APP_VERSION", "").strip()
    if version:
        return version

    root = Path(__file__).resolve().parents[1]
    # A worktree has a .git file. Exported source must not use a parent repo.
    if (root / ".git").exists():
        try:
            version = subprocess.check_output(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=root, stderr=subprocess.DEVNULL, text=True, timeout=2,
            ).strip()
            if version:
                return version
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    return "unknown"
