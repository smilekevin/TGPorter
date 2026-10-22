/* Native settings editors; credentials and authorization remain server-side. */
(() => {
    'use strict';
    const config = JSON.parse(document.getElementById('configuration-data').textContent);
    const t = text => WebI18n.t(text), byId = id => document.getElementById(id);
    const basic = new Set(['api_id', 'api_hash', 'bot_token', 'use_user_account', 'proxy']);
    const dirty = new Set(), editors = new Map();
    let csrf = config.csrf, telegram = {}, restart = [], reconnect = [], reauthorize = false, savedUserMode = false;
    let timer, polling = false, loaded = false;
    function notice(text, error = false) {
        byId('notice').hidden = !text;
        byId('notice').classList.toggle('error', error);
        byId('notice').textContent = t(text);
    }
    async function request(path, data) {
        const response = await fetch(path, {method: data ? 'POST' : 'GET', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
            body: data ? JSON.stringify(data) : undefined});
        const body = await response.json();
        if (response.status === 401) { location.href = '/login'; throw new Error('Sign in again'); }
        if (!response.ok) throw new Error(body.error || 'Operation failed');
        return body;
    }
    async function withButton(button, action) {
        button.disabled = true;
        try { await action(); } catch (error) { notice(error.message, true); }
        finally { button.disabled = false; if (!config.setup) renderTelegram(telegram); }
    }
    if (config.setup) {
        byId('setup-form').addEventListener('submit', event => {
            event.preventDefault();
            withButton(event.submitter, async () => {
                await request('/api/setup', {code: byId('setup-code').value, password: byId('setup-password').value});
                byId('setup-password').value = '';
                location.reload();
            });
        });
        return;
    }
    function changed(key) { dirty.add(key); renderTelegram(telegram); }
    function node(tag, text, className) {
        const result = document.createElement(tag);
        if (text !== undefined) result.textContent = t(text);
        if (className) result.className = className;
        return result;
    }
    function checkbox(parent, text, checked, change) {
        const label = node('label', undefined, 'choice'), input = node('input');
        input.type = 'checkbox'; input.checked = checked;
        input.addEventListener('change', () => change(input.checked));
        label.append(input, node('span', text)); parent.append(label);
        return input;
    }
    function addInput(parent, placeholder, add) {
        const row = node('div', undefined, 'add-row'), input = node('input'), button = node('button', 'Add', 'secondary');
        input.placeholder = t(placeholder); input.setAttribute('aria-label', t(placeholder));
        button.type = 'button';
        const submit = async () => {
            const value = input.value.trim();
            if (!value || button.disabled) return;
            input.setCustomValidity('');
            button.disabled = true;
            try { await add(value); input.value = ''; }
            catch (error) { input.setCustomValidity(t(error.message)); input.reportValidity(); }
            finally { button.disabled = false; }
        };
        input.addEventListener('input', () => input.setCustomValidity(''));
        button.addEventListener('click', submit);
        input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); submit(); } });
        row.append(input, button); parent.append(row);
    }
    function listEditor(key, values) {
        const host = byId(`setting-${key}`), list = [...values];
        const draw = () => {
            host.replaceChildren();
            const chips = node('div', undefined, 'chips');
            list.forEach((value, index) => {
                const chip = node('span', undefined, 'chip'), remove = node('button', '×');
                remove.type = 'button'; remove.setAttribute('aria-label', `${t('Remove')} ${value}`);
                remove.addEventListener('click', () => { list.splice(index, 1); changed(key); draw(); });
                chip.append(node('span', String(value)), remove); chips.append(chip);
            });
            host.append(chips);
            const placeholder = key === 'allowed_user_ids' ? 'User ID or username'
                : key === 'filter_advertisement_list' ? 'Add advertisement keyword' : 'Hostname or IP with optional port';
            addInput(host, placeholder, async text => {
                let value = text;
                if (key === 'mcp_allowed_hosts') {
                    value = (await request('/api/configuration/validate-host', {host: text})).host;
                }
                if (key === 'allowed_user_ids' && /^-?\d+$/.test(text) && Number.isSafeInteger(Number(text))) value = Number(text);
                if (!list.some(item => String(item) === String(value))) { list.push(value); changed(key); draw(); }
            });
        };
        draw(); editors.set(key, () => [...list]);
    }
    function filterEditor(values) {
        const host = byId('setting-download_filters'), rules = [...values];
        const list = node('ul', undefined, 'filter-rules'), input = node('textarea');
        const actions = node('div', undefined, 'actions'), status = node('p', undefined, 'help filter-status');
        const check = node('button', 'Check filter', 'secondary'), add = node('button', 'Add');
        input.id = 'filter-expression'; input.rows = 3; input.maxLength = config.filter_limits.length;
        input.placeholder = 'media_file_size >= 100MB and media_duration <= 600';
        input.setAttribute('aria-label', t('Filter expression'));
        input.setAttribute('aria-describedby', 'filter-validation');
        input.spellcheck = false;
        status.id = 'filter-validation'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        for (const button of [check, add]) button.type = 'button';
        const draw = () => {
            list.replaceChildren();
            rules.forEach((expression, index) => {
                const row = node('li'), code = node('code'), remove = node('button', 'Remove', 'secondary');
                code.textContent = expression; remove.type = 'button';
                remove.setAttribute('aria-label', `${t('Remove')}: ${expression}`);
                remove.addEventListener('click', () => { rules.splice(index, 1); changed('download_filters'); draw(); });
                row.append(code, remove); list.append(row);
            });
        };
        const validate = async append => {
            const draft = input.value;
            check.disabled = add.disabled = true;
            try {
                const {expression} = await request('/api/configuration/validate-filter', {expression: draft});
                if (input.value !== draft || !input.isConnected) return;
                if (append && !rules.includes(expression)) {
                    if (rules.length >= config.filter_limits.count) throw new Error(`Download filters cannot exceed ${config.filter_limits.count} rules`);
                    rules.push(expression); changed('download_filters'); draw();
                }
                status.textContent = t(append ? 'Filter added. Save settings to apply.' : 'Filter expression is valid');
                status.classList.remove('error'); status.classList.add('success');
                input.setAttribute('aria-invalid', 'false');
                if (append) input.value = '';
            } catch (error) {
                if (input.value !== draft || !input.isConnected) return;
                status.textContent = t(error.message); status.classList.remove('success'); status.classList.add('error');
                input.setAttribute('aria-invalid', 'true'); input.focus();
            } finally { check.disabled = add.disabled = false; }
        };
        input.addEventListener('input', () => { status.textContent = ''; input.removeAttribute('aria-invalid'); });
        check.addEventListener('click', () => validate(false));
        add.addEventListener('click', () => validate(true));
        input.addEventListener('keydown', event => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !add.disabled) {
                event.preventDefault(); validate(true);
            }
        });
        actions.append(check, add); host.replaceChildren(list, input, actions, status); draw();
        editors.set('download_filters', () => [...rules]);
    }
    const media = [['audio', 'Audio'], ['document', 'Documents'], ['photo', 'Photos'], ['video', 'Videos'],
        ['voice', 'Voice messages'], ['animation', 'Animations'], ['video_note', 'Video messages'], ['sticker', 'Stickers']];
    const naming = {
        file_path_prefix: [['from_user_id', 'Requester user ID'], ['chat_title', 'Chat title'], ['media_datetime', 'Media date'], ['media_type', 'Media type']],
        file_name_prefix: [['message_id', 'Message ID'], ['file_name', 'Original file name'], ['caption', 'Caption']]
    };
    function choiceEditor(key, values, options, ordered = false) {
        const host = byId(`setting-${key}`), selected = [...values];
        const draw = () => {
            if (key === 'file_path_prefix') {
                document.querySelector('[data-field="date_format"]').hidden = !selected.includes('media_datetime');
            }
            host.replaceChildren();
            const choices = node('div', undefined, 'choices');
            const available = [...options, ...selected.filter(value => !options.some(([id]) => id === value)).map(value => [value, value])];
            for (const [value, label] of available) checkbox(choices, label, selected.includes(value), enabled => {
                if (enabled) selected.push(value); else selected.splice(selected.indexOf(value), 1);
                changed(key); draw();
            });
            host.append(choices);
            if (ordered && selected.length) {
                host.append(node('p', 'Selected order', 'help'));
                const order = node('div', undefined, 'chips');
                selected.forEach((value, index) => {
                    const chip = node('span', undefined, 'chip');
                    chip.append(node('span', available.find(([id]) => id === value)[1]));
                    for (const [symbol, delta, title] of [['↑', -1, 'Move earlier'], ['↓', 1, 'Move later']]) {
                        const button = node('button', symbol); button.type = 'button';
                        button.setAttribute('aria-label', `${t(title)}: ${t(available.find(([id]) => id === value)[1])}`);
                        button.disabled = index + delta < 0 || index + delta >= selected.length;
                        button.addEventListener('click', () => {
                            [selected[index], selected[index + delta]] = [selected[index + delta], selected[index]];
                            changed(key); draw();
                        });
                        chip.append(button);
                    }
                    order.append(chip);
                });
                host.append(order);
            }
        };
        draw(); editors.set(key, () => [...selected]);
    }
    function formatEditor(values) {
        const host = byId('setting-file_formats'), selected = structuredClone(values);
        const common = {audio: ['mp3', 'm4a', 'flac', 'wav', 'ogg', 'aac'],
            document: ['pdf', 'epub', 'txt', 'zip', 'rar', '7z'], video: ['mp4', 'mkv', 'mov', 'webm', 'avi']};
        const draw = () => {
            host.replaceChildren();
            for (const kind of new Set([...Object.keys(common), ...Object.keys(selected)])) {
                const group = node('fieldset'), legend = node('legend', media.find(([id]) => id === kind)?.[1] || kind);
                const choices = node('div', undefined, 'choices'), current = selected[kind] || [];
                const extensions = new Set(['all', ...(common[kind] || []), ...current]);
                group.append(legend, choices);
                for (const extension of extensions) checkbox(choices, extension === 'all' ? 'All formats' : extension,
                    current.includes(extension), enabled => {
                        selected[kind] = extension === 'all' && enabled ? ['all'] : enabled
                            ? [...current.filter(item => item !== 'all'), extension] : current.filter(item => item !== extension);
                        changed('file_formats'); draw();
                    });
                addInput(group, 'Add file extension', text => {
                    const extensions = text.toLowerCase().split(/[,;\s]+/).map(value => value.replace(/^\./, ''));
                    if (extensions.some(value => !/^[a-z0-9_-]+$/.test(value))) { notice('Invalid file extension', true); return; }
                    selected[kind] = extensions.includes('all') ? ['all'] : [...new Set([...current.filter(value => value !== 'all'), ...extensions])];
                    changed('file_formats'); draw();
                });
                host.append(group);
            }
        };
        draw(); editors.set('file_formats', () => structuredClone(selected));
    }
    function proxyEditor(summary) {
        const host = byId('setting-proxy'), fields = {};
        host.replaceChildren();
        const details = node('div', undefined, 'field-grid proxy-fields');
        const enabled = checkbox(host, 'Enable Telegram proxy', !!summary, value => { details.hidden = !value; changed('proxy'); });
        details.hidden = !summary;
        for (const [key, label, type] of [['scheme', 'Protocol', 'select'], ['hostname', 'Proxy host', 'text'],
            ['port', 'Proxy port', 'number'], ['username', 'Proxy username', 'text'], ['password', 'Proxy password', 'password']]) {
            const wrapper = node('div'), input = node(type === 'select' ? 'select' : 'input');
            input.id = `proxy-${key}`;
            const caption = node('label', label); caption.htmlFor = input.id;
            if (type === 'select') for (const value of ['socks5', 'socks4', 'http']) {
                const option = node('option', value); option.value = value; input.append(option);
            } else input.type = type;
            if (type === 'password') input.autocomplete = 'new-password';
            if (type === 'number') { input.min = 1; input.max = 65535; }
            input.value = summary?.[key] ?? ({scheme: 'socks5', port: 1080}[key] || '');
            input.addEventListener('input', () => changed('proxy'));
            fields[key] = input; wrapper.append(caption, input); details.append(wrapper);
        }
        host.append(details, node('p', 'Leave proxy credentials blank to keep the saved credentials. Turn off the switch to remove the proxy.', 'help'));
        editors.set('proxy', () => {
            if (!enabled.checked) return {};
            const result = {scheme: fields.scheme.value, hostname: fields.hostname.value.trim(), port: Number(fields.port.value)};
            for (const key of ['username', 'password']) if (fields[key].value) result[key] = fields[key].value;
            return result;
        });
    }
    function uploadEditor(values) {
        const host = byId('setting-upload_drive'), current = structuredClone(values);
        host.replaceChildren();
        for (const [key, label] of [['enable_upload_file', 'Enable cloud upload'], ['before_upload_file_zip', 'Zip before upload'],
            ['after_upload_file_delete', 'Delete local files after cloud upload']]) {
            checkbox(host, label, !!current[key], value => { current[key] = value; changed('upload_drive'); });
        }
        for (const [key, label] of [['upload_adapter', 'Upload adapter'], ['rclone_path', 'Rclone path'], ['remote_dir', 'Remote directory']]) {
            const input = node(key === 'upload_adapter' ? 'select' : 'input'); input.id = `upload-${key}`;
            const caption = node('label', label); caption.htmlFor = input.id;
            if (key === 'upload_adapter') for (const value of ['rclone', 'aligo']) {
                const option = node('option', value); option.value = value; input.append(option);
            }
            input.value = current[key] || (key === 'upload_adapter' ? 'rclone' : '');
            input.addEventListener('input', () => { current[key] = input.value; changed('upload_drive'); });
            host.append(caption, input);
        }
        editors.set('upload_drive', () => structuredClone(current));
    }
    const states = {unconfigured: 'Not configured', logged_out: 'Not signed in', ready: 'Connected',
        starting: 'Telegram authorized; starting download service', code_required: 'Verification code required', password_required: 'Two-step verification required', error: 'Connection failed'};
    function renderTelegram(state) {
        telegram = state;
        const pending = reconnect.length > 0 || [...dirty].some(key => basic.has(key));
        const accountChange = reauthorize || byId('setting-use_user_account').checked !== savedUserMode;
        const ready = state.state === 'ready', code = state.state === 'code_required', password = state.state === 'password_required';
        byId('telegram-state').textContent = t(state.authorized ? 'Signed in' : state.busy ? (state.stage || 'Connecting...') : (states[state.state] || 'Not signed in'));
        byId('telegram-state').classList.toggle('verified', !!state.authorized);
        byId('account-name').textContent = state.account ? `${state.account.name} · ${state.account.id}` : '';
        const message = t(state.message || '') + (state.retry_after ? ` (${state.retry_after}s)` : '');
        byId('telegram-message').textContent = state.authorized ? '' : message;
        byId('phone-row').hidden = !byId('setting-use_user_account').checked || ((state.authorized || ready) && !accountChange) || code || password;
        byId('code-row').hidden = !code; byId('password-row').hidden = !password;
        byId('telegram-submit').textContent = t(code || password ? 'Sign in' : state.authorized && !accountChange ? 'Verify and reconnect' : 'Verify and sign in');
        byId('telegram-submit').hidden = !!state.authorized && !pending;
        byId('telegram-submit').disabled = !loaded || state.busy || state.retry_after > 0;
        byId('telegram-cancel').hidden = !code && !password && !state.busy;
        byId('telegram-logout').hidden = !(ready || state.can_logout);
        byId('telegram-logout').textContent = t('Sign out of Telegram');
        byId('telegram-logout').disabled = state.busy;
        byId('telegram-test-row').hidden = !state.authorized || accountChange;
        byId('telegram-test').disabled = !loaded || state.busy || state.retry_after > 0;
        const connection = byId('telegram-connection');
        connection.textContent = pending ? t('Verify login to apply changes') : state.busy ? t(state.stage || 'Testing Telegram connection')
            : message || t(state.connection_state === 'connected' ? 'Connection successful'
                : state.connection_state === 'failed' ? 'Connection failed' : 'Connection not tested');
        connection.classList.toggle('success', state.connection_state === 'connected' && !pending && !state.busy);
        connection.classList.toggle('error', state.connection_state === 'failed' && !state.busy);
        if (state.connection_state === 'connected' && !pending && !state.busy) {
            for (const key of ['api_hash', 'bot_token']) byId(`setting-${key}`).value = '';
        }
        for (const key of basic) {
            const input = byId(`setting-${key}`);
            if (input && input.tagName === 'INPUT') input.disabled = state.busy || code || password;
        }
    }
    function renderStatus(data) {
        restart = data.restart_required;
        reconnect = data.reconnect_required || [];
        reauthorize = !!data.reauthorize_required;
        byId('restart-notice').hidden = !restart.length;
        byId('restart-fields').textContent = restart.map(key => t(config.schema.find(field => field.key === key)?.label || key)).join(', ');
        renderTelegram(data.telegram);
    }
    function renderSettings(data, preserveCredentials = false) {
        editors.clear();
        savedUserMode = data.settings.use_user_account;
        for (const field of config.schema) {
            const input = byId(`setting-${field.key}`), value = data.settings[field.key];
            if (!input) continue;
            if (field.key === 'proxy') proxyEditor(data.proxy_summary);
            else if (field.key === 'media_types') choiceEditor(field.key, value, media);
            else if (naming[field.key]) choiceEditor(field.key, value, naming[field.key], true);
            else if (field.key === 'file_formats') formatEditor(value);
            else if (field.key === 'download_filters') filterEditor(value);
            else if (field.key === 'upload_drive') uploadEditor(value);
            else if (field.type === 'list') listEditor(field.key, value);
            else if (field.type === 'secret') {
                if (!(preserveCredentials && ['api_hash', 'bot_token'].includes(field.key))) input.value = '';
                input.placeholder = data.configured_secrets.includes(field.key) ? '••••••••' : '';
                document.querySelector(`[data-secret="${field.key}"]`).textContent = t(
                    data.configured_secrets.includes(field.key) ? 'Saved. Leave blank to keep the current value.' : 'Not configured');
            } else if (field.type === 'bool') input.checked = value;
            else input.value = value;
        }
        dirty.clear(); loaded = true; renderStatus(data);
    }
    document.querySelectorAll('[data-setting]').forEach(input => input.addEventListener('input', () => changed(input.dataset.setting)));
    async function saveSettings(preserveCredentials = false) {
        const draft = byId('filter-expression');
        if (draft?.value.trim()) {
            draft.focus(); throw new Error('Add or clear the draft filter before saving settings');
        }
        if (!byId('settings-form').reportValidity()) throw new Error('Check the highlighted fields');
        const settings = {};
        for (const key of dirty) {
            const field = config.schema.find(field => field.key === key), input = byId(`setting-${key}`);
            settings[key] = editors.has(key) ? editors.get(key)() : field.type === 'bool' ? input.checked
                : field.type === 'int' ? Number(input.value) : input.value;
        }
        const data = await request('/api/configuration', {settings});
        renderSettings(data, preserveCredentials);
        return data;
    }
    byId('settings-form').addEventListener('submit', event => {
        event.preventDefault();
        withButton(event.submitter, async () => { await saveSettings(); notice('Settings saved'); });
    });
    async function action(name, values = {}) {
        telegram = await request('/api/telegram', {action: name, ...values});
        renderTelegram(telegram);
        byId('phone-code').value = byId('two-step-password').value = '';
        schedule(300);
    }
    byId('telegram-submit').addEventListener('click', event => withButton(event.currentTarget, async () => {
        notice('');
        if (telegram.state === 'code_required') return action('sign_in', {code: byId('phone-code').value});
        if (telegram.state === 'password_required') return action('password', {password: byId('two-step-password').value});
        await saveSettings(true);
        return action('login', {phone: byId('phone').value.trim()});
    }));
    for (const id of ['phone', 'phone-code', 'two-step-password']) byId(id).addEventListener('keydown', event => {
        if (event.key === 'Enter') { event.preventDefault(); byId('telegram-submit').click(); }
    });
    byId('telegram-cancel').addEventListener('click', event => withButton(event.currentTarget, () => action('cancel')));
    byId('telegram-test').addEventListener('click', event => withButton(event.currentTarget, async () => {
        notice('');
        await saveSettings(true);
        return action('test');
    }));
    byId('telegram-logout').addEventListener('click', event => {
        if (confirm(t('Sign out of Telegram? Downloads will stop and keep their checkpoints.'))) withButton(event.currentTarget, () => action('logout'));
    });
    function schedule(delay = 2000) { clearTimeout(timer); if (!document.hidden) timer = setTimeout(poll, delay); }
    async function poll() {
        if (polling || document.hidden) return;
        polling = true;
        try { renderStatus(await request('/api/configuration')); }
        catch (error) { notice(error.message, true); }
        finally { polling = false; schedule(); }
    }
    document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(timer); else schedule(0); });
    document.addEventListener('change', event => {
        if (event.target.matches('[data-language-switch]') && loaded && !dirty.size && !byId('filter-expression')?.value.trim()) request('/api/configuration').then(renderSettings).catch(error => notice(error.message, true));
    });
    window.addEventListener('beforeunload', event => { if (dirty.size || byId('filter-expression')?.value.trim()) { event.preventDefault(); event.returnValue = ''; } });
    byId('settings-form').querySelector('button[type="submit"]').disabled = true;
    request('/api/configuration').then(data => {
        renderSettings(data); byId('settings-form').querySelector('button[type="submit"]').disabled = false;
    }).catch(error => notice(error.message, true)).finally(() => schedule());
})();
