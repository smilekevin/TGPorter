/* Browser language is independent of the Bot's process-wide language. */
(() => {
    'use strict';
    const {catalogs, aliases, patterns} = JSON.parse(document.getElementById('language-catalogs').textContent);
    const supported = Object.keys(catalogs);
    const storageKey = 'web_language';
    const isLoginPage = document.title === 'Sign in - TG Porter';
    function normalize(value) {
        const code = String(value || '').toLowerCase().replaceAll('_', '-');
        const parts = code.split('-');
        while (parts.length) {
            const match = aliases[parts.join('-')];
            if (match) return match;
            parts.pop();
        }
        return undefined;
    }
    function detectLanguage() {
        try {
            const saved = normalize(localStorage.getItem(storageKey));
            if (supported.includes(saved)) return saved;
        } catch (_) { /* Storage may be disabled in private browsing. */ }
        for (const value of navigator.languages || [navigator.language]) {
            const language = normalize(value);
            if (supported.includes(language)) return language;
        }
        return 'en';
    }
    const api = {
        language: detectLanguage(),
        t(text, language = api.language, values = {}) {
            if (text == null) return '';
            let key = text;
            if (typeof text === 'string' && !Object.hasOwn(catalogs.en.messages, key)) {
                for (const rule of patterns) {
                    const match = text.match(new RegExp(rule.pattern, 's'));
                    if (match) {
                        key = rule.key;
                        const captured = Object.fromEntries(rule.fields.map((field, index) => [field, match[index + 1]]));
                        for (const field of rule.translate_fields || []) captured[field] = api.t(captured[field], language);
                        values = {...captured, ...values};
                        break;
                    }
                }
            }
            const translated = catalogs[language]?.messages[key] ?? catalogs.en.messages[key] ?? text;
            return String(translated).replace(/\{(\w+)\}/g, (match, key) => values[key] ?? match);
        },
        apply() {
            document.documentElement.lang = api.language;
            document.querySelectorAll('[data-i18n]').forEach(element => {
                element.textContent = api.t(element.dataset.i18n);
            });
            document.querySelectorAll('[data-language-switch]').forEach(element => {
                element.value = api.language;
            });
            if (isLoginPage) {
                document.title = api.t('Sign in - TG Porter');
            }
        },
        setLanguage(value) {
            const language = normalize(value);
            if (!supported.includes(language)) return api.language;
            api.language = language;
            try { localStorage.setItem(storageKey, language); } catch (_) { /* Optional persistence. */ }
            api.apply();
            return language;
        },
    };
    window.WebI18n = api;
    document.documentElement.lang = api.language;
    document.addEventListener('DOMContentLoaded', () => {
        api.apply();
        document.querySelectorAll('[data-language-switch]').forEach(element => {
            element.addEventListener('change', () => api.setLanguage(element.value));
        });
    });
})();
