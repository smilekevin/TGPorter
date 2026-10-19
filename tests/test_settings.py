"""Migration precedence, validation and private atomic settings persistence."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from module.app import Application
from module.settings import SettingsStore, validate_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = self.root / "config.yaml"
        self.data = self.root / "data/data.json"

    def load(self):
        app = Application(str(self.legacy), str(self.data))
        self.addCleanup(app.loop.close)
        self.addCleanup(app.executor.shutdown)
        app.load_config()
        return app

    def test_fresh_start_needs_no_yaml_and_creates_defaults(self):
        app = self.load()
        self.assertFalse(self.legacy.exists())
        self.assertTrue(Path(app.settings.path).is_file())
        self.assertFalse(app.settings.setup_complete)
        self.assertFalse(app.use_user_account)
        self.assertEqual(app.allowed_user_ids, [])
        self.assertEqual(app.max_download_task, 5)
        self.assertEqual(app.file_download_concurrency, 4)
        self.assertEqual(json.loads(self.data.read_text()), {})

    def test_legacy_migration_preserves_explicit_me_and_numeric_allowlist(self):
        self.legacy.write_text('api_id: 123\napi_hash: offline\nallowed_user_ids: [me, 12345]\n')
        app = self.load()
        self.assertEqual(app.allowed_user_ids, ["me", 12345])
        self.assertEqual(self.load().allowed_user_ids, ["me", 12345])

    def test_older_settings_preserve_user_login_until_session_mode_is_resolved(self):
        app = self.load()
        path = Path(app.settings.path)
        saved = json.loads(path.read_text())
        saved.pop("use_user_account")
        saved["api_id"] = 123
        path.write_text(json.dumps(saved))
        restarted = self.load()
        self.assertTrue(restarted.use_user_account)
        self.assertTrue(restarted.settings.infer_account_mode)

    def test_migrates_yaml_and_runtime_overrides_once_and_never_reads_yaml_again(self):
        self.legacy.write_text('api_id: "123"\napi_hash: old-secret\nlanguage: en\nmax_download_task: 2\n')
        self.data.parent.mkdir()
        self.data.write_text(json.dumps({"language": "ZH", "max_download_task": 6,
                                         "chat": [{"chat_id": -1001, "last_read_message_id": 81}],
                                         "unrelated": "keep"}))
        original = self.legacy.read_bytes()
        app = self.load()
        self.assertEqual(app.settings.values["api_id"], 123)
        self.assertEqual(app.language.value, "zh-cn")
        self.assertEqual(app.max_download_task, 6)
        app.save_app_data()
        data = json.loads(self.data.read_text())
        self.assertNotIn("language", data)
        self.assertNotIn("max_download_task", data)
        self.assertEqual(data["unrelated"], "keep")
        self.assertEqual(data["chat"][0]["last_read_message_id"], 81)
        self.assertEqual(self.legacy.read_bytes(), original)
        app.settings.save({"language": "fr", "max_download_task": 3})
        self.legacy.write_text("invalid: [yaml")
        restarted = self.load()
        self.assertEqual(restarted.language.value, "fr")
        self.assertEqual(restarted.max_download_task, 3)
        self.legacy.unlink()
        self.assertEqual(self.load().max_download_task, 3)

    def test_existing_settings_ignore_stale_data_overrides(self):
        app = self.load()
        app.settings.save({"language": "ja", "max_download_task": 4})
        self.data.write_text('{"language":"fr","max_download_task":6}')
        restarted = self.load()
        self.assertEqual(restarted.language.value, "ja")
        self.assertEqual(restarted.max_download_task, 4)

    def test_invalid_settings_fail_without_falling_back_to_yaml_or_replacing_file(self):
        app = self.load()
        path = Path(app.settings.path)
        path.write_text("[]")
        self.legacy.write_text("api_id: 123\napi_hash: valid\n")
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(path.read_text(), "[]")

    def test_atomic_failure_keeps_memory_and_disk_and_removes_temporary_file(self):
        app = self.load()
        path = Path(app.settings.path)
        original = path.read_bytes()
        with patch("module.settings.os.replace", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                app.settings.save({"language": "fr"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(app.settings.values["language"], "en")
        self.assertEqual(list(path.parent.glob(".settings-*.tmp")), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_checkpoint_saves_do_not_revert_settings_pending_restart(self):
        app = self.load()
        app.settings.save({"filter_advertisement_list": ["new rule"], "file_download_concurrency": 8})
        app.save_app_data()
        self.assertEqual(app.settings.values["filter_advertisement_list"], ["new rule"])
        self.assertEqual(app.settings.values["file_download_concurrency"], 8)

    def test_nested_values_are_validated_without_exposing_secrets(self):
        for updates in ({"max_download_task": True}, {"web_port": 0}, {"api_id": -1},
                        {"media_types": ["unknown"]}, {"file_formats": {"video": "mp4"}},
                        {"chat": [{"chat_id": 1, "download_filter": "invalid ("}]},
                        {"proxy": {"scheme": "invalid", "password": "do-not-print"}},
                        {"mcp_enabled": True}, {"mcp_allowed_hosts": ["https://example.com"]}):
            with self.subTest(updates=updates), self.assertRaises(ValueError) as raised:
                validate_settings(updates)
            self.assertNotIn("do-not-print", str(raised.exception))

    def test_public_view_excludes_credentials_and_proxy_password(self):
        store = SettingsStore(str(self.root / "settings.json"))
        store.save({"api_hash": "api-secret", "bot_token": "bot-secret", "mcp_token": "mcp-secret",
                    "web_login_secret": "web-secret", "proxy": {"scheme": "http", "hostname": "host", "port": 80,
                                                                  "password": "proxy-secret"}})
        content = json.dumps(store.public_values())
        for secret in ("api-secret", "bot-secret", "mcp-secret", "web-secret", "proxy-secret"):
            self.assertNotIn(secret, content)

    def test_download_defaults_combine_with_or_and_do_not_change_saved_task_rules(self):
        from module.app import TaskNode
        from utils.meta_data import MetaData
        app = self.load()
        self.assertEqual(app.resolve_download_filter(), "")
        self.assertTrue(app.exec_filter(TaskNode(1, download_filter=""), MetaData()))
        app.settings.save({"download_filters": ["media_file_size >= 100MB", "media_duration <= 600"]})
        app.apply_live_settings()
        node = TaskNode(1, download_filter=app.resolve_download_filter())
        self.assertTrue(app.exec_filter(node, MetaData(media_file_size=150 * 1024**2, media_duration=300)))
        self.assertTrue(app.exec_filter(node, MetaData(media_file_size=150 * 1024**2, media_duration=700)))
        self.assertTrue(app.exec_filter(node, MetaData(media_file_size=50 * 1024**2, media_duration=300)))
        self.assertFalse(app.exec_filter(node, MetaData(media_file_size=50 * 1024**2, media_duration=700)))
        self.assertFalse(app.exec_filter(node, MetaData()))
        self.assertEqual(app.resolve_download_filter("media_type == 'photo'"), "media_type == 'photo'")
        app.settings.save({"download_filters": []})
        app.apply_live_settings()
        self.assertEqual(app.resolve_download_filter(), "")
        self.assertIn("media_duration", node.download_filter)
        self.assertEqual(self.load().download_filters, [])

    def test_or_defaults_preserve_each_expression_grouping_and_explicit_and(self):
        from module.app import TaskNode
        from utils.meta_data import MetaData
        app = self.load()
        app.download_filters = ["media_file_size >= 100MB and media_duration <= 600", "media_type == 'photo'"]
        node = TaskNode(1, download_filter=app.resolve_download_filter())
        self.assertTrue(app.exec_filter(node, MetaData(media_type="photo")))
        self.assertFalse(app.exec_filter(node, MetaData(media_type="video", media_file_size=150 * 1024**2, media_duration=700)))
        self.assertTrue(app.exec_filter(node, MetaData(media_type="video", media_file_size=150 * 1024**2, media_duration=300)))

    def test_invalid_download_rule_never_replaces_saved_settings(self):
        app = self.load()
        original = Path(app.settings.path).read_bytes()
        for rules in (["unknown == 1"], [12], [""], ["id == 1"] * 33):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                app.settings.save({"download_filters": rules})
            self.assertEqual(Path(app.settings.path).read_bytes(), original)
        app.settings.save({"download_filters": [" id == 1 ", "id == 1"]})
        self.assertEqual(app.settings.values["download_filters"], ["id == 1"])

    def test_advertisement_keywords_use_or(self):
        app = self.load()
        app.filter_advertisement_list = ["first ad", "second ad"]
        self.assertTrue(app.is_match_advertisement("contains second ad"))
        self.assertTrue(app.is_match_advertisement("contains first ad"))
        self.assertFalse(app.is_match_advertisement("normal caption"))

    def test_migrated_password_free_deployment_retains_public_mode(self):
        self.legacy.write_text('api_id: 123\napi_hash: old-secret\nweb_login_secret: ""\n')
        app = self.load()
        self.assertTrue(app.public_web)
        self.assertFalse(app.settings.setup_complete)

    def test_live_file_limit_does_not_change_when_persistence_fails(self):
        app = self.load()
        previous = app.max_download_task, app.max_concurrent_transmissions
        with patch("module.settings.os.replace", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                app.set_download_limit(2)
        self.assertEqual((app.max_download_task, app.max_concurrent_transmissions), previous)

    def test_false_upload_options_and_empty_naming_lists_are_applied(self):
        app = self.load()
        app.settings.save({"upload_drive": {"after_upload_file_delete": False},
                           "file_path_prefix": [], "file_name_prefix": []})
        app.apply_settings()
        self.assertFalse(app.cloud_drive_config.after_upload_file_delete)
        self.assertEqual(app.file_path_prefix, [])
        self.assertEqual(app.file_name_prefix, [])

    def test_obsolete_template_with_empty_yaml_values_can_be_imported(self):
        template = Path(__file__).resolve().parents[1] / "config.yaml.example"
        self.legacy.write_text(template.read_text().replace("your_api_id", "123"))
        app = self.load()
        self.assertEqual(app.file_path_prefix, ["chat_title", "media_datetime"])
        self.assertEqual(app.file_download_concurrency, 4)

    def test_legacy_configuration_without_per_file_setting_retains_one_worker(self):
        self.legacy.write_text('api_id: 123\napi_hash: offline\n')
        self.assertEqual(self.load().file_download_concurrency, 1)
