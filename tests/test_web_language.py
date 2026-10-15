"""Browser language selection and shared production page integration."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

from module import web
from lang import CATALOGS, ALIASES, MESSAGE_PATTERNS


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser language tests")
class WebLanguageTests(unittest.TestCase):
    def test_browser_selection_switching_and_storage_failures(self):
        source = (ROOT / "module/static/web_i18n.js").read_text()
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
function boot(languages, saved, blocked = false, title = 'Sign in - Telegram Media Downloader') {
    const label = { dataset: { i18n: 'Downloading' }, textContent: '' };
    const select = { value: '', addEventListener() {} };
    const document = {
        documentElement: {}, title,
        getElementById: () => ({textContent: JSON.stringify(PAYLOAD)}),
        querySelectorAll: selector => selector === '[data-i18n]' ? [label] : [select],
        addEventListener: (event, callback) => callback(),
    };
    const storage = new Map(saved ? [['web_language', saved]] : []);
    const context = {
        window: {}, document, navigator: { languages },
        localStorage: {
            getItem(key) { if (blocked) throw Error('blocked'); return storage.get(key); },
            setItem(key, value) { if (blocked) throw Error('blocked'); storage.set(key, value); },
        },
    };
    vm.runInNewContext(SOURCE, context);
    return { api: context.window.WebI18n, document, label, select, storage };
}
for (const [languages, expected] of [
    [['zh-TW'], 'zh-tw'], [['zh-Hans-CN'], 'zh-cn'], [['zh-Hant-TW'], 'zh-tw'], [['zh'], 'zh-cn'], [['en-US'], 'en'], [['ru-RU'], 'ru'], [['uk-UA'], 'uk'],
    [['de-DE', 'ru-RU'], 'ru'], [['fr-FR'], 'fr'], [['ja-JP'], 'ja'], [['es-ES'], 'es'], [[], 'en'],
]) {
    const { api, document } = boot(languages);
    assert.equal(api.language, expected);
    assert.equal(document.documentElement.lang, expected);
}
const { api, document, label, select, storage } = boot(['zh-CN'], 'ru');
assert.equal(api.language, 'ru');
assert.equal(label.textContent, 'Загрузка');
assert.equal(select.value, 'ru');
assert.equal(api.setLanguage('uk-UA'), 'uk');
assert.equal(storage.get('web_language'), 'uk');
assert.equal(label.textContent, 'Завантаження');
assert.equal(document.title, 'Вхід - Telegram Media Downloader');
assert.equal(api.t('Tasks: {count}', 'en', {count: 12}), 'Tasks: 12');
assert.equal(api.t('Queued 3 tasks for retry; 1 failed', 'en'), 'Queued 3 tasks for retry; 1 failed');
assert.equal(api.t('<user filename>.mp4'), '<user filename>.mp4');
assert.equal(api.t('重试失败: client不可用', 'fr'), '重试失败: client不可用');
assert.equal(api.t('Task paused', 'ja'), 'タスクを一時停止しました');
assert.equal(api.t('Pause', 'es'), 'Pausar');
assert.equal(api.t('Pause', 'fr'), 'Mettre en pause');
assert.equal(api.t('Downloading', 'zh-tw'), '下載中');
assert.equal(api.t('Retry failed: <bad>{count}', 'fr'), 'Échec de la nouvelle tentative : <bad>{count}');
assert.equal(boot(['en'], 'zh').api.language, 'zh-cn');
assert.equal(boot(['en'], 'zh_Hant').api.language, 'zh-tw');
assert.equal(api.t('Tasks: {count}', 'invalid', {count: 4}), 'Tasks: 4');
assert.equal(api.setLanguage('invalid'), 'uk');
assert.equal(boot(['zh-CN'], 'invalid').api.language, 'zh-cn');
const denied = boot(['ru-RU'], null, true);
assert.equal(denied.api.language, 'ru');
assert.equal(denied.api.setLanguage('en'), 'en');
assert.equal(boot(['uk-UA'], 'ua').api.language, 'uk');
const dashboard = boot(['en-US'], null, false, 'Telegram Media Downloader');
dashboard.api.setLanguage('uk');
assert.equal(dashboard.document.title, 'Telegram Media Downloader');
""".replace("SOURCE", json.dumps(source)).replace("PAYLOAD", json.dumps({"catalogs": CATALOGS, "aliases": ALIASES, "patterns": MESSAGE_PATTERNS}))
        result = subprocess.run(["node"], input=script, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_pages_load_shared_catalog_and_dashboard_expressions_compile(self):
        flask_app = web.get_flask_app()
        with patch.object(web, "_app", SimpleNamespace(web_login_secret="")), \
                patch.dict(flask_app.config, TESTING=True):
            client = flask_app.test_client()
            dashboard = client.get("/").get_data(as_text=True)
            with client.get("/static/web_i18n.js") as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("detectLanguage", response.get_data(as_text=True))
        with patch.object(web, "_app", SimpleNamespace(web_login_secret="test")):
            login = client.get("/login").get_data(as_text=True)
        for page in (dashboard, login):
            self.assertIn('/static/web_i18n.js', page)
            self.assertIn('data-language-switch', page)
            for code in CATALOGS:
                self.assertIn(f'value="{code}"', page)
            payload = json.loads(re.search(r'<script id="language-catalogs" type="application/json">(.*?)</script>', page, re.DOTALL)[1])
            self.assertEqual(payload['catalogs'], {code: {'name': data['name'], 'messages': data['messages']}
                                                  for code, data in CATALOGS.items()})
            self.assertIn('简体中文', page)
            self.assertIn('繁體中文', page)

        class Expressions(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = []

            def handle_starttag(self, tag, attrs):
                for key, value in attrs:
                    if value and (key.startswith((':', '@')) or key in ('x-text', 'x-show', 'x-if', 'x-init', 'x-data')):
                        self.values.append((key.startswith('@') or key == 'x-init', value))

        parser = Expressions()
        parser.feed(dashboard)
        script = re.findall(r'<script>(.*?)</script>', dashboard, re.DOTALL)[-1]
        code = 'new Function(' + json.dumps(script) + ');\n'
        code += ('for (const [statement, expression] of ' + json.dumps(parser.values)
                 + ") new Function(statement ? expression : 'return (' + expression + ')');")
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
