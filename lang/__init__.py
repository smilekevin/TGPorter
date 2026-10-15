"""Shared UTF-8 JSON catalogs for Bot messages, logs and browser text.

Catalogs are discovered at startup. Add a file with code, name, aliases and
messages to register a language in configuration, Bot commands and the WebUI.
"""

import json
import re
from enum import Enum
from pathlib import Path


def load_catalogs(directory=None):
    """Load catalogs and validate metadata before exposing them to consumers."""
    catalogs = {}
    aliases = {}
    for path in sorted(Path(directory or Path(__file__).parent).glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        code = data['code']
        if not re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*', code) or path.stem != code:
            raise ValueError(f'Invalid language code in {path.name}')
        if not isinstance(data['name'], str) or not data['name']:
            raise ValueError(f'Invalid language name in {path.name}')
        if not isinstance(data['aliases'], list) or not all(isinstance(a, str) for a in data['aliases']):
            raise ValueError(f'Invalid language aliases in {path.name}')
        if not isinstance(data['messages'], dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v
            for k, v in data['messages'].items()
        ):
            raise ValueError(f'Invalid messages in {path.name}')
        catalogs[code] = data
        for alias in [code, *data['aliases']]:
            alias = alias.lower().replace('_', '-')
            if alias in aliases:
                raise ValueError(f'Duplicate language alias: {alias}')
            aliases[alias] = code
    if 'en' not in catalogs:
        raise ValueError('The English fallback catalog is required')
    return catalogs, aliases


CATALOGS, ALIASES = load_catalogs()
SOURCE_KEYS = CATALOGS['en'].get('source_keys', {})
MESSAGE_PATTERNS = CATALOGS['en'].get('patterns', [])
# Preserve import and enum member compatibility while discovering new languages.
_members = {code.upper().replace('-', '_'): code for code in CATALOGS}
_members.update({alias.upper().replace('-', '_'): code for alias, code in ALIASES.items()})
Language = Enum('Language', _members)
_language = Language.EN


def normalize_language(value):
    """Resolve legacy config codes and regional browser language tags."""
    if isinstance(value, Language):
        return value.value
    code = str(value or '').lower().replace('_', '-')
    while code:
        if code in ALIASES:
            return ALIASES[code]
        code = code.rsplit('-', 1)[0] if '-' in code else ''
    return None


def parse_language(value):
    """Return a language member or reject unsupported configuration input."""
    code = normalize_language(value)
    if code is None:
        raise ValueError('Unsupported language')
    return Language(code)


def set_language(language):
    """Set the Bot/log language independently of browser language selection."""
    global _language
    _language = parse_language(language)


def translate(text, language='en', **values):
    """Resolve a shared key, falling back to English then the original text."""
    key = SOURCE_KEYS.get(text, text)
    if isinstance(text, str) and key == text and key not in CATALOGS['en']['messages']:
        for rule in MESSAGE_PATTERNS:
            match = re.fullmatch(rule['pattern'], text, re.DOTALL)
            if match:
                key = rule['key']
                captured = dict(zip(rule['fields'], match.groups()))
                for field in rule.get('translate_fields', []):
                    captured[field] = translate(captured[field], language)
                values = {**captured, **values}
                break
    code = normalize_language(language) or 'en'
    result = CATALOGS[code]['messages'].get(key, CATALOGS['en']['messages'].get(key, text))
    # Replace named fields only: user filenames, braces and exception details stay literal.
    return re.sub(r'\{(\w+)\}', lambda m: str(values.get(m[1], m[0])), result)


def _t(text, **values):
    """Translate a Bot message with the configured process language."""
    return translate(text, _language, **values)


def template_context():
    """Provide both production and mock templates with the same browser catalogs."""
    catalogs = {code: {'name': data['name'], 'messages': data['messages']}
                for code, data in CATALOGS.items()}
    return {'language_catalogs': catalogs, 'language_aliases': ALIASES,
            'language_source_keys': SOURCE_KEYS, 'language_patterns': MESSAGE_PATTERNS}
