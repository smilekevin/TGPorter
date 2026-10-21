"""Bounded, durable receipts for handled Bot inbox messages."""

import json
import os
import tempfile
import threading
from collections import deque
from pathlib import Path


class BotMessageStore:
    """Remember recent (bot, chat, message) IDs without storing message contents."""

    def __init__(self, path, capacity=1_000):
        if capacity < 1:
            raise ValueError("Bot message receipt capacity must be positive")
        self.path = Path(path)
        self.capacity = capacity
        self._lock = threading.RLock()
        self._messages = deque()
        self._seen = set()
        try:
            with self.path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return
        # Fail visibly rather than silently discarding a damaged deduplication history.
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("messages"), list)):
            raise ValueError("Invalid Bot message receipt store")
        for record in data["messages"]:
            if (not isinstance(record, list) or len(record) != 3
                    or any(type(value) is not int for value in record)
                    or record[0] <= 0 or record[1] == 0 or record[2] <= 0):
                raise ValueError("Invalid Bot message receipt")
            key = tuple(record)
            if key not in self._seen:
                self._messages.append(key)
                self._seen.add(key)
        while len(self._messages) > capacity:
            self._seen.remove(self._messages.popleft())

    def contains(self, key):
        """Check exact identity; message IDs need not arrive in increasing order."""
        with self._lock:
            return key in self._seen

    def mark(self, key):
        """Commit a receipt before changing the in-memory ring, preserving it on errors."""
        with self._lock:
            if key in self._seen:
                return
            messages = list(self._messages)
            messages.append(key)
            messages = messages[-self.capacity:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=".bot-messages-", suffix=".tmp", delete=False,
                ) as stream:
                    temporary_path = stream.name
                    json.dump({"version": 1, "messages": messages}, stream, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path is not None and os.path.exists(temporary_path):
                    os.remove(temporary_path)
            self._messages = deque(messages)
            self._seen = set(messages)
