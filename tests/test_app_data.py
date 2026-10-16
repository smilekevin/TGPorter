"""Persist runtime changes while treating startup configuration as read-only."""

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from ruamel import yaml

from module import bot
from module.app import Application, DownloadStatus
from module.language import Language, set_language


class AppDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(set_language, Language.EN)
        self.root = Path(temporary.name)
        self.config_file = self.root / 'config.yaml'
        self.data_file = self.root / 'data.yaml'
        self.config_file.write_text(
            '# Preserve comments, spelling and initial values.\n'
            'api_id: test\napi_hash: test\nmedia_types: []\nfile_formats: {}\n'
            'language: ZH\nmax_download_task: 2\n'
            'filter_advertisement_list: [legacy-filter]\n'
            'replace_advertisement_list: [legacy-replacement]\n'
            'group_add_advertisement: {-1001: legacy-ad}\n'
            'chat:\n- chat_id: -1001\n  last_read_message_id: 20\n'
            '- chat_id: -1002\n  last_read_message_id: 30\n', encoding='utf-8')
        self.original = self.config_file.read_bytes()
        self.config_file.chmod(0o444)

    def load(self):
        app = Application(str(self.config_file), str(self.data_file))
        self.addCleanup(app.loop.close)
        self.addCleanup(app.executor.shutdown)
        app.load_config()
        return app

    def read_data(self):
        return yaml.YAML().load(self.data_file.read_text())

    def test_saves_restore_progress_retries_ads_and_language_without_writing_config(self):
        self.data_file.write_text(
            'other: keep\nchat:\n- chat_id: -1002\n  ids_to_retry: [31]\n'
            '- chat_id: -9999\n  ids_to_retry: [9]\n  custom: keep\n'
            '- chat_id: -1001\n  ids_to_retry: [21, 22, 23]\n', encoding='utf-8')
        app = self.load()
        initial_config = copy.deepcopy(app.config)
        chat = app.chat_download_config[-1001]
        chat.last_read_message_id = 200
        chat.node.download_status = {
            21: DownloadStatus.SuccessDownload, 22: DownloadStatus.SkipDownload,
            23: DownloadStatus.FailedDownload, 24: DownloadStatus.Downloading,
        }
        app.filter_advertisement_list.append('new-filter')
        app.replace_advertisement_list.clear()
        app.group_add_advertisement[-1001] = 'new-ad'
        app.set_language(Language.FR)
        app.max_download_task = 6
        app.save_app_data()

        self.assertEqual(self.config_file.read_bytes(), self.original)
        self.assertEqual(app.config, initial_config)
        data = self.read_data()
        self.assertEqual(data['other'], 'keep')
        self.assertNotIn('max_download_task', data)
        self.assertNotIn('api_id', data)
        self.assertNotIn('api_hash', data)
        records = {item['chat_id']: item for item in data['chat']}
        self.assertEqual(records[-1001]['last_read_message_id'], 201)
        self.assertEqual(records[-1001]['ids_to_retry'], [23, 24])
        self.assertEqual(records[-1002]['ids_to_retry'], [31])
        self.assertEqual(records[-9999], {'chat_id': -9999, 'ids_to_retry': [9], 'custom': 'keep'})
        restarted = self.load()
        self.assertEqual(restarted.chat_download_config[-1001].last_read_message_id, 201)
        self.assertEqual(restarted.chat_download_config[-1001].ids_to_retry_dict, {23: True, 24: True})
        self.assertEqual(restarted.filter_advertisement_list, ['legacy-filter', 'new-filter'])
        self.assertEqual(restarted.replace_advertisement_list, [])
        self.assertEqual(restarted.group_add_advertisement, {-1001: 'new-ad'})
        self.assertEqual(restarted.language, Language.FR)
        self.assertEqual(restarted.max_download_task, 2)

    def test_empty_runtime_rules_override_legacy_configuration(self):
        app = self.load()
        app.filter_advertisement_list.clear()
        app.replace_advertisement_list.clear()
        app.group_add_advertisement.clear()
        app.save_app_data()
        restarted = self.load()
        self.assertEqual(restarted.filter_advertisement_list, [])
        self.assertEqual(restarted.replace_advertisement_list, [])
        self.assertEqual(restarted.group_add_advertisement, {})
        self.assertEqual(self.config_file.read_bytes(), self.original)

    def test_legacy_single_chat_and_retry_file_still_load(self):
        self.config_file.chmod(0o644)
        self.config_file.write_text(
            'api_id: test\napi_hash: test\nmedia_types: []\nfile_formats: {}\n'
            'chat_id: -1001\nlast_read_message_id: 80\n', encoding='utf-8')
        self.data_file.write_text('ids_to_retry: [70]\n', encoding='utf-8')
        original = self.config_file.read_bytes()
        app = self.load()
        self.assertEqual(app.chat_download_config[-1001].last_read_message_id, 80)
        self.assertEqual(app.chat_download_config[-1001].ids_to_retry_dict, {70: True})
        app.save_app_data()
        self.assertEqual(self.config_file.read_bytes(), original)
        self.assertNotIn('ids_to_retry', self.read_data())
        self.assertEqual(self.load().chat_download_config[-1001].ids_to_retry, [70])

    def test_configured_start_after_saved_checkpoint_is_respected(self):
        self.data_file.write_text(
            'chat:\n- chat_id: -1001\n  last_read_message_id: 10\n', encoding='utf-8')
        self.assertEqual(self.load().chat_download_config[-1001].last_read_message_id, 20)

    def test_snapshot_only_does_not_create_files(self):
        app = self.load()
        app.save_app_data(False)
        self.assertFalse(self.data_file.exists())
        self.assertEqual(app.app_data['language'], 'zh-cn')
        self.assertEqual(self.config_file.read_bytes(), self.original)

    def test_saves_keep_file_inode_and_serialize_concurrent_writers(self):
        self.data_file.write_text('{}\n', encoding='utf-8')
        inode = self.data_file.stat().st_ino
        app = self.load()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: app.save_app_data(), range(12)))
        self.assertEqual(self.data_file.stat().st_ino, inode)
        self.assertEqual(self.read_data()['language'], 'zh-cn')
        self.assertEqual(self.config_file.read_bytes(), self.original)

    def test_serialization_failure_keeps_existing_data_file(self):
        self.data_file.write_text('{}\n', encoding='utf-8')
        app = self.load()
        app.app_data['unknown'] = object()
        with self.assertRaises(yaml.representer.RepresenterError):
            app.save_app_data()
        self.assertEqual(self.data_file.read_text(), '{}\n')
        self.assertEqual(self.config_file.read_bytes(), self.original)


class BotAdDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ad_commands_persist_to_data_and_empty_rules_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yaml'
            data = Path(directory) / 'data.yaml'
            config.write_text('api_id: test\napi_hash: test\nmedia_types: []\nfile_formats: {}\n')
            original = config.read_bytes()
            app = Application(str(config), str(data))
            restarted = None
            try:
                app.load_config()
                client = SimpleNamespace(send_message=AsyncMock())
                controller = SimpleNamespace(app=app, client=object())
                cases = (
                    (bot.add_filter_advertisement_filter, '/add_ad filter', 'filter_advertisement_list', ['filter']),
                    (bot.remove_filter_advertisement_filter, '/remove_ad filter', 'filter_advertisement_list', []),
                    (bot.set_add_advertisement, '/set_ad link ad text', 'group_add_advertisement', {-1001: 'ad text'}),
                    (bot.set_add_advertisement, '/set_ad link', 'group_add_advertisement', {}),
                    (bot.add_replace_advertisement_filter, '/add_replace_ad link filter', 'replace_advertisement_list', ['replacement']),
                    (bot.remove_replace_advertisement_filter, '/remove_replace_ad link filter', 'replace_advertisement_list', []),
                )
                with patch.object(bot, '_bot', controller), \
                        patch.object(bot, 'parse_link', AsyncMock(return_value=(-1001, 1, None))), \
                        patch.object(bot, 'proc_replace_advertisement', AsyncMock(return_value='replacement')):
                    for handler, text, key, expected in cases:
                        with self.subTest(command=text):
                            await handler(client, SimpleNamespace(text=text, from_user=SimpleNamespace(id=7)))
                            saved = yaml.YAML().load(data.read_text())
                            self.assertEqual(saved[key], expected)
                            self.assertEqual(config.read_bytes(), original)
                restarted = Application(str(config), str(data))
                restarted.load_config()
                self.assertEqual(restarted.filter_advertisement_list, [])
                self.assertEqual(restarted.replace_advertisement_list, [])
                self.assertEqual(restarted.group_add_advertisement, {})
            finally:
                for instance in (app, restarted):
                    if instance is not None:
                        instance.loop.close()
                        instance.executor.shutdown()


if __name__ == '__main__':
    unittest.main()
