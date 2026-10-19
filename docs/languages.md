# 语言包 / Language catalogs

## 中文

Bot、已翻译日志和 WebUI 共用 `lang/` 包，每种语言一个 UTF-8 JSON 文件。使用 JSON 的原因是 Python 和浏览器都能直接读取，无需 XML 解析器、额外依赖或生成代码。重复词汇使用同一个英文键；语序不同的完整句子使用独立词条和 `{count}` 等命名占位符。

支持：`en`、`zh-cn`（简体中文）、`zh-tw`（繁体中文）、`ja`、`es`、`fr`、`ru`、`uk`。语言代码不区分大小写，接受下划线形式；旧 `ZH` / `zh` 回退 `zh-cn`，`UA` / `ua` 对应 `uk`。Bot 与网页各自选择语言。配置方法见[部署指南](../DEPLOY_CN.md)。

添加新语言：

1. 复制 `lang/fr.json` 等已翻译文件，使用规范代码命名，例如 `lang/de.json`。
2. 把 `code` 改为 `de`，`name` 改为 `Deutsch`，`aliases` 改为 `[]`（或不冲突的地区别名）。
3. 翻译 `messages` 的值，保留英文键及全部命名占位符、命令、Markdown 和必要空格。缺失的键会回退英语，然后回退原文；不要用空字符串代替缺失翻译。
4. 重启应用，网页语言选项和 Bot `/set_language` 帮助会自动加入新语言。无需修改 Python 枚举、JS 列表或 HTML 选项。
5. 在项目虚拟环境运行 `python -B -m unittest discover -s tests -p 'test*language.py' -v`。同时检查 Bot 通知、登录页和控制面板的自然语序与长度。

`lang/en.json` 的 `messages` 是英文词条列表，每个词条只写一次。其他语言的 `messages` 是英文词条到翻译的映射。代码直接使用英文键，不维护历史中文到英文的别名表。动态提示的匹配规则由英文占位符模板自动生成，无需单独维护。Python 通过 `lang._t()` 读取 Bot 当前语言，通过 `lang.translate()` 显式指定语言。`module/language.py` 保留旧导入兼容。网页通过 Jinja `tojson` 安全注入同一批词典，`web_i18n.js` 只负责浏览器选择和渲染；`run_local.py` 使用相同注入逻辑。

运行代码、注释及诊断日志使用英语，失败记录保存英文词条，按显示端的语言翻译。无法识别的历史中文记录直接显示原文，不进行转换。测试可以保留语言样本。

目前俄语与乌克兰语保留已有翻译，未翻译的新词条回退英语。下载的文件名、聊天名称和远端异常详情保持原文。

## English

The Bot, translated logs and WebUI share the `lang/` package, with one UTF-8 JSON file per language. JSON is readable by Python and browsers without XML parsers, extra dependencies or generated code. Shared vocabulary uses the same English key; complete sentences use separate entries with named placeholders such as `{count}` so languages can change word order.

Supported codes: `en`, `zh-cn` (Simplified Chinese), `zh-tw` (Traditional Chinese), `ja`, `es`, `fr`, `ru`, `uk`. Codes are case insensitive and accept underscores. Legacy `ZH` / `zh` resolve to `zh-cn`; `UA` / `ua` resolve to `uk`. Bot and browser language preferences are independent. See the [deployment guide](../DEPLOY.md) for configuration.

To add a language:

1. Copy a translated catalog such as `lang/fr.json` to a canonical filename, such as `lang/de.json`.
2. Set `code` to `de`, `name` to `Deutsch`, and `aliases` to `[]` or unique regional aliases.
3. Translate the values in `messages`. Preserve English keys, all named placeholders, commands, Markdown and significant whitespace. Missing keys fall back to English, then the original text; do not use empty strings for missing translations.
4. Restart. The WebUI selector and Bot `/set_language` help discover the new catalog automatically, without editing Python enums, JS lists or HTML options.
5. In the project virtual environment, run `python -B -m unittest discover -s tests -p 'test*language.py' -v`. Check natural wording and length in Bot notifications, sign-in and the dashboard as well.

The English catalog’s `messages` is a list of canonical English keys, with each key written once. Other languages map those English keys to translations. Code uses English keys directly without historical Chinese aliases. Matching rules for current dynamic messages are generated automatically from English placeholder templates. Python uses `lang._t()` for the configured Bot language and `lang.translate()` for an explicit language. `module/language.py` preserves existing imports. Jinja safely embeds the same catalogs using `tojson`; `web_i18n.js` handles browser preferences and rendering. `run_local.py` uses the same injection.

Runtime code, comments and diagnostics use English. Persist failures as English keys and translate at display time. Unrecognized historical Chinese records display their original text without conversion. Tests may retain language fixtures.

Existing Russian and Ukrainian translations are preserved; untranslated additions fall back to English. Downloaded filenames, chat names and remote exception details retain their original text.
