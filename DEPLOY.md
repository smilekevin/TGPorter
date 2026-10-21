# TG Porter - Deployment Guide

[中文部署指南](https://github.com/smilekevin/TGPorter/blob/main/DEPLOY_CN.md)

Use interactive web configuration and Telegram login in **Configuration** (preferred), setting the management password with the setup code from startup logs on first access. `config.yaml` is optional; command-line login requires the explicit `--login` option.

## Prerequisites

- Docker, plus the Compose plugin for Compose deployments; Python 3.14 for manual installation.
- Telegram API ID/API Hash and a Bot Token for basic setup. A phone account is optional when downloading with a user account.
- Network access to Telegram. Configure a proxy in the web page if needed; none is enabled by default.

The Docker image includes MCP and all runtime dependencies. Manual installations use `requirements.txt`; do not install Pyrogram or Pyrofork alongside Kurigram. Recreate the virtual environment when upgrading an older manual installation.

Images at `ghcr.io/smilekevin/tgporter` support amd64 and arm64.

Before upgrading, stop the service and back up `data/` and `downloads/`, including `.temp/`. Kurigram upgrades old session databases in place; restore the pre-upgrade session backup when rolling back the SDK. Never run two instances against the same session and task files.

## Quick Start

### Before You Deploy: API Credentials and Bot Token

1. Sign in at [my.telegram.org](https://my.telegram.org), open [API development tools](https://my.telegram.org/apps) and create an application to obtain API ID/API Hash. [Official instructions](https://core.telegram.org/api/obtaining_api_id).
2. Send `/newbot` to [@BotFather](https://t.me/BotFather) and keep the returned Token. [BotFather documentation](https://core.telegram.org/bots/features#botfather).
3. Enter credentials in Configuration after starting TG Porter. Basic configuration requires API ID, API Hash and Bot Token; downloads use the Bot unless you select a user account.

### Docker Image Deployment

Choose two writable absolute host paths and replace the placeholders in all commands and Compose examples before starting:

| Host path placeholder | Purpose | Container path |
| --- | --- | --- |
| `/path/to/tgporter/data` | TG Porter configuration, sessions, logs and task data | `/app/data` |
| `/path/to/downloads` | Downloads, temporary files and checkpoints | `/app/downloads` |

For example, use `/volume1/docker/tgporter/data` and `/volume1/downloads/telegram`. The directories can be anywhere on the host, independently of each other, the working directory or the source checkout. Reuse the original directories for upgrades or reauthorization.

```bash
mkdir -p "/path/to/tgporter/data" "/path/to/downloads"
docker pull ghcr.io/smilekevin/tgporter:latest
docker run -d \
  --name tgporter \
  --restart unless-stopped \
  -p 5088:5088 \
  -v "/path/to/tgporter/data:/app/data" \
  -v "/path/to/downloads:/app/downloads" \
  ghcr.io/smilekevin/tgporter:latest
docker logs tgporter
```

Open `http://SERVER_IP:5088/configuration` and use the `First-time setup code` from the logs. Complete [web setup and login](#webui-login); no interactive terminal or pre-created data file is needed.

### Docker Compose Deployment

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
```

Edit `docker-compose.yaml` first: replace `/path/to/downloads` and `/path/to/tgporter/data` on the left of the volume mappings with your chosen absolute host paths. Create those same directories and start the service; replace the paths in this command too:

```bash
mkdir -p "/path/to/tgporter/data" "/path/to/downloads"
docker compose pull
docker compose up -d
docker compose logs tgporter
```

The repository Compose file uses `/path/to/downloads:/app/downloads/` and `/path/to/tgporter/data:/app/data` as host-path placeholders, publishes `5088:5088`. The optional `config.yaml` mount is commented out; no YAML file is required. It has no default proxy or interactive terminal requirement. Open `/configuration` to initialize and log in.

The left side of each mount is the actual host path; the right side is the container path. In Configuration, use the container download path `/app/downloads`. Listener port changes also require matching Compose port mappings. Docker manages host mounts and port publication.

### Manual Install

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python media_downloader.py
```

Open `http://localhost:5088/configuration`, claim setup using the code from startup logs, set the web password, then enter API credentials and log in to Telegram. Downloads default to the project's `downloads/` directory. Subsequent starts reuse settings and valid sessions.

### Local Dev Mode

To preview the dashboard with example data:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install Flask Werkzeug
python run_local.py
```

Mock mode does not connect to Telegram or save settings. To test Configuration and login, run the [real application](#manual-install).

## Configuration

### Web Configuration and Interactive Login (preferred)

Use the header icons to switch between downloads and Configuration. Configuration dropdowns and single-line inputs use matching heights. **Sign out** ends your web login. See the [mobile and desktop screenshots in README](https://github.com/smilekevin/TGPorter/blob/main/README.md#webui-preview).

Use **Configuration** to manage Telegram login, downloads, web access and MCP. Settings are saved in `data/settings.json`; avoid editing it while the service is running. Leave saved password and Token fields blank to keep their current values. Telegram verification codes and two-step passwords are not saved.

The dashboard **Skipped** tab keeps reasons and saved filters across restarts. Desktop tabs and mobile bottom navigation share the same order (Downloading, Pending, Completed, Skipped, Failed) and outline icons. **Download** bypasses download filters, media types and formats; **Delete** removes only the record. Bot replies include skip reasons; verified existing files count as **Skipped**, without increasing successful downloads.

Bot submissions of single post links or forwarded media are registered only after the task is saved. The Pending consumer waits for a file concurrency slot before queueing them. A repeated submission of the same message while pending or downloading returns the existing task ID, without a second task. A save failure is reported explicitly. Check `/query pending` or the dashboard Pending tab, and search `data/log/tdl.log` by the displayed task ID to trace submission, preparation and queue handoff. Queue acceptance does not mean the download has completed.

### config.yaml (optional)

Optionally copy [config.yaml.example](https://github.com/smilekevin/TGPorter/blob/main/config.yaml.example) to `config.yaml` in the working directory before the first start. Fill in API credentials, account mode, download settings and any required proxy using the parameter table below:

```bash
cp config.yaml.example config.yaml
```

Prepare YAML before the first start. It is imported only if `data/settings.json` does not yet exist; later edits belong in Configuration. Existing settings take priority, and the YAML file is left unchanged. Do not delete settings, sessions or tasks to force another import.

Docker does not mount YAML by default. For the first import, add `-v "/path/to/config.yaml:/app/config.yaml:ro"` to the existing `docker run` command, or uncomment the `config.yaml` mount under the Compose service's `volumes`. Ensure the host file exists and retain the data/download mounts. Comment out this optional mount again after import if desired.

YAML does not automatically enable terminal prompts: interactive web login remains the default. Only explicit `--login` selects [command-line login](#command-line-login). Set `use_user_account: true` for a phone account or `false` for Bot Token downloads. See the example and parameter table for configuration fields.

### Parameters

| Parameter | Description | Default |
| --- | --- | --- |
| `api_id` | Telegram API ID; required to authorize the account | `0` |
| `api_hash` | Telegram API Hash; saved values are never echoed | `""` |
| `proxy` | Proxy config (scheme / hostname / port) | `{}` |
| `bot_token` | Required for basic setup; command/notification Bot and default download account ([@BotFather](https://t.me/BotFather)) | `""` |
| `use_user_account` | Use phone-account login for downloads instead of the Bot | `false` |
| `allowed_user_ids` | Allowed Bot command user IDs; human download accounts are allowed automatically, and legacy `me` grants no human access in Bot mode | `[]` |
| `language` | Bot/log language, independent of browser language; saves and applies immediately | `"en"` |
| `max_download_task` | Simultaneous files, 1–6; saves and applies immediately | `5` |
| `file_download_concurrency` | Concurrent download workers per file | `4` |
| `download_part_size_mb` | Fixed part size in MiB; positive integer | `64` |
| `save_path` | File save path | `"./downloads"` |
| `media_types` | Media types to download | `["audio", "document", "photo", "video", "voice", "animation"]` |
| `file_formats` | Extension lists for audio/document/video; all accepts every format | `{"audio": ["all"], "document": ["pdf", "epub"], "video": ["mp4"]}` |
| `file_path_prefix` | Ordered directory-name fields; an empty list adds none | `["chat_title", "media_datetime"]` |
| `download_filters` | Default download expressions, combined with OR; at most 32 rules of 4096 characters each | `[]` |
| `file_name_prefix` | Ordered filename fields; an empty list uses the message-ID fallback | `["file_name"]` |
| `file_name_prefix_split` | Separator between filename fields | `" - "` |
| `hide_file_name` | Hide filenames in UI | `false` |
| `date_format` | Media date directory format; shown when Media date is selected in directory fields, retained when deselected | `"%Y_%m"` |
| `drop_no_audio_video` | Compatibility field that is parsed but does not currently filter media | `false` |
| `enable_download_txt` | Save text-only messages as .txt | `false` |
| `chat` | Optional startup history scan list; nested fields are below; omit for MCP-only single-post submissions | `[]` |
| `filter_advertisement_list` | Advertisement keyword tags in Configuration → Downloads; any match (OR) skips forwarding | `[]` |
| `replace_advertisement_list` | Text removed from forwarded captions | `[]` |
| `group_add_advertisement` | Additional caption text mapped by destination chat ID | `{}` |
| `forward_limit` | Local forwarding-call budget per 60 seconds; does not bypass Telegram FLOOD_WAIT | `33` |
| `after_upload_telegram_delete` | Delete the local file after Telegram re-upload, including failed uploads | `true` |
| `web_login_secret` | Management password created during setup; protects Configuration even in public mode | `""` |
| `public_web` | Open dashboard/download APIs; Configuration still requires management authentication | `false` |
| `web_host` | Shared WebUI/API/MCP bind address; normally keep `0.0.0.0` inside Docker | `"0.0.0.0"` |
| `web_port` | Shared WebUI/API/MCP listener port | `5088` |
| `mcp_enabled` | Enable the same-port `/mcp` Streamable HTTP endpoint, always requiring a token | `false` |
| `mcp_token` | MCP-only bearer token; printable ASCII without whitespace; required when MCP is enabled | `""` |
| `mcp_read_only` | Expose only MCP query tools without restricting separate browser sessions | `false` |
| `mcp_allowed_hosts` | Additional allowed Host values: domain/IP with optional port; localhost/loopback already allowed | `[]` |
| `log_level` | Advanced dropdown immediately before Web debug logging: `TRACE`, `DEBUG`, `INFO`, `SUCCESS`, `WARNING`, `ERROR`, `CRITICAL`; requires restart after saving | `"INFO"` |
| `start_timeout` | Main Telegram session startup timeout in seconds; does not limit normal media transfers | `60` |
| `max_concurrent_transmissions` | 0 calculates capacity automatically; nonzero overrides SDK transport capacity | `0` |
| `upload_drive` | Optional cloud-upload configuration; nested fields are below | `{}` |
| `debug_web` | Flask debug state and ASGI debug logging | `false` |

#### Nested settings

| Parameter | Description | Default |
| --- | --- | --- |
| `chat[].chat_id` | Chat/channel ID or resolvable name to scan; required for each scan entry | — |
| `chat[].last_read_message_id` | Starting read-message ID; a later persisted checkpoint takes precedence | `0` |
| `chat[].download_filter` | Explicit download expression for this chat; empty inherits `download_filters` | `""` |
| `chat[].upload_telegram_chat_id` | Telegram destination for re-uploaded media; requires permission to send | `null` |
| `upload_drive.enable_upload_file` | Enable cloud uploads after downloading | `false` |
| `upload_drive.rclone_path` | rclone executable; bundled at the default path in the main image, installed separately for manual setups; configure its remote before use | `./rclone/rclone`, with `.exe` on Windows |
| `upload_drive.remote_dir` | Cloud destination, for example `drive:/telegram` | `""` |
| `upload_drive.before_upload_file_zip` | Compress each file into a ZIP before upload | `false` |
| `upload_drive.after_upload_file_delete` | Delete local files after cloud upload; false preserves them | `true` |
| `upload_drive.upload_adapter` | Upload adapter; normally `rclone`; legacy `aligo` needs extra installation and login and is absent from the main requirements | `rclone` |
| `proxy.scheme` | Telegram proxy scheme, such as `http` or `socks5`; separate from Compose environment proxies | — |
| `proxy.hostname` | Telegram proxy host without the scheme | — |
| `proxy.port` | Integer Telegram proxy port | — |

Media types, extensions and naming fields use checkboxes; add custom extensions and reorder naming fields with the arrows. For Bot commands, add your numeric Telegram user ID under **Allowed users**. A signed-in user account is automatically allowed; `me` does not grant a person access in Bot mode. Manage channels through Bot commands.

#### Download filters

In **Configuration → Downloads**, add/remove download expressions and advertisement keyword tags, then **Save settings**. Download expressions are validated when added; expand the reference for supported fields and examples.

Download filters use **OR**: any true rule matches; an empty list adds no filter. Use `and` within a rule to require multiple conditions. Media type/format restrictions still apply. Advertisement keywords use **OR** to skip forwarding when any case-sensitive, plain-text keyword occurs in the caption; remove tags with **×**.

Defaults apply to new download tasks without restart. Explicit task/channel expressions take precedence; existing tasks keep their saved rules. `/add_filter`, `/add_ad` and `/remove_ad` share these settings; refresh the page after Bot changes. Validation checks expressions without accessing Telegram or testing real messages.

#### Applying settings

- Password, public downloads, language, allowed users and download settings apply on save. New naming, path and part settings affect subsequent downloads.
- After changing API credentials, Bot Token, account mode, proxy or connection timeout, verify or reconnect in Configuration. No service restart is needed.
- Listener address/port, MCP, logging, startup channel scans and cloud-upload settings require restart. The page labels these fields.

#### Parallel downloads and checkpoints

`max_download_task` controls simultaneous files (1–6); `file_download_concurrency` controls connections per file (1–32); `download_part_size_mb` sets part size (1–4096 MiB). Defaults are 5 files, 4 connections per file and 64 MiB parts. Leave transport capacity at `0` for automatic sizing.

Each download preallocates one `downloads/.temp/timestamp_guid.tmp.temp` data file. Workers write directly at their range offsets, without separate `.part` files or a merge copy. Version 3 `.parts/manifest.json` records file identity, ranges, worker count and each range's durable prefix. Preallocated file length is not progress. Data is synchronized before atomically updating the checkpoint, normally after 8 MiB of new data or once a second, with forced commits on range completion and orderly stop. An abrupt exit may require downloading data written after the last commit again.

Pauses, network failures and restarts preserve new checkpoints. Saved ranges and worker counts take precedence over changed settings. Old `.part` files, older manifests and sequential `.temp` files without a new manifest are not migrated or resumed. Finish old downloads before upgrading, or explicitly delete the affected task's cache and download again. Unsupported cache is preserved with an error; older versions cannot resume new checkpoints either.

Where supported, allocation reserves disk space; otherwise only logical file length is set, so later writes can still run out of space or exceed a quota. Normal downloads need one file's data plus checkpoint metadata, without a second full merge copy. Linux/macOS use `pwrite`; platforms without it use independently opened file handles for positioned writes. NAS allocation and synchronization guarantees depend on the filesystem.

After every range completes, the file is synchronized and all handles close. Atomic renames first create the ready staging file and then publish it at the final path; completion is recorded afterward. Staging and destination must share a filesystem. Cross-device publication fails and preserves staging instead of copying. Avoid separate filesystem mounts inside the download tree.

When deleting a task, select file deletion only if you also want to remove local files and partial data. Cleanup waits for connections and in-flight disk writes to stop.

Bot/log language is set in Configuration; the header selector controls browser language. Supported codes: `en`, `zh-cn`, `zh-tw`, `ja`, `es`, `fr`, `ru`, `uk`; `zh` means Simplified Chinese. See the [language guide](https://github.com/smilekevin/TGPorter/blob/main/docs/languages.md) to add a translation.

Downloads skipped because a complete local file already exists appear in the Skipped tab with their filename and reason, and survive restarts. These duplicate downloads do not count toward completed downloads in the Web API, Bot queries or MCP. Deleting a skipped record preserves the local file. Filtered, unsupported and cancelled downloads are also excluded from completed downloads.

## WebUI Login

1. Open `/configuration`, enter the **First-time setup code** from startup logs, and create a management password. New or changed passwords need at least 8 characters with uppercase and lowercase letters and a number. Existing passwords remain valid; restarting before setup completes generates a new code.
2. In **Basic configuration**, enter API ID, API Hash and Bot Token, then click **Verify and sign in**. Downloads use the Bot by default.
3. To use your personal account, select **Use a user account for downloads**, enter your phone number with country code, then provide the Telegram code and account password if requested.
4. Wait for the green check. A Telegram login notification alone does not mean startup is complete.

Saved Telegram sessions are reused after restart. Use **Test connection** to check a saved login, or **Reconnect session** after a network failure. Expired or canceled login attempts can be started again. **Sign out of Telegram** signs out that account; tasks and partial downloads remain for recovery.

**Web access:** downloads and Configuration require the management password by default. Enable **Public downloads** to let anyone who can reach the service view, submit and manage downloads without signing in. Configuration still requires the management password. A blank password field keeps the existing password. Older password-free deployments retain public downloads; use the setup code to create a Configuration password.

**Remember me (7 days)** is optional: selecting it keeps the browser signed in for seven days, including browser and service restarts. Without it, the cookie lasts for the browser session and remains valid across service restarts, for up to seven days. The signing key is generated automatically in `data/web_session.key` with mode 0600; preserve this file when upgrading or recreating containers. Changing the password signs out other browser sessions; the current session remains signed in when you change its password. Web APIs use the same browser login and require a CSRF token for changes. An MCP Token is only for `/mcp`.

Upgrading from a version with an in-memory signing key requires signing in once more; subsequent service restarts preserve the new login.

The dashboard separates Active and Completed using task status; unfinished files display at most 99.9%. When a task leaves Active or you open Completed, the dashboard refreshes the latest completed records. Failed reads retry on subsequent polls without a page reload. Search and pagination retain the current filter, and history loads on demand.

**Task stages:** Pending tasks show **Queued**; after scheduling, the Downloading tab shows **Preparing**, then **Downloading**, or **Paused** after a manual pause. Queue capacity and Telegram cooldowns appear as separate reasons; zero speed does not move a download back to the queue.

### Command-line Login

Web login is recommended. For terminal login, save credentials and account mode in Configuration first, or prepare optional YAML before the first start, then add `--login`.

For a manual installation, stop the existing process and run from its original working directory and virtual environment:

```bash
python media_downloader.py --login
```

Follow the prompts for your phone number, Telegram code and account password if needed. Bot mode uses the saved Token. Codes and passwords are not saved. After login, the service keeps running; later starts can omit `--login`.

With Compose, stop the original service before launching an interactive instance with the same persistent mounts. Never share sessions between concurrent instances:

```bash
docker compose stop tgporter
docker compose run --rm -it --service-ports tgporter python media_downloader.py --login
# After successful login, press Ctrl+C to stop the foreground instance.
docker compose up -d tgporter
```

For a direct image deployment, replace the placeholders below with the exact host directories used by the original instance. Add the read-only YAML mount described above to the temporary container if using optional YAML:

```bash
docker stop tgporter
docker run --rm -it --name tgporter-login \
  -v "/path/to/tgporter/data:/app/data" \
  -v "/path/to/downloads:/app/downloads" \
  ghcr.io/smilekevin/tgporter:latest python media_downloader.py --login
# After successful login, press Ctrl+C to stop the foreground instance.
docker start tgporter
```

Use the original image version, data/download mounts and network settings. A user account needs an interactive terminal for first login; unattended deployments should use Configuration.

## MCP Agent Integration

### Enable MCP

MCP uses the existing WebUI port at `/mcp` and is disabled by default. No extra container is needed.

Generate a Token:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

1. In **Configuration → Web and MCP**, enter **MCP Token**, enable **Enable MCP**, save and restart.
2. For a domain, reverse proxy or remote IP, add the hostname used by the client under **MCP service hostname**, for example `nas.example.com` or `192.168.1.20:5088`. Localhost is already allowed. Use a hostname/IP with an optional port (1–65535), without a URL scheme, path, credentials or wildcard. Invalid entries show an error. This list does not change the listening address.

### Agent Client

```json
{
  "mcpServers": {
    "tgporter": {
      "url": "http://127.0.0.1:5088/mcp",
      "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"}
    }
  }
}
```

Use this separate Token for MCP, not your web password. **Read-only mode** allows queries only. See the [tool reference](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md).

### Install the Optional Skill

Download **`TGPorterSkills.zip`** from the [GitHub Release assets](https://github.com/smilekevin/TGPorter/releases) and give it to an agent that supports skills. If the client requires a directory, extract the ZIP and import `tgporter/`. Follow your client's import instructions.

You can also import the entire [`skills/tgporter/` directory](https://github.com/smilekevin/TGPorter/tree/main/skills/tgporter) from a source checkout. Keep `SKILL.md` and `references/` together.

Ask the agent to help deploy TG Porter, connect MCP or manage downloads. Skill installation alone does not start the service or configure MCP; provide your own instance address and Token. The client needs Streamable HTTP and Bearer Token support. For a downloaded bundle, use its bundled release information to select the matching image and source version.

CI preview ZIPs are available in the corresponding Actions run's **Artifacts** for 14 days and require GitHub login. They use that run's SHA image rather than a Release.

To build a preview ZIP locally, use the Python environment from [Manual Install](#manual-install):

```bash
python tools/build_skill.py --tag v1.2.3 --commit "$(git rev-parse HEAD)" --output-dir dist
```

`v1.2.3` is an example tag for this local preview; the command does not publish a Release.

### Submit a Download

Use MCP `submit_download` or the download page to submit one HTTPS `t.me` post link, such as `https://t.me/channel/123` or `https://t.me/c/123456/789`. Topic links are also supported. The download account must have access to the post. Accepted tasks enter the queue; acceptance does not mean the download is complete. Submitting the same unfinished post returns its existing task.

Older Bot-free deployments remain supported. Web and MCP submissions do not send Bot notifications automatically.

## Docker Management

```bash
docker compose logs -f tgporter
docker compose stop tgporter
docker compose up -d
docker compose restart tgporter
docker compose pull
docker compose up -d
docker compose ps
```

Only GitHub Releases update `latest`. Ordinary CI publishes `sha-FULL_COMMIT_SHA` images. To keep a fixed version, use its version or SHA tag in Compose; image references and digests are listed in the Release notes.

The image includes its application version. Release builds display the release tag; CI builds display the nearest version tag reachable from the built commit plus its short hash, such as `v0.3.2+92afcd8`. Without a reachable tag, CI displays `sha-92afcd8`. CI builds on a tagged commit also include the hash to distinguish them from Release builds. Published CI image tags still use the full commit SHA. Do not override `APP_VERSION` in Compose.

## Image Migration and Local Builds

When upgrading an older installation, optional YAML and saved language, concurrency and advertisement rules migrate on the first start without `data/settings.json`. After that, use Configuration. See [optional YAML import](#configyaml-optional).

Stop the old service, back up data and reuse the existing `data/` and download mounts. After import, future deployments need only those directories. Do not remove sessions or task files to reset configuration.

Build locally with the source version:

```bash
docker build --build-arg APP_VERSION="$(git describe --tags --always --dirty)" -t tgporter:local .
```

Set the Compose image to `tgporter:local`, then run `docker compose up -d`. Keep local image tags distinct from published releases.

## Docker Data Persistence

| Path | Purpose |
| --- | --- |
| `data/settings.json` | Configuration, credentials, Bot/log language, concurrency and ad rules; created automatically with mode 0600 |
| `data/web_session.key` | Private browser-session signing key; preserves logins across service restarts |
| `data/data.json` | Channel checkpoints and retry IDs; unrelated fields and disabled-channel records are retained |
| `data/sessions/` | Telegram authorization |
| `data/bot.yaml` | Existing Bot task/listener metadata |
| `data/log/tdl.log` | Application log, 10 MB rotation, 30-day retention, configured level |
| `data/bot_tasks.json` | Download and forwarding tasks |
| `data/task_counter.json` | Daily task sequence numbers |
| `data/download_history.json` | Completed and failed downloads |
| `downloads/` | Completed files |
| `downloads/.temp/` | Staging data and `.parts/manifest.json` checkpoints |

Mount writable `data/` and download directories. The application creates its runtime files and keeps partial downloads under `.temp/`; no separate temporary-data mount is needed. Recreating the container preserves host-mounted data. Keep backups private because they contain credentials and Telegram authorization. JSON data files are stored directly in `data/`; `data/log/` contains logs only. For older installations, stop the service and move JSON files from `data/log/` to `data/` before restarting; the old location is no longer read.

## Proxy Configuration

In **Configuration → Advanced → Telegram proxy**, enable the switch, select `socks5`, `socks4` or `http`, and enter host and port. For Docker Desktop, a reachable SOCKS5 proxy might use `host.docker.internal:1080`. Username/password are optional; blank fields keep saved credentials. Disable the switch to remove the proxy. Save and verify or reconnect to apply changes.

Container `127.0.0.1` refers to the container itself. Docker Desktop may use `host.docker.internal`; Linux needs a host address actually reachable from that container. Check your own network instead of copying another machine's gateway. Docker image-pull proxies and Telegram SDK proxies are separate.

## Troubleshooting

- **Web page unavailable:** check port conflicts/mappings, firewall and listener address. Telegram connectivity does not gate the initial Configuration page.
- **Missing setup code:** inspect current startup logs. Codes appear only before setup completes and rotate on restart. Open `/configuration` on migrated public deployments.
- **Login failure:** verify API credentials, proxy and permissions. Request a fresh expired code and respect the rate-limit countdown. No terminal interaction is needed.
- **Invalid session:** reconnect from Configuration, or sign out and log in again if reconnect fails. Tasks and checkpoints remain.
- **Changes not applied:** check the restart notice; port changes also require Docker mapping updates.
- **MCP 404 / 401 / Host rejection:** check enable/restart, the separate Token, and actual Host/allowlist respectively.
- **Save failure:** check data write permissions and disk space; failed atomic replacement preserves the old file.

### Bot Heartbeat and Message Diagnostics

The dashboard shows Bot connection status, latency and last check time. Without a Bot Token, it shows not configured. This status describes the Bot connection; it does not confirm download-account access or Bot command permissions. The service checks periodically and tries to reconnect after repeated failures; Telegram rate limits are respected.

View recent logs:

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

Application logs are also in `data/log/tdl.log` (10 MB rotation, 30-day retention). Use `INFO` for normal operation and `DEBUG` for more connection detail.

- **Bot is disconnected:** check Telegram access, proxy settings and recent `Bot heartbeat failed` errors.
- **Bot is connected but commands do not respond:** check **Allowed users** in Configuration. `authorized=False` in the logs means the sender lacks permission.
- **Messages arrive but replies stall:** check `Bot handler started` / `Bot handler finished` and reconnect errors.
- **Single-message links:** send the `https://t.me/...` post link directly to the Bot; link previews are supported for public and private channels. The download account must have access to the post.
- **Status is unknown:** check the last check time, whether the service is still running and whether Telegram is reachable.

MCP exposes the same Bot status; see the [tool reference](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#mcp-tool-reference). To verify real download integrity, follow the [parallel-download guide](https://github.com/smilekevin/TGPorter/blob/main/docs/parallel-download-testing.md).
