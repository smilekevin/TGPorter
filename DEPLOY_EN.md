# TG Porter - Deployment Guide

[Chinese deployment guide](https://github.com/smilekevin/TGPorter/blob/main/DEPLOY.md)

Installation, configuration, login and operations are maintained here. See the [README](https://github.com/smilekevin/TGPorter/blob/main/README.md#features) for features, Bot commands, Web APIs and architecture.

English: [Prerequisites](#prerequisites) · [Credentials](#before-you-deploy-api-credentials-and-bot-token) · [Docker Image](#docker-image-deployment) · [Compose](#docker-compose-deployment) · [Manual Install](#manual-install) · [Local Dev](#local-dev-mode) · [Configuration](#configuration) · [WebUI Login](#webui-login) · [MCP](#mcp-agent-integration) · [Operations](#docker-management) · [Persistence](#docker-data-persistence) · [Proxy](#proxy-configuration) · [Troubleshooting](#troubleshooting)

---

## Prerequisites

The Telegram SDK is the Kurigram version pinned in `requirements.txt`, imported as `pyrogram`. Do not co-install Pyrogram, Pyrofork and Kurigram in one virtual environment. For an existing manual installation, recreate the virtual environment before installing the full requirements.

The WebUI uses native HTML/CSS/JS and requires no Node.js installation or npm build. CI workflow script tests use Node.js 24 separately. Workflows use `actions/checkout@v5`, `actions/setup-node@v5` and `actions/setup-python@v6`, alongside `docker/build-push-action@v7`, `docker/login-action@v4`, `docker/setup-buildx-action@v4` and `docker/setup-qemu-action@v4`; these actions all run natively on Node.js 24.

Install main-service dependencies from `requirements.txt`, which explicitly pins the tested Starlette, Uvicorn and Pydantic versions. Filter expressions use the pinned Lark parser; PLY and generated `parsetab.py` files are no longer needed. Flask-Login, Requests and PyCryptodome are no longer required. Recreate an older virtual environment when upgrading so leftover packages cannot hide missing dependencies.

Before starting this version for the first time, stop the service and back up `data/sessions/`, `config.yaml`, `data/data.json`, `downloads/` and `downloads/.temp/`. Kurigram upgrades existing session databases in place. To roll back to the previous SDK, stop the service and restore the session backup from before migration. Task and `.parts/` checkpoint formats remain unchanged; offline migration tests do not replace real Telegram download acceptance checks.

- Image deployment requires Docker; Compose deployment also requires Docker Compose. Manual installation uses Python 3.14 and pip.
- Actual downloads require Telegram API credentials and access to the target messages. Mock development mode does not require Telegram login.
- The display name is **TG Porter**, the repository name is `TGPorter`, and the GHCR image, Compose service and container use lowercase `tgporter`.
- When upgrading, stop the old container and reuse its existing data mounts before starting the renamed service, so two instances cannot access the same sessions and task data. Rename the GitHub repository separately; the new GHCR image must be published by CI or a Release workflow before it can be pulled.
- The prebuilt image is `ghcr.io/smilekevin/tgporter`, supporting amd64 and arm64. Published GitHub Releases create version, SHA and `latest` tags. CI verification images use SHA tags and do not update `latest`. The README Version badge follows the latest stable GitHub Release tag; its Python badge reads `python-version` from `.github/workflows/tests.yml`. Badge updates appear after caches refresh.
- For PR or main verification, use the SHA tag or digest from **CI image for verification** in the [CI Summary](https://github.com/smilekevin/TGPorter/actions/workflows/tests.yml). The [Release workflow](https://github.com/smilekevin/TGPorter/actions/workflows/docker-build.yml) adds pull commands, digests and platforms to Release notes after a successful image push, preserving the existing description and updating the same section on reruns. Package visibility must be Public for unauthenticated pulls.

The commands below use `docker-compose`; replace it with `docker compose` if you use the Compose plugin.

## Quick Start

Docker deployment uses this fork's `ghcr.io/smilekevin/tgporter:latest` image, updated when GitHub Releases are published.

### Before You Deploy: API Credentials and Bot Token

These steps apply to image, Docker Compose and manual deployments. Local mock development mode does not require Telegram login.

**Get `api_id` and `api_hash`**

1. Sign in to [my.telegram.org](https://my.telegram.org) with the phone number of an existing Telegram account.
2. Open [API development tools](https://my.telegram.org/apps), fill in the application details and create an application.
3. Copy the displayed `api_id` and `api_hash` into your local `config.yaml`. Both phone and Bot Token login require these fields. Keep them private and out of public repositories. See [Telegram's official instructions](https://core.telegram.org/api/obtaining_api_id).

**Get a Bot Token**

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`.
2. Follow the prompts to set the bot's name and username, then copy the complete Token returned by BotFather.
3. Enter the Token in `config.yaml` as `bot_token` for the command and notification bot. See [Telegram's official instructions](https://core.telegram.org/bots/features#botfather).

The main download client supports phone or Bot Token login. Setting `bot_token` in the configuration does not replace the main client's first login. When downloading as a bot, it must have access to the target messages. First login and login after session loss are described under each deployment method below. See [Kurigram's authentication documentation](https://docs.kurigram.icu/start/auth) for background on both login methods.

### Docker Image Deployment

After starting the Bot, send `/help` or `/start` and use “Join us” to open the [Telegram group](https://t.me/tg_porter).

With Docker installed on your server or NAS, run this fork's prebuilt image directly.

1. **Prepare configuration and persistent data**: download [config.yaml.example](./config.yaml.example) from this repository, copy it to `config.yaml`, and create the `downloads/` and `data/` directories. Do not pre-create a runtime data file; the application generates `data/data.json` on first startup.

```bash
mkdir -p tgporter
cd tgporter
mkdir -p downloads data

curl -fL https://raw.githubusercontent.com/smilekevin/TGPorter/main/config.yaml.example \
  -o config.yaml.example
[ -f config.yaml ] || cp config.yaml.example config.yaml
```

2. **Edit the configuration**: open `config.yaml` in a text editor and enter `api_id`, `api_hash` and `bot_token`, and change `web_login_secret` to your own password. See [Configuration](#configuration) for media types, permissions and concurrency. The template includes an example proxy; adjust `proxy` for your environment or set `proxy: {}` if you do not need one. With the mounts and port mapping below, keep `save_path: /app/downloads`, `web_host: 0.0.0.0` and `web_port: 5000`.

3. **Pull the image and start it for the first time**: mount the configuration file, `data/` and downloads, then use interactive mode for Telegram login.

```bash
docker pull ghcr.io/smilekevin/tgporter:latest

docker run -it \
  --name tgporter \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v "$PWD/downloads:/app/downloads" \
  ghcr.io/smilekevin/tgporter:latest
```

4. **First login (or when login is needed again, such as after session expiry)**: **The account signed in here performs the actual downloads**. The program accesses and downloads messages as that Telegram user or bot, using that account's permissions. When `Enter phone number or bot token:` appears in the terminal, choose either option and confirm with `y` when prompted:

- **Phone**: enter your phone number with its country code (for example, `+8613812345678`), then the verification code sent by Telegram. Enter your two-step verification password if enabled.
- **Bot Token**: enter the complete Token obtained in [Before You Deploy](#before-you-deploy-api-credentials-and-bot-token). No phone verification code is needed.

After login succeeds, press `Ctrl+C` to stop the current foreground run. The session persists in the mounted `data/sessions/` directory; a valid saved session avoids repeat login on restart. Tasks and download history persist in `data/log/`. See [Docker Data Persistence](#docker-data-persistence) for mount details.

5. **Start in the background and access WebUI**: run the following commands to stop the container and use [`docker start`](https://docs.docker.com/reference/cli/docker/container/start/) to start the existing container in the background:

```bash
docker stop tgporter
docker start tgporter
```

Once started, open `http://SERVER_IP:5000` and enter your configured password. See [WebUI Login](#webui-login).

### Docker Compose Deployment

Deploy with the repository's `docker-compose.yaml`:

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter

# Preserve existing configuration and runtime data.
[ -f config.yaml ] || cp config.yaml.example config.yaml
mkdir -p downloads data
# Edit config.yaml with your credentials and change the WebUI password.
```

Prepare only `config.yaml` as a file before starting Docker. Mount `data/` as a writable directory; JSON, logs and sessions are created automatically. Staging defaults to `save_path/.temp/` (`/app/downloads/.temp` in this example), without a separate mount. The repository Compose file enables example proxy environment variables; adjust or remove them for your network. A complete example without environment variable proxies is:

```yaml
services:
  tgporter:
    image: ghcr.io/smilekevin/tgporter:latest
    container_name: tgporter
    network_mode: bridge
    stdin_open: true
    tty: true
    ports:
      - "5000:5000"
    environment:
      - TZ=Asia/Shanghai
      # - http_proxy=socks5://172.17.0.1:1080
      # - https_proxy=socks5://172.17.0.1:1080
    volumes:
      # Downloads (change the host path if needed)
      - "./downloads:/app/downloads/"
      # Configuration files
      - "./config.yaml:/app/config.yaml:ro"
      - "./data:/app/data"
    restart: unless-stopped
```

You can change the host download directory to `/your/download/path:/app/downloads/`; keep `save_path: /app/downloads` inside the container. When changing `web_port`, update the Compose port mapping too. Prebuilt images need only configuration and data mounts; avoid mounting local source code over the image.

**First login (or when login is needed again, such as after session expiry)**: **The account signed in here performs the actual downloads**. The program accesses and downloads messages as that Telegram user or bot, using that account's permissions. Start a one-off container in an interactive terminal, stopping the background service first if it is running:

```bash
# Pull a published Release image.
docker-compose pull
docker-compose stop tgporter
docker-compose run --rm tgporter
```

At `Enter phone number or bot token:`, enter your phone number with its country code or a complete Bot Token, and confirm with `y`. Phone login also requires a verification code and your two-step verification password if enabled; Bot Token login does not require a phone verification code. After login succeeds, press `Ctrl+C` to stop this foreground run, then start the background service:

```bash
docker-compose up -d

# View logs
docker-compose logs -f
```

Compose saves the session in the host's `data/sessions/` directory. `--rm` removes only the one-off container, leaving mounted directories intact. A valid saved session avoids repeat login on later starts.

### Manual Install

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

[ -f config.yaml ] || cp config.yaml.example config.yaml
# Enter credentials and a WebUI password; set save_path to a local path such as ./downloads.
# Adjust proxy for your network, or use proxy: {} if it is not needed.
```

**First login (or when login is needed again, such as after session expiry)**: **The account signed in here performs the actual downloads**. The program accesses and downloads messages as that Telegram user or bot, using that account's permissions. Stop any running downloader process, then run the following command from the project directory in an interactive terminal:

```bash
python media_downloader.py
```

At `Enter phone number or bot token:`, enter your phone number with its country code or a complete Bot Token, and confirm with `y`. Phone login also requires a verification code and your two-step verification password if enabled; Bot Token login does not require a phone verification code. The program continues running after login. The session is saved in the project's `data/sessions/` directory. A valid saved session avoids repeat login on later starts.

### Local Dev Mode

From the repository root, use a Python 3.14 virtual environment. Only Flask and Werkzeug are required; Telegram credentials and the full downloader dependencies are unnecessary.

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install Flask==3.1.3 Werkzeug==3.1.9
# No Telegram account needed, mock data for WebUI debugging
python run_local.py
# Open http://localhost:5000
```

Select **English** from the language menu at the top right to preview the [English dashboard shown in the README](https://github.com/smilekevin/TGPorter/blob/main/README.md#webui-preview). The README provides a 2560 × 1440 screenshot captured in local Mock mode, with demo download data and a simulated Connected Bot status; this mode does not perform real downloads.

---

## Configuration

Download filters retain comparisons, arithmetic, `and` / `or`, `&&` / `||`, parentheses, single-quoted strings, `r'...'` regexes and `B` / `KB` / `MB` / `GB` / `TB` units. Dates require a full time (for example, `2024-01-01 00:00:00`). Regex comparisons use full matching and accept the regex on either side. Illegal characters now produce syntax errors instead of being skipped. Missing-field behavior and operator precedence retain the existing rules.

### config.yaml

Adjust the example for your environment and change `web_login_secret` before deployment. The example uses four workers per file; the application default is one when the option is omitted. Use `proxy: {}` if you do not need the template proxy.

```yaml
# Telegram API credentials (required)
api_id: your_api_id
api_hash: your_api_hash
bot_token: your_bot_token

# Media types
media_types:
  - audio
  - document
  - photo
  - video
  - voice
  - animation

# File format filter (all = all formats)
file_formats:
  audio:
    - all
  document:
    - pdf
    - epub
  video:
    - mp4

# Download settings
save_path: /app/downloads
file_path_prefix:
  - chat_title
  - media_datetime
file_name_prefix:
  - file_name
hide_file_name: false

# Concurrency
max_download_task: 5  # number of workers
file_download_concurrency: 4  # concurrent download workers per file
download_part_size_mb: 64  # fixed part size in MiB; the final part uses the remaining size

# WebUI
web_host: 0.0.0.0
web_port: 5000
web_login_secret: "replace-with-a-long-password"  # WebUI password; "" disables login
mcp_token: ""  # MCP-only bearer token, independent of the browser password
mcp_enabled: false
mcp_read_only: false
mcp_allowed_hosts: []

# Permissions
allowed_user_ids:
  - 'me'  # 'me' = currently logged-in account

# Other
language: zh-cn
date_format: '%Y_%m'
enable_download_txt: false
log_level: DEBUG

# Proxy (Docker: access host proxy)
proxy:
  scheme: http
  hostname: 172.17.0.1
  port: 20172
```

### Parameters

Defaults apply when an option is omitted and may differ from the example configuration.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `api_id` | Required Telegram API ID ([my.telegram.org](https://my.telegram.org/apps)) | — |
| `api_hash` | Required Telegram API Hash | — |
| `bot_token` | Command and notification bot Token ([@BotFather](https://t.me/BotFather)); may be empty in MCP-only mode | `""` |
| `media_types` | Required media type list; examples include `audio`, `document`, `photo`, `video`, `voice`, `animation` and `video_note` | — |
| `file_formats` | Required non-empty format lists for `audio`, `document` and `video`; `all` as the first entry accepts all formats of that type; the template allows PDF/EPUB documents and MP4 videos | — |
| `save_path` | File save path | `./downloads` |
| `file_path_prefix` | Ordered subdirectory fields: `chat_title`, `media_datetime`, `media_type`; omitted, `null` or empty lists retain the default | `[chat_title, media_datetime]` |
| `file_name_prefix` | Ordered filename fields: `message_id`, `file_name`, `caption`; absent values are skipped, with message ID as fallback; omitted, `null` or empty lists retain the default | `[message_id, file_name]` |
| `file_name_prefix_split` | Separator between filename fields | `" - "` |
| `chat` | Optional startup history scan list; nested fields are below; omit for MCP-only single-post submissions | No scan |
| `upload_drive` | Optional cloud-upload configuration; nested fields are below | Disabled |
| `max_download_task` | Concurrent download workers | `5` |
| `file_download_concurrency` | Concurrent download workers per file | `1` |
| `download_part_size_mb` | Fixed part size in MiB; positive integer | `64` |
| `web_host` | Shared WebUI/API/MCP bind address; normally keep `0.0.0.0` inside Docker | `0.0.0.0` |
| `web_port` | Shared WebUI/API/MCP listener port | `5000` |
| `web_login_secret` | WebUI/API session-login password; empty preserves public access; independent of MCP authentication | `""` |
| `mcp_token` | MCP-only bearer token; printable ASCII without whitespace; required when MCP is enabled | `""` |
| `mcp_enabled` | Enable the same-port `/mcp` Streamable HTTP endpoint, always requiring a token | `false` |
| `mcp_read_only` | Expose only MCP query tools without restricting separate browser sessions | `false` |
| `mcp_allowed_hosts` | Additional allowed Host values: domain/IP with optional port; localhost/loopback already allowed | `[]` |
| `allowed_user_ids` | Allowed Bot users (`'me'` = self) | `[]` |
| `language` | Default Bot language and translated logs (`en` / `zh-cn` / `zh-tw` / `ja` / `es` / `fr` / `ru` / `uk`; legacy `ZH` / `UA` remain valid), independent of WebUI | `EN` |
| `proxy` | Proxy config (scheme / hostname / port) | `{}` |
| `log_level` | Log level | `INFO` |
| `hide_file_name` | Hide filenames in UI | `false` |
| `enable_download_txt` | Save text-only messages as .txt | `false` |
| `date_format` | Date directory format | `%Y_%m` |

#### Nested Configuration and Optional Features

`chat` is a list of objects; `upload_drive` and `proxy` are mappings. The following fields belong under those parents rather than at the top level. Configure them only when using the corresponding feature:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `chat[].chat_id` | Chat/channel ID or resolvable name to scan; required for each scan entry | — |
| `chat[].last_read_message_id` | Starting read-message ID; a later persisted checkpoint takes precedence | `0` |
| `chat[].download_filter` | Download filter expression for this chat; empty adds no filter | `""` |
| `chat[].upload_telegram_chat_id` | Telegram destination for re-uploaded media; requires permission to send | `null` |
| `upload_drive.enable_upload_file` | Enable cloud uploads after downloading | `false` |
| `upload_drive.rclone_path` | rclone executable; bundled at the default path in the main image, installed separately for manual setups; configure its remote before use | `./rclone/rclone`, with `.exe` on Windows |
| `upload_drive.remote_dir` | Cloud destination, for example `drive:/telegram` | `""` |
| `upload_drive.before_upload_file_zip` | Compress each file into a ZIP before upload | `false` |
| `upload_drive.after_upload_file_delete` | Delete the original local file after successful cloud upload; the current loader only applies truthy values, so `false` cannot override its default `true` | `true` |
| `upload_drive.upload_adapter` | Upload adapter; normally `rclone`; legacy `aligo` needs extra installation and login and is absent from the main requirements | `rclone` |
| `proxy.scheme` | Telegram proxy scheme, such as `http` or `socks5`; separate from Compose environment proxies | — |
| `proxy.hostname` | Telegram proxy host without the scheme | — |
| `proxy.port` | Integer Telegram proxy port | — |
| `proxy.username` / `proxy.password` | Proxy credentials, only when authentication is required | No authentication |

#### Advanced and Compatibility Settings

These settings can usually be omitted. Advertisement settings are initial defaults; saved rules in `data/data.json` take precedence and Bot changes save immediately. `drop_no_audio_video` is currently parsed without an execution path, so do not rely on it to filter media.

| Parameter | Description | Default |
|-----------|-------------|---------|
| `start_timeout` | Main Telegram session startup timeout in seconds; does not limit normal media transfers | `60` |
| `max_concurrent_transmissions` | SDK transport-capacity override, normally omitted; saved concurrency and WebUI/API/MCP worker changes recalculate it | `max_download_task * max(5, file_download_concurrency)` |
| `debug_web` | Flask debug state and ASGI debug logging | `false` |
| `forward_limit` | Local forwarding-call budget per 60 seconds; does not bypass Telegram FLOOD_WAIT | `33` |
| `after_upload_telegram_delete` | Remove the local file when Telegram re-upload handling ends; cleanup runs in `finally`, including after upload exceptions | `true` |
| `filter_advertisement_list` | Keywords whose matching content is skipped during forwarding | `[]` |
| `replace_advertisement_list` | Text removed from forwarded captions | `[]` |
| `group_add_advertisement` | Additional caption text mapped by destination chat ID | `{}` |
| `drop_no_audio_video` | Compatibility field that is parsed but does not currently filter media | `false` |
| `restart_program` | Internal run-control flag; `true` exits the main wait loop; keep disabled for normal deployment | `false` |
| `download_min_part_size_mb` | Legacy part-size key, read only when `download_part_size_mb` is absent | `64` |
| `chat_id` | Legacy single-chat scan entry; use the `chat` list for new configurations | Unset |
| `last_read_message_id` | Legacy top-level starting checkpoint used with `chat_id`; required for that format | — |
| `download_filter` | Legacy filter mapping by chat ID; new configurations use `chat[].download_filter` | Unset |
| `ids_to_retry` | Legacy single-chat retry-message list; new tasks recover through the shared queue and runtime data | `[]` |

`api_id`, `api_hash`, `media_types` and `file_formats` must be present; enabled MCP additionally requires a non-empty `mcp_token`. Use YAML `true` / `false` for boolean settings rather than quoted strings. Template values differ from defaults: `file_download_concurrency` is `4`, `file_name_prefix` contains only `file_name`, `date_format` is empty, and proxy addresses are environment examples. Restart after editing `config.yaml`. Manage live tasks, language, advertisement rules and concurrency through application interfaces rather than editing runtime JSON.

The WebUI (including sign-in) and Bot share `lang/*.json` catalogs: Simplified Chinese (`zh-cn`), Traditional Chinese (`zh-tw`), English (`en`), Japanese (`ja`), Spanish (`es`), French (`fr`), Russian (`ru`) and Ukrainian (`uk`). Each language has one file and shared vocabulary is translated once. English `messages` lists canonical keys; other languages map those keys to translations. The browser matches language preferences in order; `zh` falls back to `zh-cn`, while `zh-TW` / `zh-HK` / `zh-Hant` select Traditional Chinese. Unsupported languages fall back to English. Manual choices are saved in the current browser and take priority; no IP geolocation is used. Configure the Bot independently, for example `language: zh-tw` or `/set_language ja`. Legacy `ZH` / `zh` still mean Simplified Chinese and `UA` / `ua` still mean Ukrainian. Bot language is saved with runtime state in `data/data.json` using canonical codes, leaving `config.yaml` unchanged. Runtime code, configuration example comments and the PWA app name use English; tests may retain required language fixtures. Failure records store English keys and translate when displayed by the Bot or browser; unrecognized historical records retain their original text. Missing messages fall back to English, then the original text; existing Russian and Ukrainian translations are preserved, with English fallback for untranslated additions.

To add a language, copy a translated catalog such as `lang/fr.json` to a new language file, edit `code`, `name`, `aliases` and `messages`, then restart. Both Bot and browser discover it automatically; no extra dependency or frontend build is required. See the [catalog guide](https://github.com/smilekevin/TGPorter/blob/main/docs/languages.md) for steps and placeholder rules.

Files are split into fixed-size parts using `download_part_size_mb`; the final part contains the remaining bytes.
At 64 MiB per part, a 40 MiB file has one part, a 100 MiB file has two and a 500 MiB file has eight.
Each file starts with one connection. A new connection must receive and write its first valid block before the next connection may start, with starts at least one second apart, up to `file_download_concurrency` workers. A blocked handshake or first response prevents expansion. A failed start closes its connection and requeues its unfinished part before another attempt, using the existing failure budget. Healthy connections continue downloading; confirming startup does not require finishing a whole part. Finished workers take the next queued part until
all parts are complete, then the parts are merged in order. Each worker owns an independent Telegram download
TCP connection and uses it for successive parts; different workers and files do not share download connections.
Pauses retain connections; completion, cancellation and client restart close them. File workers start on demand when tasks enter the queue and exit when it empties; an empty startup creates none. Raising the limit adds workers only for queued tasks; lowering it lets active files finish before reducing concurrency. `max_download_task` limits
simultaneous files: five files with four workers each can run up to twenty concurrent workers, without an additional global concurrency setting.
Concurrency changes through WebUI, Web API or MCP apply immediately and save to `max_download_task` in `data/data.json` for subsequent restarts; `config.yaml` stays read-only. Without a saved runtime value, the configured initial value applies (`5` when omitted). To restore the configured default, stop the service and remove the JSON `max_download_task` key.
Cancellation closes connections before applying the file deletion choice to staging files and `.parts/` in `downloads/.temp/`. For active and failed tasks, the deletion dialog indicates whether staging files or part directories exist without listing cache paths; it reports no files only when neither output nor cache at recorded paths exists. WebUI checks the file and cache option by default for active/failed tasks and leaves it unchecked for completed tasks, including batch actions. Deselect it to retain files and checkpoints. Web API/MCP `delete_file` defaults to `false`; Bot cancellation still cleans staging by default. Older failed records without a recorded staging path can only clean files whose paths are known.
A failed download connection closes and returns its unfinished part to the queue while healthy connections keep running. A single worker failure does not end the file while another worker remains; only three consecutive failures of the last worker fail the download, without another file-level retry budget. Completing a part resets the counter. The last worker retries with up to 50% random jitter capped at 30 seconds: the two waits are 10–15 and 20–30 seconds. Local disk writes and checkpoint validation errors are not retried as connection errors. Explicit expired/invalid references refetch the original message and validate file identity and size before resuming; other BadRequest errors retain their actual exception type. FLOOD_WAIT / FLOOD_PREMIUM_WAIT does not consume attempts or impose cumulative limits or a 600-second manual-resume threshold. Throttled connections close with checkpoints retained; cooldown blocks new connections while healthy existing connections continue taking parts. If all connections are throttled, they wait for the longest shared cooldown, then automatically resume from checkpoints with gradual connection creation. The UI displays the shared FLOOD_WAIT countdown. Manual pause, resume and cancellation remain available; cooldown expiry does not clear a manual pause.
The initial part size and worker count are saved. Pauses, retries and process restarts reuse this plan and skip completed parts.
Progress sums downloaded bytes across all parts; resumed bytes do not count toward new transfer speed.
The WebUI header shows the total active stream count; file cards, Bot progress reports and `/query` show each file's count. This counts streams executing download requests (including requests awaiting data), rather than the configured worker limit. Workers waiting to start, idle workers, retry backoff, cooldown waits and local merging are excluded. Paused files show zero, as does the total during a global pause. Counts resume with downloads and are not retained after completion, failure, cancellation or restart.
The Bot `/query` command lists download records and pending tasks, six rows per page with category totals. Use the category, paging and refresh buttons or enter `/query active`, `/query pending`, `/query completed 2` or `/query failed`. Active lists include paused tasks. Commands and buttons use the existing `allowed_user_ids` allowlist and do not modify tasks.
The Bot edits the existing progress message about every 10 seconds when its content changes, without waiting for 20% milestones. FLOOD_WAIT notices show remaining seconds; downloads resume automatically after cooldown while manual pauses remain in effect. If the Bot messaging endpoint itself is rate limited, reports stop during its cooldown and resume with a notice afterward; undelivered completion reports remain available for retry. Each report waits at most 10 seconds, so notification failures do not stall downloads.
Legacy checkpoints remain resumable. If the new field is absent, `download_min_part_size_mb` is accepted as a
legacy part-size value without rewriting the configuration file. The earlier `download_concurrency` setting has been removed.

New downloads use `downloads/.temp/timestamp_guid.tmp` without channel titles or original filenames. The timestamp is the message's Unix time in seconds (`0` when no date is available); the GUID is derived from the owning client, chat ID, message ID and media identity. Retries and restarts reuse the same path without an extra index file. Part plans and data live in `timestamp_guid.tmp.parts/`, including `manifest.json`; SDK/merge checkpoints may use `timestamp_guid.tmp.temp`. Existing legacy checkpoints keep their paths until completion or explicit deletion. Completed files retain the configured naming rules.
Persist the entire `downloads/.temp` directory. Merging multiple parts may
temporarily require twice the file size in disk space; a single part is moved without a full extra copy.
See the [download test guide](https://github.com/smilekevin/TGPorter/blob/main/docs/parallel-download-testing.md) for offline and live acceptance checks.
For cross-DC regular media downloads, create the auth key through the target DC's regular endpoint once and reuse it in memory for subsequent workers/files of the same account, DC and test mode. A per-key lock prevents duplicate handshakes. Each worker still owns its media session and TCP connection and follows the existing authorization flow. The account's home DC still reuses its stored key; CDN connections retain their own endpoints. The cache is isolated by account, never written to disk or logs, and cleared when the client stops/restarts. Explicit key-rejection errors discard only the matching old cache entry so the next attempt can negotiate a new key. Auth-key creation has a 60-second limit; a timeout closes that connection and preserves checkpoints for the existing connection retry policy. This limit does not cap normal file transfers. DEBUG logs include media/auth endpoint addresses, ports and CDN flags, without key or authorization payloads.
The application writes only `data/log/tdl.log`, rotating at 10MB and retaining logs for 30 days. It no longer creates `download.log`; existing historical logs are left in place. In this log, `Waiting for first data` identifies the file, worker and DC before data arrives.
`Opened independent download connection #…` means the first block has arrived; `Confirmed independent download startup` confirms that a valid block was written and allows another connection to start. `Closed independent download connection #…` records resource cleanup. Cancellation before any data also logs resource release.
Set `log_level: DEBUG` to trace DC resolution, storage reads, auth-key creation, session startup, authorization export/import and the first media request. While waiting for first data, a WARNING reports the current stage and time spent in that stage every 30 seconds, without changing request timeouts or retries. SDK log levels are separate; application diagnostics omit authorization payloads.

---

## WebUI Login

The search field above the list filters the current tab by task ID or filename and retains the query across tabs. Clear search to restore the full list. Active/Failed filter their complete data; Pending/Completed search on the server before pagination, including unloaded pages. Select all applies only to matching tasks. Paused files remain in Downloading, with a status filter and shortcuts to select all, running or paused matches. The list shows matching and total counts. The standalone global pause button is removed; pause/resume acts on the selection. Pause is available for running selections and resume for paused selections; both are disabled for mixed states. Cancel task in Pending cancels an unstarted queued task; submit its link again to download it later. Completed offers batch deletion only, while Failed offers batch retry and deletion. Initial requests load in parallel. Visible pages refresh in cycles, scheduling the next cycle only after the previous one finishes. Polling pauses while hidden and refreshes immediately on return; requests already in flight may finish. Status/active tasks refresh each cycle, Pending/failures at least five seconds apart, and completed counts at least ten seconds apart. Read requests time out after 15 seconds; mutations retain their existing waiting behavior. Slow connections can lengthen refresh intervals. The system reduced-motion preference disables decorative animations.

Open `http://SERVER_IP:5000`. A non-empty `web_login_secret` protects the dashboard and download APIs. Enter that password on the login page; use the top-right action to log out. The login page offers an unchecked “Remember me (7 days)” option. Selecting it stores a persistent cookie so reopening the browser does not require another password; otherwise it uses a browser-session cookie (browser session restore may retain it). Both expire after at most seven days and require login again after a service restart or password change. State-changing requests using browser sessions also require the session's CSRF token; the WebUI supplies it automatically. The Web API accepts only browser login sessions, not bearer tokens. Agents use `/mcp` with a separate MCP token.

The dashboard accepts one HTTPS `t.me` post link (public, private or topic). It uses the existing server Telegram login and requires access to the chat. Submission persists before the existing consumer fetches media; unfinished duplicate posts reuse their task. With no `bot_token` and MCP disabled, the same backend still starts and the service stays running after configured downloads finish to accept WebUI tasks.

Set `web_login_secret: ""` to disable password login and restart the service after changing configuration. Existing deployments must use an image containing the login implementation; changing an older image's configuration alone does not enable it.

## MCP Agent Integration

MCP is integrated into the downloader, reusing the main image, container and `web_port` (default `5000`). Its endpoint is `/mcp`. Tools call internal download-management operations directly. [Tool reference](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#mcp-tool-reference). The 13 tools submit and manage downloads; `submit_download` accepts a single Telegram message link and persists it in the existing Pending queue.

### Enable MCP

MCP is disabled by default. Generate a random token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Set the quoted `mcp_token` value in `config.yaml` and enable MCP. Tokens must be printable ASCII strings without whitespace; an empty string or `null` means unconfigured:

```yaml
mcp_token: "paste-your-generated-token-here"
mcp_enabled: true
mcp_read_only: false
mcp_allowed_hosts: ["nas.example.com"]
```

Enabling MCP without a non-empty `mcp_token` fails startup. Every request requires a bearer token. When disabled, `/mcp` returns `404`.

Use `mcp_allowed_hosts` for additional allowed domains/IPs, such as `["nas.example.com", "192.168.1.20:5000"]`. Ports must match the request's Host header; omit scheme and path. Wildcards are not supported. Localhost/loopback remain allowed by default. Origin validation automatically reuses the same allowlist for HTTP/HTTPS origins. The listener still uses `web_host` and `web_port`; configure the full connection URL only in the client.

After updating the main image to a version containing this feature, edit the configuration and restart the existing container:

```bash
docker compose restart tgporter
```

Initial deployment, image updates and source builds use the main-service instructions above. No additional image, container, port, environment variables or dependency file is needed. Local installs use `pip install -r requirements.txt`. Existing persistence mounts remain unchanged.

### Agent Client

Connect through Streamable HTTP at the server’s `/mcp` URL. Clients supporting `url` and `headers` can use this example; actual fields depend on the client:

```json
{
  "mcpServers": {
    "TGPorter": {
      "url": "https://nas.example.com/mcp",
      "headers": {
        "Authorization": "Bearer paste-the-token-from-config.yaml"
      }
    }
  }
}
```

Use `http://127.0.0.1:5000/mcp` locally or the configured NAS URL on a LAN. For public access, use an HTTPS reverse proxy that preserves the original Host header and forwards GET, POST and DELETE requests to `/mcp`. Authentication uses a preconfigured bearer token, without an OAuth login page. Store tokens in protected configuration and request headers, never URLs or tool parameters. Missing or invalid tokens return `401`.

`mcp_read_only: true` exposes only the three query tools, without download submission. After rotating a token, restart the downloader and update agent configuration; the previous token is revoked. Write tools are enabled by default; deletion's `delete_file` defaults to `false`. See the [tool reference](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#mcp-tool-reference).

### Install the Optional Skill

The repository includes the [tgporter skill](https://github.com/smilekevin/TGPorter/blob/main/skills/tgporter/SKILL.md) for deployment guidance, MCP connection and download management, intended for agent clients that support `SKILL.md`. Managing tasks through MCP also requires Streamable HTTP and preconfigured bearer-token support. The skill is independent of a particular vendor's client, plugin format or installer.

Obtain the entire `skills/tgporter/` directory from a source checkout or release source archive, preserving the relative locations of `SKILL.md` and `references/`. Import the directory or place it in the skill location documented by your client. If the client supports importing from GitHub URLs, use the [skill directory link](https://github.com/smilekevin/TGPorter/tree/main/skills/tgporter). For a pinned version, replace `main` in the URL with a release tag or commit containing the skill; preserve local changes before updating an existing skill.

The recommended option is to download **`TGPorterSkills.zip`** from the [GitHub Release assets](https://github.com/smilekevin/TGPorter/releases) and give it directly to an agent that can read ZIP files. For clients requiring directory import, extract it and import `tgporter/`. The bundle contains only the skill, English deployment and configuration guide `DEPLOY_EN.md`, configuration/Compose examples, release metadata and license. Required setup guidance is readable locally, and the agent can still explain it in the user's language. The Chinese guide, developer instructions, test plans, design drafts, README and screenshots are excluded. Dependency installation, image pulls and Telegram login still need network access and the user's credentials.

After pushing the image, the Release workflow builds and uploads the ZIP, replacing the same asset on reruns. `release.json` defines the tag, image, digest, source commit and download URLs in one place, with each file's SHA-256. `references/release.md` renders exact image-pull commands, source-checkout commands and download links from the same release data. Compose selects the release image through its YAML field. The skill, deployment guide and configuration example are copied unchanged, without replacement based on headings, command text or URL formats. Before deployment, the agent reads the bundled release information for exact values and commands, then consults this general guide for procedures and parameters. This guide's `latest` / `main` examples do not specify the ZIP's pinned version. Required setup guidance is readable locally; optional references remain public links. The user configures their own MCP instance URL.

After the verification image is published, same-repository PRs, merged `main` and manual CI runs also upload **`TGPorterSkills.zip`** to the run's **Artifacts**, with a download link under **Agent skill CI preview** in the Summary. Preview bundles select the published `sha-<full-commit-sha>` image and digest; source and configuration URLs use the same commit. Their `release.json` has `kind: "ci"` and does not invent Release tag links. Downloads require GitHub login and are retained for 14 days. Fork and Dependabot PRs do not publish images or deployable preview bundles, but still run offline packaging tests.

Installation paths, loading or refresh behavior, and invocation syntax depend on the client. Example requests are "Use the tgporter skill to help deploy and connect MCP" or "Use the tgporter skill to list failed tasks." These illustrate intent rather than universal client commands.

Installing the skill does not deploy the service or configure MCP. Configure your own instance URL and token in the client as described above; the skill contains no shared service endpoint or real credentials. Deployment guidance reads matching local documentation or the deployment guide on GitHub; GitHub access is needed when local docs are unavailable. If the client lacks file or terminal access needed for deployment, follow the guidance manually. For routine download management, discover and inspect the connected MCP tools' descriptions, parameter schemas and annotations through the client. Use those definitions and actual results without fetching the GitHub tool reference. Derive batch sizes and pagination limits from the actual schemas.

The skill ships with this repository's source and version tags. Once a maintainer publishes a commit or release containing the directory, users can obtain and import it without a separate skill repository. Client-specific plugin packages or marketplace listings are optional distribution methods.

Maintainers can preview the bundle from the repository root using a Python 3.14 virtual environment with `requirements.txt` installed as described in [Manual Install](#manual-install), without Telegram credentials or publishing a Release:

```bash
python tools/build_skill.py --tag v1.2.3 --commit "$(git rev-parse HEAD)" --output-dir dist
```

The Release workflow supplies the actual tag, source commit and published image digest; `v1.2.3` in the local command is only a preview example.

### Submit a Download

Call `submit_download` with arguments such as:

```json
{"url": "https://t.me/channel_name/123"}
```

Public posts, private-channel posts such as `https://t.me/c/123456/789`, and topic-message links are supported. The downloader's logged-in Telegram account must have access to the chat. For MCP-only submissions, set `bot_token: ""`; the service stays running and starts the existing queue/recovery backend without creating another Bot session. Only single-message HTTPS `t.me` links are accepted; channel homepages, invite links, query parameters, whole-channel downloads and ranges are unsupported. Chat lookup times out after 10 seconds. Startup-not-ready and Telegram cooldown conditions return tool errors.

A `pending` response means durable queue acceptance, not a completed download. `task_id` identifies active/completed records; `pending_task_id` identifies the pending record and can be passed to `remove_pending_task`. `task_id_display` is for display only. Repeating a post that is still pending/downloading returns its existing task with `duplicate: true`. Submissions reuse concurrency limits and restart recovery without sending extra Bot notifications. Use `list_downloads` to follow progress.

## Docker Management

```bash
# Background start and logs.
docker-compose up -d
docker-compose logs -f

# Stop or restart.
docker-compose down
docker-compose restart

# Update to the image configured in Compose.
docker-compose pull
docker-compose up -d --force-recreate

# Container status.
docker-compose ps
```

For a container created with `docker run`:

```bash
docker logs -f tgporter
docker restart tgporter
docker stop tgporter
docker start tgporter
```

To update an image, recreate the container with the new image while preserving configuration, ports and mounts. Restarting the existing container does not change its image.

## Image Migration and Local Builds

The old `ghcr.io/smilekevin/hermes-telegram-downloader` package retains historical images. For migration, change Compose's `image` to the new package and select a newly built version or digest. Historical digests still require the old package address.

To build local source, clone the repository and prepare configuration, set `image: tgporter:local` and add `build: .` to the Compose service, then run:

```bash
docker-compose build
# Stop any background service before first login.
docker-compose stop tgporter
docker-compose run --rm tgporter
# After login, press Ctrl+C and start in the background.
docker-compose up -d
```

After updating local source, run `docker-compose up -d --build`. See [Docker Compose Deployment](#docker-compose-deployment) for first-login details.

## Docker Data Persistence

| Path | Description |
|------|-------------|
| `./downloads/` | Downloaded files |
| `./config.yaml` | Read-only startup configuration; never rewritten by the application |
| `./data/` | Mounted as `/app/data`; the application creates the runtime contents below |
| `./data/data.json` | Chat scan positions, retry IDs, ad rules, Bot language and concurrency |
| `./data/log/` | Logs + task persistence (bot_tasks.json / task_counter.json / download_history.json) |
| `./data/sessions/` | Telegram login sessions |
| `./data/bot.yaml` | Bot filter settings managed by the application |
| `./downloads/.temp/` | Staging files and `.parts/` checkpoints, persisted with downloads |

The application only reads `config.yaml`; scans, Bot commands and shutdown never rewrite it. Docker examples mount it with `:ro` and mount `data/` as a writable directory. First startup automatically creates `data/data.json`; no runtime file needs to be prepared. JSON saves use locked atomic replacement through a temporary file. Each `chat[]` record contains its `chat_id`, retry message IDs in `ids_to_retry` and the next scan position in `last_read_message_id`. Startup uses the later of the configured initial position and saved checkpoint. Records for currently unconfigured chats and unrelated data keys are preserved.

The top-level `filter_advertisement_list`, `replace_advertisement_list`, `group_add_advertisement` and Bot `language` now live in `data/data.json`. Existing config values remain initial defaults; saved data takes precedence whenever its key exists, including empty lists and dictionaries. Bot language, ad rules and concurrency settings save immediately, before success is reported. Browser language stays in browser storage. To restore initial ad rules or language, stop the service and remove only the corresponding data keys, preserving download state. To rescan earlier messages, also stop the service before adjusting that chat's saved checkpoint and configured initial position.

Persist only `config.yaml`, `data/` and the download directory. The application creates `data/log/`, `data/sessions/` and `.temp/` inside the download directory; no separate log, session or temp mount is needed. Recreating a container keeps this host data. A custom host download directory replaces `./downloads/` in this table.

When the main download account is a Bot, list the human operator's numeric Telegram user ID in `allowed_user_ids`. Numeric IDs do not depend on that Bot's peer cache. `me` refers to the main download account itself and does not automatically authorize a human operator. Startup help notifications are sent only to a human main account.

## Proxy Configuration

Configure the Kurigram connection to Telegram in `config.yaml`:

```yaml
proxy:
  scheme: socks5
  hostname: 172.17.0.1
  port: 1080
```

The template uses an HTTP proxy at `172.17.0.1:20172`. Match the scheme, host and port to your actual proxy. Other requests that support environment variable proxies can use Compose's `environment`:

```yaml
environment:
  - http_proxy=socks5://PROXY_IP:PORT
  - https_proxy=socks5://PROXY_IP:PORT
```

Inside a container, `127.0.0.1` refers to the container itself. Use a reachable proxy host; `172.17.0.1` depends on the Docker or NAS network. Manual installations use a host reachable from the local machine. If no proxy is needed, set `proxy: {}` and remove or comment out the Compose proxy environment variables. Omitting `proxy` or setting it to `null` also disables the Telegram proxy. Both the main client and Bot client convert empty proxy configuration to the `None` value required by Kurigram.

## Troubleshooting

1. **Container does not start**: inspect `docker-compose logs`; configuration mount sources must be files.
2. **Cannot connect to Telegram**: check proxy reachability from the container.
3. **Download fails**: inspect the failure reason in the WebUI Failed tab.
4. **Permission errors**: ensure mounted directories are writable.
5. **Session is lost**: check the `data/sessions/` mount and repeat interactive login.
6. **FLOOD_WAIT**: wait for the Telegram cooldown; pending tasks are retried automatically.

### Bot Heartbeat and Message Diagnostics

The dashboard header automatically refreshes Bot health, showing connected, connecting, disconnected, unresponsive, reconnecting, rate limited, stopped or not configured, with successful heartbeat round-trip latency and the last check time. `GET /get_bot_status` reads an in-memory snapshot under the existing Web login protection and sends no additional Telegram requests. MCP `get_status.bot_status` returns the same snapshot under the independent MCP token authentication, including in read-only mode; see the [MCP tool reference](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#mcp-tool-reference) for fields. `connected` requires a recent matching Pong; the heartbeat probes immediately on startup and shows connecting until the first Pong arrives, without an initial 60-second wait; a successful probe older than 70 seconds becomes unknown, and failed Web reads also show unknown. Without `bot_token` (including Mock mode), it shows not configured. This describes the Bot session only, independently of the main account and media connections.

When a Bot is configured, an independent loop sends an MTProto Ping immediately when the heartbeat starts, then every 60 seconds, and waits up to 10 seconds for a matching Pong. Each probe makes one attempt: Kurigram counts total attempts in `retries`, so `retries=1` sends once while `0` sends nothing and immediately reports a false timeout. No new configuration is required. Kurigram also sends its own keepalive Ping about every 5 seconds without waiting for a response; the application probe additionally verifies that Telegram responds. Three consecutive failures trigger a Bot reconnect attempt. A successful restart has a 300-second reconnect cooldown; a failed restart can retry after 60 seconds. The same independent loop handles the existing progress-report connection-error threshold. Probes defer during a known Bot FLOOD_WAIT; a FLOOD_WAIT from the probe records and respects the cooldown instead of counting as a network failure. Shutdown cancels and awaits the heartbeat; operation without a Bot does not start it.

For the default container name:

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

The same events go to `data/log/tdl.log` under the configured `log_level`, rotation and retention. At `INFO`, `Bot heartbeat OK` reports `rtt_ms`, the local `connected` flag, pending `update_queue`, `workers_alive`, `active_handlers`, the oldest handler's age and function name, and seconds since the last received message. `Bot heartbeat failed` includes the exception type, elapsed time and consecutive failure count. Reconnect attempts, outcomes and cooldowns are logged too; recovery emits `Bot heartbeat recovered`, and event-loop delays of at least 10 seconds report `event_loop_delay_s`. `Bot message received` records the message ID, type, `authorized` result and age since sending (`message_age_s`), followed by `Bot handler started` / `Bot handler finished` with the matching function, outcome and elapsed time. Logs exclude message bodies, command arguments, callback data and tokens.

Deprecation warnings for `message.forward_from_chat`, `message.forward_from_message_id` or the `reply_to_message_id` sending argument do not indicate network loss. The Bot now uses Kurigram `forward_origin` and `ReplyParameters`, preserving channel origins, Bot-inbox fallback and reply targets.

A Pong confirms round-trip communication on the Bot session; it does not prove message parsing, authorization or command replies work. If heartbeats succeed but `/help` does not respond, check for `authorized=False`, message receipt logs, `update_queue`, `workers_alive` and handlers that never finish. Missing heartbeats can also indicate a blocked downloader event loop, a stopped process or log-level filtering; a responsive WebUI does not rule these out. Probes send no test messages to users, create no additional Telegram sessions and do not restart the main download client.
