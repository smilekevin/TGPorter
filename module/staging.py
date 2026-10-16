"""Stable ASCII staging names that survive retries without a separate index."""

import json
import os
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


_STAGING_NAME = re.compile(r"\d+_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp\Z")


def is_staging_name(name):
    """Identify generated staging basenames, excluding SDK/part suffixes."""
    return bool(_STAGING_NAME.fullmatch(name))


def _has_checkpoint(path):
    return any(Path(path + suffix).exists() for suffix in ("", ".temp", ".parts"))


def get_staging_path(directory, account, chat_id, message_id, media_identity,
                     timestamp, legacy_path):
    """Use a reproducible UUID so task retries and restarts keep the same data.

    The timestamp comes from the message rather than the current attempt.
    Existing legacy checkpoints keep their paths until completion; no writers
    or saved part plans are moved while selecting a name.
    """
    identity = json.dumps([str(account), str(chat_id), str(message_id), str(media_identity)],
                          ensure_ascii=True, separators=(",", ":"))
    name = f"{max(0, int(timestamp))}_{uuid5(NAMESPACE_URL, identity)}.tmp"
    path = os.path.join(directory, name)
    if _has_checkpoint(path):
        return path
    if legacy_path:
        try:
            # A legacy title may contain '..' or resolve outside temp. Only
            # reuse paths inside this staging root, never arbitrary files.
            root = os.path.realpath(directory)
            if (os.path.commonpath((root, os.path.realpath(legacy_path))) == root
                    and _has_checkpoint(legacy_path)):
                return legacy_path
        except (OSError, ValueError):
            # Invalid or overlong legacy names must not prevent safe staging.
            pass
    return path
