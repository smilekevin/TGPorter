"""Default paths covered by the single runtime-data directory mount."""

import os


DATA_DIR = os.path.join(os.path.abspath("."), "data")
APP_DATA_FILE = os.path.join(DATA_DIR, "data.json")
LOG_DIR = os.path.join(DATA_DIR, "log")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
BOT_CONFIG_FILE = os.path.join(DATA_DIR, "bot.yaml")
