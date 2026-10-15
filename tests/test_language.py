"""Catalog coverage and compatibility across Bot, browser and saved configuration."""

import ast
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import lang
from module import bot
from module.language import Language, _t, set_language


ROOT = Path(__file__).resolve().parents[1]


class LanguageTests(unittest.TestCase):
    def tearDown(self):
        set_language(Language.EN)

    def test_legacy_and_regional_codes(self):
        for value, expected in [('ZH', 'zh-cn'), ('zh', 'zh-cn'), ('zh_CN', 'zh-cn'),
                                ('zh-TW', 'zh-tw'), ('zh-Hant-TW', 'zh-tw'),
                                ('zh-HK', 'zh-tw'), ('UA', 'uk'), ('ja-JP', 'ja'),
                                ('es-MX', 'es'), ('fr-CA', 'fr')]:
            with self.subTest(value=value):
                self.assertEqual(lang.parse_language(value).value, expected)
        self.assertIs(Language.ZH, Language.ZH_CN)
        self.assertIs(Language.UA, Language.UK)
        with self.assertRaises(ValueError):
            lang.parse_language('../config')

    def test_requested_languages_are_complete_and_preserve_fields(self):
        baseline = lang.CATALOGS['en']['messages']
        fields = lambda text: set(re.findall(r'\{\w+\}', text))
        for code in ('zh-cn', 'zh-tw', 'ja', 'es', 'fr'):
            messages = lang.CATALOGS[code]['messages']
            self.assertEqual(set(messages), set(baseline), code)
            for key, value in messages.items():
                self.assertEqual(fields(value), fields(baseline[key]), (code, key))
        # Both entry points resolve the same key instead of keeping separate dictionaries.
        for code in lang.CATALOGS:
            set_language(code)
            self.assertEqual(_t('Downloading'), lang.translate('Downloading', code))
        self.assertEqual(lang.translate('Retry failed: {error}', 'fr', error='{count}<file>'),
                         'Échec de la nouvelle tentative : {count}<file>')

    def test_missing_translation_falls_back_and_unknown_text_stays_literal(self):
        with patch.dict(lang.CATALOGS['fr']['messages'], clear=True):
            self.assertEqual(lang.translate('Pause', 'fr'), 'Pause')
        self.assertEqual(lang.translate('<filename>{unknown}', 'fr'), '<filename>{unknown}')

    def test_english_catalog_lists_each_key_once(self):
        data = json.loads((ROOT / 'lang/en.json').read_text())
        self.assertIsInstance(data['messages'], list)
        self.assertEqual(len(data['messages']), len(set(data['messages'])))
        self.assertNotIn('source_keys', data)
        self.assertNotIn('patterns', data)
        self.assertEqual(set(data['messages']), set(lang.CATALOGS['en']['messages']))

    def test_new_catalog_discovery_and_conflicting_alias_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'en.json').write_text(json.dumps(lang.CATALOGS['en']))
            data = {'code': 'de', 'name': 'Deutsch', 'aliases': ['de-de'],
                    'messages': {'Pause': 'Pausieren'}}
            (root / 'de.json').write_text(json.dumps(data))
            catalogs, aliases = lang.load_catalogs(root)
            self.assertEqual(catalogs['de']['messages']['Pause'], 'Pausieren')
            self.assertEqual(aliases['de-de'], 'de')
            data['aliases'] = ['en']
            (root / 'de.json').write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'Duplicate language alias'):
                lang.load_catalogs(root)

    def test_runtime_sources_keep_translations_in_catalogs(self):
        paths = [ROOT / 'media_downloader.py', ROOT / 'run_local.py',
                 *list((ROOT / 'module').glob('*.py')),
                 *list((ROOT / 'tools').glob('*.py')),
                 *list((ROOT / 'module/templates').glob('*.html')),
                 ROOT / 'module/static/web_i18n.js']
        for path in paths:
            self.assertIsNone(re.search(r'[\u3400-\u9fff]', path.read_text()), path.name)

    def test_current_english_reasons_translate_and_old_text_stays_literal(self):
        key = 'Retry failed: client unavailable'
        legacy = '重试失败: client不可用'
        for code in ('zh-cn', 'zh-tw', 'ja', 'es', 'fr'):
            with self.subTest(code=code):
                expected = lang.CATALOGS[code]['messages'][key]
                self.assertEqual(lang.translate(key, code), expected)
                self.assertEqual(lang.translate(legacy, code), legacy)
                self.assertEqual(lang.translate('Resume failed: <error>{count}', code),
                                 lang.translate('Resume failed: {error}', code, error='<error>{count}'))
        self.assertEqual(lang.translate('Download returned no file (failed after 3 retries)', 'es'),
                         'La descarga no devolvió ningún archivo (falló tras 3 reintentos)')

    def test_configuration_accepts_legacy_codes_and_saves_canonical_codes(self):
        from module.app import Application
        for value in ('ZH', 'zh-cn', 'zh-tw', 'ja', 'es', 'fr', 'UA'):
            with self.subTest(value=value):
                directory = tempfile.TemporaryDirectory()
                app = Application(str(Path(directory.name) / 'config.yaml'),
                                  str(Path(directory.name) / 'data.yaml'))
                try:
                    app.assign_config({'api_id': 'test', 'api_hash': 'test',
                                       'media_types': [], 'file_formats': {}, 'language': value})
                    app.update_config()
                    self.assertEqual(app.config['language'], lang.normalize_language(value))
                finally:
                    app.loop.close()
                    directory.cleanup()

    def test_all_literal_bot_translation_keys_exist(self):
        for path in [ROOT / 'media_downloader.py', *list((ROOT / 'module').glob('*.py'))]:
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == '_t' and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    key = node.args[0].value
                    self.assertIn(key, lang.CATALOGS['en']['messages'], (path.name, key))


class BotLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_accepts_new_codes_and_uses_native_name(self):
        from unittest.mock import AsyncMock
        for code in ('zh', 'zh-cn', 'zh-tw', 'ja', 'es', 'fr', 'ua'):
            with self.subTest(code=code):
                app = SimpleNamespace(set_language=set_language)
                client = SimpleNamespace(send_message=AsyncMock())
                message = SimpleNamespace(text=f'/set_language {code}',
                                          from_user=SimpleNamespace(id=12))
                try:
                    with patch.object(bot, '_bot', SimpleNamespace(app=app)):
                        await bot.set_language(client, message)
                    native_name = lang.CATALOGS[lang.normalize_language(code)]['name']
                    self.assertIn(native_name, client.send_message.await_args.args[1])
                finally:
                    set_language(Language.EN)

    async def test_retry_failures_persist_english_keys_for_later_locale_selection(self):
        from unittest.mock import AsyncMock, Mock
        from module.download_service import _async_retry_download
        from module import download_stat
        for backend, expected in (
            (SimpleNamespace(client=None), 'Retry failed: client unavailable'),
            (SimpleNamespace(client=object(), get_message_client=Mock(return_value=None)),
             'Retry failed: message client unavailable'),
            (SimpleNamespace(client=object(), get_message_client=Mock(return_value=SimpleNamespace(
                get_messages=AsyncMock(return_value=SimpleNamespace(empty=True))))),
             'Retry failed: message not found or deleted'),
        ):
            with self.subTest(expected=expected):
                with patch.object(bot, '_bot', backend), patch.object(download_stat, 'add_failed_download') as record:
                    set_language('ja')
                    try:
                        await _async_retry_download(10, 20, original_task_id='failed-task')
                    finally:
                        set_language('en')
                self.assertEqual(record.call_args.kwargs['error_message'], expected)
                self.assertEqual(record.call_args.kwargs['task_id'], 'failed-task')
                for code in ('zh-cn', 'zh-tw', 'ja', 'es', 'fr'):
                    self.assertEqual(lang.translate(expected, code), lang.CATALOGS[code]['messages'][expected])

    async def test_invalid_command_lists_discovered_languages(self):
        from unittest.mock import AsyncMock
        client = SimpleNamespace(send_message=AsyncMock())
        for text in ('/set_language', '/set_language invalid'):
            await bot.set_language(client, SimpleNamespace(text=text, from_user=SimpleNamespace(id=12)))
            self.assertIn('zh-cn', client.send_message.await_args.args[1])
            self.assertIn('zh-tw', client.send_message.await_args.args[1])
