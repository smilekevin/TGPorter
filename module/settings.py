"""Validated, atomic application settings, independent of task checkpoints."""

import copy
import json
import os
import tempfile
import threading

from lang import parse_language


# One schema drives persistence and the native Configuration form.
FIELDS = {
    "api_id": (0, "Basic configuration", "API ID"),
    "api_hash": ("", "Basic configuration", "API Hash"),
    "bot_token": ("", "Basic configuration", "Bot Token"),
    "use_user_account": (False, "Basic configuration", "Use a user account for downloads"),
    "allowed_user_ids": ([], "Basic configuration", "Allowed users"),
    "language": ("en", "Basic configuration", "Bot and log language"),
    "max_download_task": (5, "Downloads", "Simultaneous files"),
    "file_download_concurrency": (4, "Downloads", "Workers per file"),
    "download_part_size_mb": (64, "Downloads", "Part size (MiB)"),
    "save_path": ("./downloads", "Downloads", "Download directory"),
    "media_types": (["audio", "document", "photo", "video", "voice", "animation"], "Downloads", "Media types"),
    "file_formats": ({"audio": ["all"], "document": ["pdf", "epub"], "video": ["mp4"]}, "Downloads", "File formats"),
    "download_filters": ([], "Downloads", "Download filters"),
    "filter_advertisement_list": ([], "Downloads", "Advertisement keywords"),
    "file_path_prefix": (["chat_title", "media_datetime"], "Downloads", "Directory name fields"),
    "date_format": ("%Y_%m", "Downloads", "Media date format"),
    "file_name_prefix": (["file_name"], "Downloads", "File name fields"),
    "file_name_prefix_split": (" - ", "Downloads", "File name separator"),
    "hide_file_name": (False, "Downloads", "Hide file names"),
    "drop_no_audio_video": (False, "Downloads", "Skip videos without audio"),
    "enable_download_txt": (False, "Downloads", "Download message text"),
    "chat": ([], "Channels", "Channel scans"),
    "replace_advertisement_list": ([], "Channels", "Advertisement replacements"),
    "group_add_advertisement": ({}, "Channels", "Channel advertisements"),
    "forward_limit": (33, "Channels", "Forward limit"),
    "after_upload_telegram_delete": (True, "Channels", "Delete local files after forwarding"),
    "web_login_secret": ("", "Web and MCP", "Web password"),
    "public_web": (False, "Web and MCP", "Public dashboard and Web API"),
    "web_host": ("0.0.0.0", "Web and MCP", "Listen address"),
    "web_port": (5000, "Web and MCP", "Listen port"),
    "mcp_enabled": (False, "Web and MCP", "Enable MCP"),
    "mcp_token": ("", "Web and MCP", "MCP Token"),
    "mcp_read_only": (False, "Web and MCP", "MCP read only"),
    "mcp_allowed_hosts": ([], "Web and MCP", "MCP service hostname"),
    "proxy": ({}, "Advanced", "Telegram proxy"),
    "start_timeout": (60, "Advanced", "Connection timeout (seconds)"),
    "max_concurrent_transmissions": (0, "Advanced", "Transport capacity (0 = automatic)"),
    "upload_drive": ({}, "Advanced", "Cloud upload"),
    "log_level": ("INFO", "Advanced", "Log level"),
    "debug_web": (False, "Advanced", "Web debug logging"),
}
SECRETS = {"api_hash", "bot_token", "web_login_secret", "mcp_token", "proxy"}
RESTART_FIELDS = {"web_host", "web_port", "debug_web", "mcp_enabled", "mcp_token",
                  "mcp_read_only", "mcp_allowed_hosts", "log_level", "chat", "upload_drive"}
CONNECTION_FIELDS = {"api_id", "api_hash", "bot_token", "use_user_account", "proxy", "start_timeout"}
LIVE_FIELDS = FIELDS.keys() - RESTART_FIELDS - CONNECTION_FIELDS
RUNTIME_FIELDS = {"language", "max_download_task", "filter_advertisement_list",
                  "replace_advertisement_list", "group_add_advertisement"}
LIMITS = {"api_id": (0, 2147483647), "max_download_task": (1, 6),
          "file_download_concurrency": (1, 32), "download_part_size_mb": (1, 4096),
          "web_port": (1, 65535), "start_timeout": (1, 600),
          "forward_limit": (1, 10000), "max_concurrent_transmissions": (0, 10000)}


def defaults():
    return {key: copy.deepcopy(field[0]) for key, field in FIELDS.items()}


def validate_settings(values):
    """Validate a complete snapshot without exposing supplied secrets in errors."""
    from module.mcp_server import validate_allowed_hosts

    if not isinstance(values, dict) or values.keys() - FIELDS.keys():
        raise ValueError("Unknown configuration field")
    result = {**defaults(), **copy.deepcopy(values)}
    for key, (default, _, _) in FIELDS.items():
        if type(result[key]) is not type(default):
            raise ValueError(f"{key}: invalid value type")
        if key in LIMITS and not LIMITS[key][0] <= result[key] <= LIMITS[key][1]:
            raise ValueError(f"{key}: value outside supported range")
    result["language"] = parse_language(result["language"]).value
    result["mcp_allowed_hosts"] = validate_allowed_hosts(result["mcp_allowed_hosts"])
    token = result["mcp_token"]
    if any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("mcp_token must be printable ASCII without whitespace")
    if result["mcp_enabled"] and not token:
        raise ValueError("mcp_enabled requires a non-empty mcp_token")
    if result["log_level"] not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("log_level: unsupported level")
    for key in ("media_types", "file_path_prefix", "file_name_prefix",
                "filter_advertisement_list", "replace_advertisement_list"):
        if not all(isinstance(item, str) for item in result[key]):
            raise ValueError(f"{key}: expected a list of strings")
    if not set(result["media_types"]) <= {"audio", "document", "photo", "video", "voice", "animation", "video_note", "sticker"}:
        raise ValueError("media_types: unsupported media type")
    from module.filter import MAX_DOWNLOAD_FILTERS, validate_download_filter
    if len(result["download_filters"]) > MAX_DOWNLOAD_FILTERS:
        raise ValueError(f"Download filters cannot exceed {MAX_DOWNLOAD_FILTERS} rules")
    result["download_filters"] = list(dict.fromkeys(
        validate_download_filter(expression) for expression in result["download_filters"]))
    if not all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
               for k, v in result["file_formats"].items()):
        raise ValueError("file_formats: expected media types mapped to extension lists")
    if not all(type(item) in (str, int) for item in result["allowed_user_ids"]):
        raise ValueError("allowed_user_ids: expected user IDs or me")
    if not all(type(key) in (str, int) and isinstance(value, str)
               for key, value in result["group_add_advertisement"].items()):
        raise ValueError("group_add_advertisement: expected chat IDs mapped to text")
    upload = result["upload_drive"]
    upload_types = {"enable_upload_file": bool, "before_upload_file_zip": bool,
                    "after_upload_file_delete": bool, "rclone_path": str,
                    "remote_dir": str, "upload_adapter": str}
    if (upload.keys() - upload_types.keys()
            or any(type(value) is not upload_types[key] for key, value in upload.items())
            or upload.get("upload_adapter", "rclone") not in {"rclone", "aligo"}):
        raise ValueError("upload_drive: invalid cloud upload settings")
    for item in result["chat"]:
        if (not isinstance(item, dict) or type(item.get("chat_id")) not in (str, int)
                or type(item.get("last_read_message_id", 0)) is not int
                or item.get("last_read_message_id", 0) < 0
                or not isinstance(item.get("download_filter", ""), str)):
            raise ValueError("chat: invalid channel scan")
        if item.get("download_filter"):
            from module.filter import _PARSER
            from utils.format import replace_date_time
            try:
                _PARSER.parse(replace_date_time(item["download_filter"]))
            except Exception:
                raise ValueError("chat: invalid download filter") from None
    proxy = result["proxy"]
    if proxy and (proxy.get("scheme") not in {"http", "socks4", "socks5"}
                  or not isinstance(proxy.get("hostname"), str) or not proxy["hostname"]
                  or type(proxy.get("port")) is not int or not 1 <= proxy["port"] <= 65535
                  or any(not isinstance(proxy.get(key, ""), str) for key in ("username", "password"))):
        raise ValueError("proxy: invalid scheme, host, port or credentials")
    if not result["web_host"] or not result["save_path"] or "\x00" in result["save_path"]:
        raise ValueError("Listen address and download directory are required")
    return result


class SettingsStore:
    """Serialize readers and writers and replace one private settings file atomically."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.values = defaults()
        self.setup_complete = False
        self.infer_account_mode = False

    def load(self):
        with self.lock, open(self.path, encoding="utf-8") as stream:
            saved = json.load(stream)
            if not isinstance(saved, dict):
                raise ValueError("Settings must be a JSON object")
            self.setup_complete = saved.pop("_setup_complete", bool(saved.get("web_login_secret")))
            if type(self.setup_complete) is not bool:
                raise ValueError("Invalid setup state")
            # Earlier releases authorized the main account independently of Bot
            # configuration. Preserve that choice when upgrading an installation.
            self.infer_account_mode = "use_user_account" not in saved
            saved.setdefault("use_user_account", bool(saved.get("api_id")))
            self.values = validate_settings(saved)

    def save(self, updates, *, setup_complete=None):
        with self.lock:
            values = {**self.values, **copy.deepcopy(updates)}
            values = validate_settings(values)
            complete = self.setup_complete if setup_complete is None else setup_complete
            content = json.dumps({**values, "_setup_complete": complete}, ensure_ascii=False, indent=2) + "\n"
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                                 prefix=".settings-", suffix=".tmp", delete=False) as stream:
                    temporary = stream.name
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            self.values = values
            self.setup_complete = complete

    def public_values(self):
        with self.lock:
            return {key: copy.deepcopy(value) for key, value in self.values.items() if key not in SECRETS}
