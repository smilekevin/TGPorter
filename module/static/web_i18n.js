/* Browser language is independent of the Bot's process-wide language. */
(() => {
    'use strict';
    const translations = {
    "记住我（7 天）": ["Remember me (7 days)", "Запомнить меня (7 дней)", "Запам’ятати мене (7 днів)"],
    "Telegram 消息链接": ["Telegram post link", "Ссылка на сообщение Telegram", "Посилання на повідомлення Telegram"],
    "正在提交...": ["Submitting...", "Отправка...", "Надсилання..."],
    "提交下载": ["Submit download", "Добавить загрузку", "Додати завантаження"],
    "提交单条 Telegram 消息链接，使用服务端已登录的账号下载。": ["Submit one Telegram post link using the account already logged in on the server.", "Добавьте ссылку на сообщение для загрузки через аккаунт сервера.", "Додайте посилання на повідомлення для завантаження через обліковий запис сервера."],
    "提交失败": ["Submission failed", "Не удалось добавить задачу", "Не вдалося додати завдання"],
    "任务已存在": ["Task already exists", "Задача уже существует", "Завдання вже існує"],
    "已提交到下载队列": ["Added to download queue", "Добавлено в очередь загрузки", "Додано до черги завантаження"],
    "勾选后清理本地文件和下载临时数据；取消勾选则保留。": ["Select to remove local files and partial downloads; deselect to keep them.", "Отметьте для удаления локальных файлов и временных данных; снимите для сохранения.", "Позначте для видалення локальних файлів і тимчасових даних; зніміть для збереження."],

    " 个": [
        " items",
        " шт.",
        " шт."
    ],
    " 个任务": [
        " tasks",
        " задач",
        " завдань"
    ],
    ", 不存在 ": [
        ", missing ",
        ", отсутствует ",
        ", відсутні "
    ],
    "TG 限速等待中": [
        "Waiting for Telegram rate limit",
        "Ожидание лимита Telegram",
        "Очікування ліміту Telegram"
    ],
    "TG 静默限速中": [
        "Telegram speed limited",
        "Telegram ограничивает скорость",
        "Telegram обмежує швидкість"
    ],
    "Telegram API 频率限制，正在等待恢复": [
        "Telegram API rate limit; waiting to resume",
        "Лимит запросов Telegram API; ожидаем возобновления",
        "Ліміт запитів Telegram API; очікуємо відновлення"
    ],
    "下载中": [
        "Downloading",
        "Загрузка",
        "Завантаження"
    ],
    "下载速度持续低于 200 KB/s，已被 Telegram 限制": [
        "Speed remains below 200 KB/s; limited by Telegram",
        "Скорость ниже 200 КБ/с; ограничена Telegram",
        "Швидкість нижча за 200 КБ/с; обмежена Telegram"
    ],
    "个任务吗？删除后无法恢复。": [
        "tasks? This cannot be undone.",
        "задач? Это действие необратимо.",
        "завдань? Цю дію неможливо скасувати."
    ],
    "任务和本地文件已删除": [
        "Task and local file deleted",
        "Задача и локальный файл удалены",
        "Завдання та локальний файл видалено"
    ],
    "任务已删除": [
        "Task deleted",
        "Задача удалена",
        "Завдання видалено"
    ],
    "任务已删除（文件: ": [
        "Task deleted (file: ",
        "Задача удалена (файл: ",
        "Завдання видалено (файл: "
    ],
    "任务已暂停": [
        "Task paused",
        "Задача приостановлена",
        "Завдання призупинено"
    ],
    "任务已继续": [
        "Task resumed",
        "Задача возобновлена",
        "Завдання відновлено"
    ],
    "任务开始后会在这里显示": [
        "Tasks will appear here when they start",
        "Задачи появятся здесь после запуска",
        "Завдання з’являться тут після запуску"
    ],
    "保存中...": [
        "Saving...",
        "Сохранение...",
        "Збереження..."
    ],
    "全选": [
        "Select all",
        "Выбрать всё",
        "Вибрати все"
    ],
    "全部暂停": [
        "Pause all",
        "Приостановить всё",
        "Призупинити все"
    ],
    "全部继续": [
        "Resume all",
        "Возобновить всё",
        "Відновити все"
    ],
    "创建于 ": [
        "Created ",
        "Создано ",
        "Створено "
    ],
    "删除": [
        "Delete",
        "Удалить",
        "Видалити"
    ],
    "删除失败": [
        "Deletion failed",
        "Не удалось удалить",
        "Не вдалося видалити"
    ],
    "删除选中": [
        "Delete selected",
        "Удалить выбранное",
        "Видалити вибране"
    ],
    "剩余 ": [
        "Remaining ",
        "Осталось ",
        "Залишилося "
    ],
    "加载中...": [
        "Loading...",
        "Загрузка...",
        "Завантаження..."
    ],
    "取消": [
        "Cancel",
        "Отмена",
        "Скасувати"
    ],
    "取消选择": [
        "Clear selection",
        "Снять выделение",
        "Зняти вибір"
    ],
    "同时删除本地文件": [
        "Also delete local files",
        "Также удалить локальные файлы",
        "Також видалити локальні файли"
    ],
    "失败": [
        "Failed",
        "Ошибки",
        "Помилки"
    ],
    "失败于 ": [
        "Failed ",
        "Ошибка ",
        "Помилка "
    ],
    "失败的下载会在这里显示": [
        "Failed downloads will appear here",
        "Здесь появятся неудачные загрузки",
        "Тут з’являться невдалі завантаження"
    ],
    "完成于 ": [
        "Completed ",
        "Завершено ",
        "Завершено "
    ],
    "完成的下载会在这里显示": [
        "Completed downloads will appear here",
        "Здесь появятся завершённые загрузки",
        "Тут з’являться завершені завантаження"
    ],
    "尝试其他关键词": [
        "Try other keywords",
        "Попробуйте другие ключевые слова",
        "Спробуйте інші ключові слова"
    ],
    "已全部暂停": [
        "All tasks paused",
        "Все задачи приостановлены",
        "Усі завдання призупинено"
    ],
    "已全部继续": [
        "All tasks resumed",
        "Все задачи возобновлены",
        "Усі завдання відновлено"
    ],
    "已删除 ": [
        "Deleted ",
        "Удалено ",
        "Видалено "
    ],
    "已加入重试队列": [
        "Queued for retry",
        "Добавлено в очередь повторных попыток",
        "Додано до черги повторних спроб"
    ],
    "已加载全部": [
        "All loaded:",
        "Загружено всё:",
        "Завантажено все:"
    ],
    "已完成": [
        "Completed",
        "Завершено",
        "Завершено"
    ],
    "已暂停": [
        "Paused",
        "Приостановлено",
        "Призупинено"
    ],
    "已移除待下载任务": [
        "Pending task removed",
        "Ожидающая задача удалена",
        "Завдання з черги видалено"
    ],
    "已等待 ": [
        "Waiting ",
        "Ожидание ",
        "Очікування "
    ],
    "已选择": [
        "Selected",
        "Выбрано",
        "Вибрано"
    ],
    "并发": [
        "Concurrent",
        "Параллельно",
        "Паралельно"
    ],
    "并发任务数": [
        "Concurrent tasks",
        "Параллельные задачи",
        "Паралельні завдання"
    ],
    "并发任务数已设置为 ": [
        "Concurrent tasks set to ",
        "Число параллельных задач: ",
        "Кількість паралельних завдань: "
    ],
    "待下载": [
        "Pending",
        "В очереди",
        "У черзі"
    ],
    "批量重试失败": [
        "Batch retry failed",
        "Ошибка массового повтора",
        "Помилка масового повтору"
    ],
    "按接收时间排序 · 最早优先": [
        "By arrival time · oldest first",
        "По времени получения · сначала старые",
        "За часом отримання · спочатку найстаріші"
    ],
    "接收于 ": [
        "Received ",
        "Получено ",
        "Отримано "
    ],
    "搜索任务ID或文件名...": [
        "Search task ID or filename...",
        "Поиск по ID задачи или имени файла...",
        "Пошук за ID завдання або назвою файлу..."
    ],
    "操作失败": [
        "Operation failed",
        "Операция не выполнена",
        "Операцію не виконано"
    ],
    "文件状态检查失败（网络波动），可重试打开": [
        "Could not check file status; reopen to retry",
        "Не удалось проверить файл; откройте снова",
        "Не вдалося перевірити файл; відкрийте знову"
    ],
    "暂停": [
        "Pause",
        "Приостановить",
        "Призупинити"
    ],
    "暂停失败": [
        "Pause failed",
        "Не удалось приостановить",
        "Не вдалося призупинити"
    ],
    "暂无下载任务": [
        "No active downloads",
        "Нет активных загрузок",
        "Немає активних завантажень"
    ],
    "暂无失败任务": [
        "No failed tasks",
        "Нет задач с ошибками",
        "Немає завдань з помилками"
    ],
    "暂无已完成任务": [
        "No completed tasks",
        "Нет завершённых задач",
        "Немає завершених завдань"
    ],
    "暂无待下载任务": [
        "No pending tasks",
        "Нет задач в очереди",
        "Немає завдань у черзі"
    ],
    "未找到匹配的任务": [
        "No matching tasks",
        "Совпадений не найдено",
        "Збігів не знайдено"
    ],
    "未知错误": [
        "Unknown error",
        "Неизвестная ошибка",
        "Невідома помилка"
    ],
    "本地文件已不存在": [
        "Local file no longer exists",
        "Локальный файл больше не существует",
        "Локальний файл більше не існує"
    ],
    "条": [
        "items",
        "записей",
        "записів"
    ],
    "正在检查文件...": [
        "Checking files...",
        "Проверка файлов...",
        "Перевірка файлів..."
    ],
    "正在获取文件信息...": [
        "Fetching file information...",
        "Получение сведений о файле...",
        "Отримання відомостей про файл..."
    ],
    "消息数据获取中": [
        "Fetching message",
        "Получение сообщения",
        "Отримання повідомлення"
    ],
    "登录": [
        "Sign in",
        "Войти",
        "Увійти"
    ],
    "登录 - Telegram Media Downloader": [
        "Sign in - Telegram Media Downloader",
        "Вход - Telegram Media Downloader",
        "Вхід - Telegram Media Downloader"
    ],
    "登录控制面板": [
        "Sign in to dashboard",
        "Вход в панель управления",
        "Вхід до панелі керування"
    ],
    "确定要删除此任务吗？删除后无法恢复。": [
        "Delete this task? This cannot be undone.",
        "Удалить эту задачу? Это действие необратимо.",
        "Видалити це завдання? Цю дію неможливо скасувати."
    ],
    "确定要删除选中的": [
        "Delete selected",
        "Удалить выбранные",
        "Видалити вибрані"
    ],
    "确认删除": [
        "Delete",
        "Удалить",
        "Видалити"
    ],
    "确认删除任务": [
        "Confirm task deletion",
        "Подтвердите удаление задач",
        "Підтвердьте видалення завдань"
    ],
    "移除": [
        "Remove",
        "Убрать",
        "Прибрати"
    ],
    "移除失败": [
        "Removal failed",
        "Не удалось убрать",
        "Не вдалося прибрати"
    ],
    "等待中": [
        "Waiting",
        "Ожидание",
        "Очікування"
    ],
    "继续": [
        "Resume",
        "Возобновить",
        "Відновити"
    ],
    "继续失败": [
        "Resume failed",
        "Не удалось возобновить",
        "Не вдалося відновити"
    ],
    "网络错误: ": [
        "Network error: ",
        "Ошибка сети: ",
        "Помилка мережі: "
    ],
    "自动恢复": [
        "Resumes automatically",
        "Возобновится автоматически",
        "Відновиться автоматично"
    ],
    "设置失败": [
        "Could not save setting",
        "Не удалось сохранить настройку",
        "Не вдалося зберегти налаштування"
    ],
    "访问密码": [
        "Password",
        "Пароль",
        "Пароль"
    ],
    "进度": [
        "Progress",
        "Прогресс",
        "Прогрес"
    ],
    "退出": [
        "Sign out",
        "Выйти",
        "Вийти"
    ],
    "通过 Telegram Bot 发送的链接会在这里排队等待下载": [
        "Links sent to the Telegram Bot will queue here",
        "Ссылки, отправленные боту Telegram, появятся здесь",
        "Посилання, надіслані боту Telegram, з’являться тут"
    ],
    "重试": [
        "Retry",
        "Повторить",
        "Повторити"
    ],
    "重试失败": [
        "Retry failed",
        "Не удалось повторить",
        "Не вдалося повторити"
    ],
    "重试选中": [
        "Retry selected",
        "Повторить выбранное",
        "Повторити вибране"
    ],
    "需要重新登录": [
        "Please sign in again",
        "Войдите снова",
        "Увійдіть знову"
    ],
    "项": [
        "items",
        "элементов",
        "елементів"
    ],
    "（文件: 成功删除 ": [
        " (files deleted: ",
        " (удалено файлов: ",
        " (видалено файлів: "
    ],
    "页面已过期，请重新输入密码。": [
        "Page expired. Please enter your password again.",
        "Страница устарела. Введите пароль снова.",
        "Сторінка застаріла. Введіть пароль знову."
    ],
    "密码错误，请重试。": [
        "Incorrect password. Please try again.",
        "Неверный пароль. Попробуйте ещё раз.",
        "Неправильний пароль. Спробуйте ще раз."
    ],
    "未知": [
        "Unknown",
        "Неизвестно",
        "Невідомо"
    ],
    "频道消息": [
        "Channel post",
        "Сообщение канала",
        "Допис каналу"
    ],
    "转发消息": [
        "Forwarded message",
        "Пересланное сообщение",
        "Переслане повідомлення"
    ],
    "消息": [
        "Message",
        "Сообщение",
        "Повідомлення"
    ],
    "应用尚未初始化完成": [
        "Application is still starting",
        "Приложение ещё запускается",
        "Застосунок ще запускається"
    ],
    "）": [
        ")",
        ")",
        ")"
    ],
    "任务数：{count}": [
        "Tasks: {count}",
        "Задач: {count}",
        "Завдань: {count}"
    ],
    "已选择 {count} 项": [
        "Selected: {count}",
        "Выбрано: {count}",
        "Вибрано: {count}"
    ],
    "已加载全部 {count} 条": [
        "All {count} items loaded",
        "Загружено записей: {count}",
        "Завантажено записів: {count}"
    ],
    "确定要删除选中的 {count} 个任务吗？删除后无法恢复。": [
        "Delete {count} selected tasks? This cannot be undone.",
        "Удалить выбранные задачи ({count})? Это действие необратимо.",
        "Видалити вибрані завдання ({count})? Цю дію неможливо скасувати."
    ]
};
    const supported = ['zh', 'en', 'ru', 'uk'];
    const storageKey = 'web_language';
    const isLoginPage = document.title === '登录 - Telegram Media Downloader';
    function normalize(value) {
        const base = String(value || '').toLowerCase().split(/[-_]/)[0];
        return base === 'ua' ? 'uk' : base;
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
            // Translate structured service messages while preserving exception details.
            if (language !== 'zh' && typeof text === 'string') {
                const queued = text.match(/^已加入重试队列 (\d+) 个任务(?:，(\d+) 个失败)?$/);
                if (queued) {
                    return api.t('已加入重试队列', language) + ': ' + queued[1] +
                        (queued[2] ? ' (' + api.t('失败', language) + ': ' + queued[2] + ')' : '');
                }
                const retryError = text.match(/^重试失败: (.*)$/s);
                if (retryError) return api.t('重试失败', language) + ': ' + retryError[1];
            }
            const row = translations[text];
            const translated = language === 'zh' || !row ? text : row[supported.indexOf(language) - 1] || text;
            return String(translated).replace(/\{(\w+)\}/g, (match, key) => values[key] ?? match);
        },
        apply() {
            document.documentElement.lang = api.language === 'zh' ? 'zh-CN' : api.language;
            document.querySelectorAll('[data-i18n]').forEach(element => {
                element.textContent = api.t(element.dataset.i18n);
            });
            document.querySelectorAll('[data-language-switch]').forEach(element => {
                element.value = api.language;
            });
            if (isLoginPage) {
                document.title = api.t('登录 - Telegram Media Downloader');
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
    document.documentElement.lang = api.language === 'zh' ? 'zh-CN' : api.language;
    document.addEventListener('DOMContentLoaded', () => {
        api.apply();
        document.querySelectorAll('[data-language-switch]').forEach(element => {
            element.addEventListener('change', () => api.setLanguage(element.value));
        });
    });
})();
