"""Application module"""

import asyncio
import copy
import functools
import json
import os
import tempfile
import threading
import time
from asyncio import Lock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, List, Optional, Union

from loguru import logger
from ruamel import yaml

from module.cloud_drive import CloudDrive, CloudDriveConfig
from module.filter import Filter
from module.language import Language, parse_language, set_language
from module.runtime_paths import APP_DATA_FILE
from utils.format import replace_date_time, validate_title
from utils.meta_data import MetaData

_yaml = yaml.YAML()
# pylint: disable = R0902


class DownloadStatus(Enum):
    """Download status"""

    SkipDownload = 1
    SuccessDownload = 2
    FailedDownload = 3
    Downloading = 4


class ForwardStatus(Enum):
    """Forward status"""

    SkipForward = 1
    SuccessForward = 2
    FailedForward = 3
    Forwarding = 4
    StopForward = 5
    CacheForward = 6


class UploadStatus(Enum):
    """Upload status"""

    SkipUpload = 1
    SuccessUpload = 2
    FailedUpload = 3
    Uploading = 4


class TaskType(Enum):
    """Task Type"""

    Download = 1
    Forward = 2
    ListenForward = 3


class QueryHandler(Enum):
    """Query handler"""

    StopDownload = 1
    StopForward = 2
    StopListenForward = 3


@dataclass
class UploadProgressStat:
    """Upload task"""

    file_name: str
    total_size: int
    upload_size: int
    start_time: float
    last_stat_time: float
    upload_speed: float


@dataclass
class CloudDriveUploadStat:
    """Cloud drive upload task"""

    file_name: str
    transferred: str
    total: str
    percentage: str
    speed: str
    eta: str


class QueryHandlerStr:
    """Query handler"""

    _strMap = {
        QueryHandler.StopDownload.value: "stop_download",
        QueryHandler.StopForward.value: "stop_forward",
        QueryHandler.StopListenForward.value: "stop_listen_forward",
    }

    @staticmethod
    def get_str(value):
        """
        Get the string value associated with the given value.

        Parameters:
            value (any): The value for which to retrieve the string value.

        Returns:
            str: The string value associated with the given value.
        """
        return QueryHandlerStr._strMap[value]


class TaskNode:
    """Task node"""

    def __init__(
        self,
        chat_id: Union[int, str],
        from_user_id: Union[int, str] = None,
        reply_message_id: int = 0,
        replay_message: str = None,
        upload_telegram_chat_id: Union[int, str] = None,
        has_protected_content: bool = False,
        download_filter: str = None,
        limit: int = 0,
        start_offset_id: int = 0,
        end_offset_id: int = 0,
        bot=None,
        task_type: TaskType = TaskType.Download,
        task_id: int = 0,
        topic_id: int = 0,
        task_id_display: str = "",
    ):
        self.chat_id = chat_id
        self.download_temp_paths = {}
        self.from_user_id = from_user_id
        self.upload_telegram_chat_id = upload_telegram_chat_id
        self.reply_message_id = reply_message_id
        self.reply_message = replay_message
        self.has_protected_content = has_protected_content
        self.download_filter = download_filter
        self.bypass_filters = False
        self.skip_reasons = {}
        self.limit = limit
        self.start_offset_id = start_offset_id
        self.end_offset_id = end_offset_id
        self.bot = bot
        self.task_id = task_id
        self.task_type = task_type
        self.total_task = 0
        self.total_download_task = 0
        self.failed_download_task = 0
        self.success_download_task = 0
        self.skip_download_task = 0
        self.last_reply_time = time.time()
        self.last_edit_msg: str = ""
        self.last_bot_flood_notice: float = time.time()  # Ignore notification waits before this task.
        self.initial_progress_reported: bool = False  # Whether first progress report has been sent
        self.flood_wait_until: float = 0  # Non-blocking FLOOD_WAIT cooldown
        self.total_download_byte = 0
        self.forward_msg_detail_str: str = ""
        self.upload_user = None
        self.total_forward_task: int = 0
        self.success_forward_task: int = 0
        self.failed_forward_task: int = 0
        self.skip_forward_task: int = 0
        self.is_running: bool = False
        self.client = None
        self.upload_success_count: int = 0
        self.is_stop_transmission = False
        self.discard_partial = False
        self.media_group_ids: dict = {}
        self.media_group_ids_lock: Lock = Lock()
        self.download_status: dict = {}
        self.upload_status: dict = {}
        self.upload_stat_dict: dict = {}
        self.topic_id = topic_id
        self.reply_to_message = None
        self.cloud_drive_upload_stat_dict: dict = {}
        self.source_chat_title: str = ""
        self.source_chat_id: int = 0
        self.source_message_id: int = 0

        # Generate sequential display id (persistent across restarts)
        if task_id_display:
            self.task_id_display = task_id_display
        else:
            from module.task_store import _get_next_seq, _lock
            date_str = time.strftime('%m%d')
            with _lock:
                seq = _get_next_seq(date_str)
            self.task_id_display = f"{date_str}-{seq}"

    def skip_msg_id(self, msg_id: int):
        """Skip if message id out of range"""
        if self.start_offset_id and msg_id < self.start_offset_id:
            return True

        if self.end_offset_id and msg_id > self.end_offset_id:
            return True

        return False

    def is_finish(self):
        """If is finish"""
        return self.is_stop_transmission or (
            self.is_running
            and not getattr(self, "_recovering_range", False)
            and self.task_type != TaskType.ListenForward
            and self.total_task > 0
            and self.total_task == self.total_download_task
        )

    def stop_transmission(self, *, discard_partial=False):
        """Stop task"""
        self.is_stop_transmission = True
        self.discard_partial = self.discard_partial or discard_partial

    def stat(self, status: DownloadStatus):
        """
        Updates the download status of the task.

        Args:
            status (DownloadStatus): The status of the download task.

        Returns:
            None
        """
        self.total_download_task += 1
        if status is DownloadStatus.SuccessDownload:
            self.success_download_task += 1
        elif status is DownloadStatus.SkipDownload:
            self.skip_download_task += 1
        else:
            self.failed_download_task += 1

    def stat_forward(self, status: ForwardStatus, count: int = 1):
        """Stat upload"""
        self.total_forward_task += count
        if status is ForwardStatus.SuccessForward:
            self.success_forward_task += count
        elif status is ForwardStatus.SkipForward:
            self.skip_forward_task += count
        else:
            self.failed_forward_task += count

    def can_reply(self, immediate=False):
        """
        Checks if the bot can reply to a message
            based on the time elapsed since the last reply.

        Returns:
            True if the time elapsed since
                the last reply is at least 10 seconds, False otherwise.
            Immediate reports skip this interval but still honor FLOOD_WAIT.
        """
        cur_time = time.time()
        # Check FLOOD_WAIT cooldown
        if cur_time < self.flood_wait_until:
            return False
        if immediate or cur_time - self.last_reply_time >= 10.0:
            self.last_reply_time = cur_time
            return True

        return False


class LimitCall:
    """Limit call"""

    def __init__(
        self,
        max_limit_call_times: int = 0,
        limit_call_times: int = 0,
        last_call_time: float = 0,
    ):
        """
        Initializes the object with the given parameters.

        Args:
            max_limit_call_times (int): The maximum limit of call times allowed.
            limit_call_times (int): The current limit of call times.
            last_call_time (int): The time of the last call.

        Returns:
            None
        """
        self.max_limit_call_times = max_limit_call_times
        self.limit_call_times = limit_call_times
        self.last_call_time = last_call_time

    async def wait(self, node: TaskNode):
        """
        Wait for a certain period of time before continuing execution.

        This function does not take any parameters.

        This function does not return anything.
        """
        while True:
            now = time.time()
            time_span = now - self.last_call_time
            if node.is_stop_transmission:
                break

            if time_span > 60:
                self.limit_call_times = 0
                self.last_call_time = now

            if self.limit_call_times + 1 <= self.max_limit_call_times:
                self.limit_call_times += 1
                break

            # logger.debug("Waiting for 10 seconds...")
            await asyncio.sleep(1)


class ChatDownloadConfig:
    """Chat Message Download Status"""

    def __init__(self):
        self.ids_to_retry_dict: dict = {}

        # need storage
        self.download_filter: str = None
        self.ids_to_retry: list = []
        self.last_read_message_id = 0
        self.total_task: int = 0
        self.finish_task: int = 0
        self.need_check: bool = False
        self.upload_telegram_chat_id: Union[int, str] = None
        self.node: TaskNode = TaskNode(0)


def get_config(config, key, default=None, val_type=str, verbose=True):
    """
    Retrieves a configuration value from the given `config` dictionary
    based on the specified `key`.

    Args:
        config (dict): A dictionary containing the configuration values.
        key (str): The key of the configuration value to retrieve.
        default (Any, optional): The default value to be returned
            if the `key` is not found.
        val_type (type, optional): The data type of the configuration value.
        verbose (bool, optional): A flag indicating whether to print
            a warning message if the `key` is not found.

    Returns:
        The configuration value associated with the specified `key`,
         converted to the specified `type`. If the `key` is not found,
         the `default` value is returned.
    """
    val = config.get(key, default)
    if isinstance(val, val_type):
        return val

    if verbose:
        logger.warning(f"{key} is not {val_type.__name__}")

    return default


class Application:
    """Load managed settings and persist task checkpoints separately."""

    def __init__(
        self,
        config_file: str = "config.yaml",
        app_data_file: str = APP_DATA_FILE,
        application_name: str = "UndefineApp",
    ):
        """
        Init and update TG Porter config

        Parameters
        ----------
        config_file: str
            Config file name

        app_data_file: str
            App data file

        application_name: str
            Application Name

        """
        self.config_file: str = config_file
        self.app_data_file: str = os.path.abspath(app_data_file)
        self.data_path: str = os.path.dirname(self.app_data_file)
        self.application_name: str = application_name
        self.download_filter = Filter()
        self.is_running = True

        self.total_download_task = 0

        self.chat_download_config: dict = {}

        self.save_path = os.path.join(os.path.abspath("."), "downloads")
        self.temp_save_path = os.path.join(self.save_path, ".temp")
        self.api_id: str = ""
        self.api_hash: str = ""
        self.bot_token: str = ""
        self.use_user_account: bool = False
        self._chat_id: str = ""
        self.media_types: List[str] = []
        self.download_filters: List[str] = []
        self.file_formats: dict = {}
        self.proxy: dict = {}
        self.restart_program = False
        self.config: dict = {}
        self.app_data: dict = {}
        self._app_data_lock = threading.RLock()
        from module.settings import SettingsStore
        self.settings = SettingsStore(os.path.join(self.data_path, "settings.json"))
        self._runtime_settings_snapshot = {}
        self.setup_token = ""
        self.telegram_login = None
        self.file_path_prefix: List[str] = ["chat_title", "media_datetime"]
        self.file_name_prefix: List[str] = ["message_id", "file_name"]
        self.file_name_prefix_split: str = " - "
        self.log_file_path = os.path.join(self.data_path, "log")
        self.session_file_path = os.path.join(self.data_path, "sessions")
        self.cloud_drive_config = CloudDriveConfig()
        self.hide_file_name = False
        self.caption_name_dict: dict = {}
        self.caption_entities_dict: dict = {}
        self.max_concurrent_transmissions: int = 1
        self.web_host: str = "0.0.0.0"
        self.web_port: int = 5088
        self.max_download_task: int = 5
        self.file_download_concurrency: int = 1
        self.download_part_size_mb: int = 64
        self.language = Language.EN
        self.after_upload_telegram_delete: bool = True
        self.web_login_secret: str = ""
        self.public_web = False
        self.mcp_token: str = ""
        self.mcp_enabled: bool = False
        self.mcp_read_only: bool = False
        self.mcp_allowed_hosts: List[str] = []
        self.debug_web: bool = False
        self.log_level: str = "INFO"
        self.start_timeout: int = 60
        self.allowed_user_ids: List = []  # Human download accounts are authorized automatically.
        self.date_format: str = "%Y_%m"  # Format for the optional media-date directory component.
        self.drop_no_audio_video: bool = False
        self.enable_download_txt: bool = False
        self.filter_advertisement_list: List = []
        self.replace_advertisement_list: List = []
        self.group_add_advertisement: dict = {}
        self.forward_limit_call = LimitCall(max_limit_call_times=33)

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.executor = ThreadPoolExecutor(
            min(32, (os.cpu_count() or 0) + 4), thread_name_prefix="multi_task"
        )

    # pylint: disable = R0915
    def assign_config(self, _config: dict) -> bool:
        """assign config from str.

        Parameters
        ----------
        _config: dict
            application config dict

        Returns
        -------
        bool
        """
        # pylint: disable = R0912
        # TODO: judge the storage if enough,and provide more path
        if _config.get("save_path") is not None:
            self.save_path = os.path.abspath(_config["save_path"])
        self.temp_save_path = os.path.join(self.save_path, ".temp")

        self.api_id = _config["api_id"]
        self.api_hash = _config["api_hash"]
        self.bot_token = _config.get("bot_token", "")
        self.use_user_account = _config.get("use_user_account", False)

        self.media_types = _config["media_types"]
        self.file_formats = _config["file_formats"]
        self.download_filters = copy.deepcopy(_config.get("download_filters", []))

        self.hide_file_name = _config.get("hide_file_name", False)

        # option
        if _config.get("proxy"):
            self.proxy = _config["proxy"]
        if _config.get("restart_program"):
            self.restart_program = _config["restart_program"]
        if _config.get("file_path_prefix") is not None:
            self.file_path_prefix = _config["file_path_prefix"]
        if _config.get("file_name_prefix") is not None:
            self.file_name_prefix = _config["file_name_prefix"]

        upload_drive_config = _config.get("upload_drive", {})
        for key in ("enable_upload_file", "rclone_path", "remote_dir", "before_upload_file_zip",
                    "after_upload_file_delete", "upload_adapter"):
            if key in upload_drive_config:
                setattr(self.cloud_drive_config, key, upload_drive_config[key])

        self.file_name_prefix_split = _config.get(
            "file_name_prefix_split", self.file_name_prefix_split
        )
        self.web_host = _config.get("web_host", self.web_host)
        self.web_port = _config.get("web_port", self.web_port)

        # TODO: add check if expression exist syntax error

        self.max_download_task = _config.get(
            "max_download_task", self.max_download_task
        )

        self.file_download_concurrency = _config.get("file_download_concurrency", 1)
        self.download_part_size_mb = _config.get("download_part_size_mb", _config.get("download_min_part_size_mb", 64))
        for key in ("file_download_concurrency", "download_part_size_mb"):
            value = getattr(self, key)
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")

        self.max_concurrent_transmissions = max(
            self.max_download_task * 5,
            self.max_download_task * self.file_download_concurrency,
        )

        self.max_concurrent_transmissions = _config.get(
            "max_concurrent_transmissions", self.max_concurrent_transmissions
        )
        self._transport_capacity_override = _config.get("max_concurrent_transmissions", 0)

        language = _config.get("language", "EN")

        try:
            self.language = parse_language(language)
        except ValueError:
            pass

        self.after_upload_telegram_delete = _config.get(
            "after_upload_telegram_delete", self.after_upload_telegram_delete
        )

        web_login_secret = _config.get("web_login_secret", self.web_login_secret)
        self.web_login_secret = "" if web_login_secret is None else str(web_login_secret)
        self.public_web = _config.get("public_web", False)
        mcp_token = _config.get("mcp_token", self.mcp_token)
        if mcp_token is None:
            mcp_token = ""
        if not isinstance(mcp_token, str) or any(not 33 <= ord(char) <= 126 for char in mcp_token):
            raise ValueError("mcp_token must be a printable ASCII string without whitespace")
        self.mcp_token = mcp_token
        for name in ("mcp_enabled", "mcp_read_only"):
            value = _config.get(name, getattr(self, name))
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
            setattr(self, name, value)
        from module.mcp_server import validate_allowed_hosts
        self.mcp_allowed_hosts = validate_allowed_hosts(
            _config.get("mcp_allowed_hosts", self.mcp_allowed_hosts)
        )
        if self.mcp_enabled and not self.mcp_token:
            raise ValueError("mcp_enabled requires a non-empty mcp_token")
        self.debug_web = _config.get("debug_web", self.debug_web)
        self.log_level = _config.get("log_level", self.log_level)

        self.start_timeout = get_config(
            _config, "start_timeout", self.start_timeout, int
        )

        self.allowed_user_ids = get_config(
            _config,
            "allowed_user_ids",
            self.allowed_user_ids,
            list,
        )

        self.date_format = get_config(
            _config,
            "date_format",
            self.date_format,
            str,
        )

        self.drop_no_audio_video = get_config(
            _config, "drop_no_audio_video", self.drop_no_audio_video, bool
        )

        self.enable_download_txt = get_config(
            _config, "enable_download_txt", self.enable_download_txt, bool
        )

        self.filter_advertisement_list = copy.deepcopy(get_config(
            _config,
            "filter_advertisement_list",
            self.filter_advertisement_list,
            list,
        ))

        self.replace_advertisement_list = copy.deepcopy(get_config(
            _config,
            "replace_advertisement_list",
            self.replace_advertisement_list,
            list,
        ))

        if _config.get("group_add_advertisement"):
            self.group_add_advertisement = copy.deepcopy(_config["group_add_advertisement"])
        try:
            date = datetime(2023, 10, 31)
            date.strftime(self.date_format)
        except Exception as e:
            logger.warning(f"config date format error: {e}")
            self.date_format = "%Y_%m"

        forward_limit = _config.get("forward_limit", None)
        if forward_limit:
            try:
                forward_limit = int(forward_limit)
                self.forward_limit_call.max_limit_call_times = forward_limit
            except ValueError:
                pass

        if _config.get("chat"):
            chat = _config["chat"]
            for item in chat:
                if "chat_id" in item:
                    self.chat_download_config[item["chat_id"]] = ChatDownloadConfig()
                    self.chat_download_config[
                        item["chat_id"]
                    ].last_read_message_id = item.get("last_read_message_id", 0)
                    self.chat_download_config[
                        item["chat_id"]
                    ].download_filter = item.get("download_filter", "")
                    self.chat_download_config[
                        item["chat_id"]
                    ].upload_telegram_chat_id = item.get(
                        "upload_telegram_chat_id", None
                    )
        elif _config.get("chat_id"):
            # Compatible with lower versions
            self._chat_id = _config["chat_id"]

            self.chat_download_config[self._chat_id] = ChatDownloadConfig()

            if _config.get("ids_to_retry"):
                self.chat_download_config[self._chat_id].ids_to_retry = _config[
                    "ids_to_retry"
                ]
                for it in self.chat_download_config[self._chat_id].ids_to_retry:
                    self.chat_download_config[self._chat_id].ids_to_retry_dict[
                        it
                    ] = True

            self.chat_download_config[self._chat_id].last_read_message_id = _config[
                "last_read_message_id"
            ]
            download_filter_dict = _config.get("download_filter", None)

            self.config["chat"] = [
                {
                    "chat_id": self._chat_id,
                    "last_read_message_id": self.chat_download_config[
                        self._chat_id
                    ].last_read_message_id,
                }
            ]

            if download_filter_dict and self._chat_id in download_filter_dict:
                self.chat_download_config[
                    self._chat_id
                ].download_filter = download_filter_dict[self._chat_id]
                self.config["chat"][0]["download_filter"] = download_filter_dict[
                    self._chat_id
                ]

        # pylint: disable = R1733
        for key, value in self.chat_download_config.items():
            self.chat_download_config[key].download_filter = replace_date_time(
                value.download_filter
            )

        return True

    def assign_app_data(self, app_data: dict) -> bool:
        """Restore task checkpoints; settings migrate separately during load_config()."""
        for chat in app_data.get("chat", []):
            chat_id = chat.get("chat_id")
            if chat_id in self.chat_download_config:
                config = self.chat_download_config[chat_id]
                config.ids_to_retry = chat.get("ids_to_retry", [])
                config.ids_to_retry_dict = {message_id: True for message_id in config.ids_to_retry}
                if "last_read_message_id" in chat:
                    config.last_read_message_id = max(
                        config.last_read_message_id, chat["last_read_message_id"])
        return True

    async def upload_file(
        self,
        local_file_path: str,
        progress_callback: Callable = None,
        progress_args: tuple = (),
    ) -> bool:
        """Upload file"""

        if not self.cloud_drive_config.enable_upload_file:
            return False

        ret: bool = False
        if self.cloud_drive_config.upload_adapter == "rclone":
            ret = await CloudDrive.rclone_upload_file(
                self.cloud_drive_config,
                self.save_path,
                local_file_path,
                progress_callback,
                progress_args,
            )
        elif self.cloud_drive_config.upload_adapter == "aligo":
            ret = await self.loop.run_in_executor(
                self.executor,
                functools.partial(
                    CloudDrive.aligo_upload_file,
                    self.cloud_drive_config, self.save_path, local_file_path
                ),
            )

        return ret

    def get_file_save_path(
        self, media_type: str, chat_title: str, media_datetime: str
    ) -> str:
        """Get file save path prefix.

        Parameters
        ----------
        media_type: str
            see config.yaml media_types

        chat_title: str
            see channel or group title

        media_datetime: str
            media datetime

        Returns
        -------
        str
            file save path prefix
        """

        res: str = self.save_path
        for prefix in self.file_path_prefix:
            if prefix == "chat_title":
                res = os.path.join(res, chat_title)
            elif prefix == "media_datetime":
                res = os.path.join(res, media_datetime)
            elif prefix == "media_type":
                res = os.path.join(res, media_type)
        return res

    def get_file_name(
        self, message_id: int, file_name: Optional[str], caption: Optional[str]
    ) -> str:
        """Get file save path prefix.

        Parameters
        ----------
        message_id: int
            Message id

        file_name: Optional[str]
            File name

        caption: Optional[str]
            Message caption

        Returns
        -------
        str
            File name
        """

        res: str = ""
        for prefix in self.file_name_prefix:
            if prefix == "message_id":
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{message_id}"
            elif prefix == "file_name" and file_name:
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{file_name}"
            elif prefix == "caption" and caption:
                if res != "":
                    res += self.file_name_prefix_split
                res += f"{caption}"
        if res == "":
            res = f"{message_id}"

        return validate_title(res)

    def need_skip_message(
        self, download_config: ChatDownloadConfig, message_id: int
    ) -> bool:
        """if need skip download message.

        Parameters
        ----------
        chat_id: str
            Config.yaml defined

        message_id: int
            Readily to download message id
        Returns
        -------
        bool
        """
        if message_id in download_config.ids_to_retry_dict:
            return True

        return False

    def resolve_download_filter(self, expression=None):
        """Snapshot defaults for a new task; explicit task/channel rules take precedence."""
        if expression:
            return expression
        return " or ".join(f"({rule})" for rule in self.download_filters)

    def exec_filter(self, download_config: ChatDownloadConfig, meta_data: MetaData):
        """
        Executes the filter on the given download configuration.

        Args:
            download_config (ChatDownloadConfig): The download configuration object.
            meta_data (MetaData): The meta data object.

        Returns:
            bool: The result of executing the filter.
        """
        if download_config.download_filter:
            self.download_filter.set_meta_data(meta_data)
            return self.download_filter.exec(download_config.download_filter)

        return True

    def save_app_data(self, immediate: bool = True):
        """Save settings changed by Bot/API callers and snapshot task checkpoints."""
        with self._app_data_lock:
            self.save_runtime_settings(immediate)
            chats = self.app_data.setdefault("chat", [])
            for chat_id, value in self.chat_download_config.items():
                unfinished_ids = set(value.ids_to_retry)
                for message_id in value.ids_to_retry:
                    if value.node.download_status.get(message_id) in (
                        DownloadStatus.SuccessDownload, DownloadStatus.SkipDownload
                    ):
                        unfinished_ids.discard(message_id)
                # Web setting saves can race with downloader-loop status updates.
                for message_id, status in list(value.node.download_status.items()):
                    if status not in (DownloadStatus.SuccessDownload, DownloadStatus.SkipDownload):
                        unfinished_ids.add(message_id)
                value.ids_to_retry = sorted(unfinished_ids)
                record = next((chat for chat in chats if chat.get("chat_id") == chat_id), None)
                if record is None:
                    record = {"chat_id": chat_id}
                    chats.append(record)
                # Preserve the existing next-message checkpoint convention, including scans
                # with only filtered or text messages. Match records by chat ID, not order.
                record["last_read_message_id"] = value.last_read_message_id + 1
                record["ids_to_retry"] = value.ids_to_retry

            from module.settings import RUNTIME_FIELDS
            for key in RUNTIME_FIELDS:
                self.app_data.pop(key, None)

            if immediate:
                self._write_app_data()

    def runtime_settings(self):
        from module.settings import RUNTIME_FIELDS
        return {key: copy.deepcopy(self.language.value if key == "language" else getattr(self, key))
                for key in RUNTIME_FIELDS}

    def save_runtime_settings(self, immediate=True):
        """Do not overwrite saved, restart-pending settings during checkpoint writes."""
        values = self.runtime_settings()
        changed = {key: value for key, value in values.items()
                   if value != self._runtime_settings_snapshot.get(key)}
        if immediate and changed:
            self.settings.save(changed)
            self._runtime_settings_snapshot = values

    def set_download_limit(self, workers):
        """Persist the live file limit before publishing it to request/loop callers."""
        with self._app_data_lock:
            self.settings.save({"max_download_task": workers})
            self.max_download_task = workers
            self.max_concurrent_transmissions = self.transport_capacity(workers)
            self._runtime_settings_snapshot["max_download_task"] = workers

    def transport_capacity(self, workers):
        return getattr(self, "_transport_capacity_override", 0) or workers * max(5, self.file_download_concurrency)

    def _write_app_data(self):
        """Create the data directory and replace runtime JSON atomically under the save lock."""
        with self._app_data_lock:
            content = json.dumps(self.app_data, ensure_ascii=False, indent=2) + "\n"
            os.makedirs(self.data_path, exist_ok=True)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_path,
                                                 prefix=".data-", suffix=".tmp", delete=False) as stream:
                    temporary_path = stream.name
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.app_data_file)
            finally:
                if temporary_path is not None and os.path.exists(temporary_path):
                    os.remove(temporary_path)

    def set_language(self, language: Language):
        """Set Language"""
        self.language = language
        set_language(language)

    def load_config(self):
        """Prefer managed settings; import optional YAML only before settings exist."""
        from module.settings import FIELDS, RUNTIME_FIELDS, defaults
        settings_exist = os.path.exists(self.settings.path)
        if os.path.exists(self.app_data_file):
            with open(self.app_data_file, encoding="utf-8") as stream:
                app_data = json.load(stream)
            if not isinstance(app_data, dict):
                raise ValueError("Runtime data must be a JSON object")
            self.app_data = app_data
        if settings_exist:
            self.settings.load()
        else:
            values = defaults()
            legacy = os.path.join(os.path.abspath("."), self.config_file)
            if os.path.isfile(legacy):
                with open(legacy, encoding="utf-8") as stream:
                    old = _yaml.load(stream) or {}
                # Round-trip YAML containers are converted before writing JSON.
                old = json.loads(json.dumps(old))
                values.update({key: value for key, value in old.items() if key in FIELDS and value is not None})
                # Unset YAML sequences used the old loader's defaults, and older
                # installations without a per-file setting used one connection.
                if old.get("file_download_concurrency") is None:
                    values["file_download_concurrency"] = 1
                if old.get("chat_id") and not old.get("chat"):
                    chat_id = old["chat_id"]
                    values["chat"] = [{"chat_id": chat_id,
                        "last_read_message_id": old.get("last_read_message_id", 0),
                        "download_filter": (old.get("download_filter") or {}).get(str(chat_id), "")}]
                if "download_min_part_size_mb" in old and "download_part_size_mb" not in old:
                    values["download_part_size_mb"] = old["download_min_part_size_mb"]
                values["api_id"] = int(values["api_id"])
                values["use_user_account"] = old.get("use_user_account", True)
                values["proxy"] = values["proxy"] or {}
                values["mcp_token"] = values["mcp_token"] or ""
                values["public_web"] = not bool(values["web_login_secret"])
                logger.info("Imported optional startup configuration into data/settings.json")
            for key in RUNTIME_FIELDS:
                if key in self.app_data:
                    values[key] = self.app_data[key]
            self.settings.save(values, setup_complete=bool(values["web_login_secret"]))
            self.settings.infer_account_mode = os.path.isfile(legacy)
        self.apply_settings()
        migrated_keys = RUNTIME_FIELDS & self.app_data.keys()
        for key in migrated_keys:
            self.app_data.pop(key)
        if not os.path.exists(self.app_data_file) or migrated_keys:
            # An initial empty snapshot must not advance configured scan bounds.
            self._write_app_data()

    def apply_settings(self):
        """Apply a saved snapshot before the Telegram backend starts."""
        self.config = copy.deepcopy(self.settings.values)
        if not self.config.get("max_concurrent_transmissions"):
            self.config.pop("max_concurrent_transmissions", None)
        self.chat_download_config.clear()
        self.proxy = {}
        self.cloud_drive_config = CloudDriveConfig()
        self.assign_config(self.config)
        self.group_add_advertisement = {
            int(key) if str(key).lstrip("-").isdigit() else key: value
            for key, value in self.settings.values["group_add_advertisement"].items()
        }
        self.assign_app_data(self.app_data)
        self._runtime_settings_snapshot = self.runtime_settings()

    def apply_live_settings(self):
        """Update future work without replacing active scans or saved part plans."""
        from module.settings import LIVE_FIELDS
        values = self.settings.values
        if not hasattr(self, "active_settings"):
            self.active_settings = {}
        for key in LIVE_FIELDS - {"language", "save_path", "forward_limit", "max_concurrent_transmissions",
                                   "group_add_advertisement"}:
            if hasattr(self, key):
                setattr(self, key, copy.deepcopy(values[key]))
        self.set_language(parse_language(values["language"]))
        self.save_path = os.path.abspath(values["save_path"])
        self.temp_save_path = os.path.join(self.save_path, ".temp")
        self.forward_limit_call.max_limit_call_times = values["forward_limit"]
        self._transport_capacity_override = values["max_concurrent_transmissions"]
        self.max_concurrent_transmissions = self.transport_capacity(self.max_download_task)
        self.group_add_advertisement = {
            int(key) if str(key).lstrip("-").isdigit() else key: value
            for key, value in values["group_add_advertisement"].items()
        }
        self._runtime_settings_snapshot = self.runtime_settings()
        for key in LIVE_FIELDS:
            self.active_settings[key] = copy.deepcopy(values[key])
        self.pre_run(initialize_upload=False)

    def pre_run(self, initialize_upload=True):
        """before run application do"""
        if initialize_upload:
            self.cloud_drive_config.pre_run()
        for directory in (self.data_path, self.log_file_path, self.session_file_path,
                          self.temp_save_path):
            os.makedirs(directory, exist_ok=True)
        set_language(self.language)

    def is_match_advertisement(self, caption) -> bool:
        """is match advertisement

        Parameters
        ----------
        caption: str
        """
        for ad in self.filter_advertisement_list:
            if ad in caption:
                return True

        return False

    def set_caption_name(
        self, chat_id: Union[int, str], media_group_id: Optional[str], caption: str
    ):
        """set caption name map

        Parameters
        ----------
        chat_id: str
            Unique identifier for this chat.

        media_group_id: Optional[str]
            The unique identifier of a media message group this message belongs to.

        caption: str
            Caption for the audio, document, photo, video or voice, 0-1024 characters.
        """
        if not media_group_id:
            return

        if chat_id in self.caption_name_dict:
            self.caption_name_dict[chat_id][media_group_id] = caption
        else:
            self.caption_name_dict[chat_id] = {media_group_id: caption}

    def get_caption_name(
        self, chat_id: Union[int, str], media_group_id: Optional[str]
    ) -> Optional[str]:
        """set caption name map
                media_group_id: Optional[str]
            The unique identifier of a media message group this message belongs to.

        caption: str
            Caption for the audio, document, photo, video or voice, 0-1024 characters.
        """

        if (
            not media_group_id
            or chat_id not in self.caption_name_dict
            or media_group_id not in self.caption_name_dict[chat_id]
        ):
            return None

        return str(self.caption_name_dict[chat_id][media_group_id])

    def set_caption_entities(
        self, chat_id: Union[int, str], media_group_id: Optional[str], caption_entities
    ):
        """
        set caption entities map
        """
        if not media_group_id:
            return

        if chat_id in self.caption_entities_dict:
            self.caption_entities_dict[chat_id][media_group_id] = caption_entities
        else:
            self.caption_entities_dict[chat_id] = {media_group_id: caption_entities}

    def set_download_id(
        self, node: TaskNode, message_id: int, download_status: DownloadStatus
    ):
        """Set Download status"""
        if download_status is DownloadStatus.SuccessDownload:
            self.total_download_task += 1

        if node.chat_id not in self.chat_download_config:
            return

        self.chat_download_config[node.chat_id].finish_task += 1

        self.chat_download_config[node.chat_id].last_read_message_id = max(
            self.chat_download_config[node.chat_id].last_read_message_id, message_id
        )
