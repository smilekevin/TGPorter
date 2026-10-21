# Project Guidelines for Agents

These instructions apply to the entire repository. Read this file, relevant source code, and the working tree diff before making changes. Also follow any more specific AGENTS.md in the directory you are editing. Explicit user instructions for the current task take precedence.

## Branches and Pull Requests

- **Branch names must not contain `codex`, including `codex/` and `codex-`. Do not use the tool's default Codex branch prefix.**
- Name branches by purpose: `feat/<feature>`, `fix/<issue>`, `docs/<topic>`, `refactor/<module>`, `test/<scope>`, or `chore/<task>`. Use short lowercase English names with hyphens, such as `docs/project-agent-guidelines`.
- Choose a compliant name before creating a branch. Run `git branch --show-current` before each commit, push, and PR creation. If the current branch violates the naming rule, move the task's changes to a compliant new branch before committing or pushing.
- Normally start from the target repository's latest `main`. If the current checkout contains another task or uncommitted changes, use an isolated checkout. Do not overwrite, stage, or commit unrelated user changes.
- This fork's PR target is `smilekevin/TGPorter`. The local `fork` remote normally points there, while `origin` normally points to upstream `MangoIsIllegal/hermes-telegram-downloader`. Verify the actual URLs with `git remote -v` before pushing; do not infer the target from the remote name alone.
- Write all PR titles and descriptions in English.
- Keep each PR focused on one problem. Commit messages and PR titles should describe the resulting behavior; prefixes such as `feat:`, `fix:`, and `docs:` are appropriate. Describe the reason for the change, validation results, and anything not verified in the PR body.
- When the user asks for a PR, complete the relevant commit, push, and PR creation. Merge or publish a Release only within the user's authorization; a request to create a PR does not automatically authorize merging it.

## Project Purpose and Source Map

This project downloads, forwards, and listens for Telegram media. It uses Python, asyncio, Kurigram, and Flask, with Telegram Bot and WebUI entry points. Tasks, download history, and partial downloads must survive process restarts.

| Path | Responsibility |
| --- | --- |
| `media_downloader.py` | Main application, file task queue, worker pool, download execution, startup, and shutdown |
| `module/app.py` | Managed settings loading/migration, runtime data persistence, Application, TaskNode, and task status types |
| `module/settings.py`, `module/configuration.py` | Validated atomic settings and authenticated Configuration/setup routes |
| `module/telegram_login.py` | Non-interactive Telegram authorization on the downloader loop |
| `module/runtime_paths.py` | Default data directory, runtime JSON, log, session and Bot configuration paths |
| `module/bot.py` | Bot commands, message entry points, task recovery, Pending consumer, forwarding, and listening |
| `module/bot_query.py` | Read-only `/query` task lists and pagination using the shared download service |
| `module/task_store.py` | Persistent tasks and daily task sequence numbers, using locks and atomic file replacement |
| `module/bot_message_store.py` | Atomic, private ring of the last 1,000 handled Bot inbox identities for replay deduplication |
| `module/download_stat.py` | Download progress, history, failures, pause, and cancellation state |
| `module/parallel_download.py` | Fixed-size ranges, per-file queues, durable checkpoints, and atomic publication |
| `module/partial_file.py` | Preallocation, positioned writes, disk synchronization, and cancellation-safe disk operations |
| `module/staging.py` | Stable ASCII staging names and compatibility with legacy checkpoint paths |
| `module/download_connection.py` | Independent connections owned by download workers and resource cleanup |
| `module/telegram.py` | Python 3.14-compatible import of Kurigram through the pyrogram namespace |
| `module/pyrogram_extension.py` | Telegram download/upload integration, file reference refresh, rate-limit state, and SDK extensions |
| `module/web.py` | Flask pages/API, authentication/CSRF and the single-port ASGI listener |
| `module/download_service.py` | Shared task queries and mutations for Web API and MCP, including async retry submission |
| `module/templates/`, `module/static/` | WebUI templates, native HTML/CSS/JS, and static assets |
| `module/filter.py`, `module/cloud_drive.py` | Message filtering and optional cloud uploads |
| `lang/`, `module/language.py` | Shared per-language JSON catalogs, discovery and translation; compatibility imports |
| `run_local.py` | Standalone Mock WebUI for UI development; does not perform real downloads |
| `tests/` | unittest regression tests that require no Telegram account |
| `tools/benchmark_download.py` | Real Telegram download benchmarks and interruption/resume acceptance checks |
| `module/mcp_server.py` | Integrated Streamable HTTP MCP tools at `/mcp`, using shared download operations |
| `skills/tgporter/` | Distributable user skill for deployment guidance, MCP connection and download management |
| `tools/build_skill.py` | Release skill ZIP generation with bundled documentation, examples and pinned image/source references |
| `README.md`, `DEPLOY_CN.md`, `DEPLOY.md`, `docs/` | Bilingual overview, separate Chinese/English deployment guides, and focused design/test guides |

## Design Constraints

### Scheduling and Recovery

- Persist single-message tasks as Pending before the consumer fetches their messages and hands them to the shared file task queue. Batch history scans and live listeners also feed this queue.
- `recover_tasks()` in `module/bot.py` restores task state and sequence numbers. Runnable incomplete tasks return to Pending to avoid a burst of network requests after a restart. Individual pauses persist in `bot_tasks.json`, stop writers before releasing capacity and appear in Pending as paused; restart and global resume must not make them runnable. Explicit resume returns them to the normal pending queue.
- `module/download_control.py` owns loop-bound interruption and detaches paused files from range/listener owners without losing account, filter, topic, destination or v3 checkpoint data.
- Keep offline Bot replay enabled and register authorized handlers before startup. Deduplicate message handlers by Bot account/chat/message ID using `data/bot_processed_messages.json`; persist only handled messages, retain the last 1,000 identities and preserve callback button reuse. Concurrent duplicate messages wait for the first handler; exceptions, cancellation and rejected single-message task writes remain retryable. Receipt persistence does not replace task persistence or provide an exactly-once transaction.
- Keep the Bot heartbeat independent of progress notifications. Require a bounded Telegram response, respect Bot FLOOD_WAIT, log dispatch metadata without message contents or credentials, and cancel and await the singleton monitor during shutdown.
- Start the Pending consumer even when there are no historical tasks. Repeated startup must not create duplicate consumers. Cancel and await background tasks during shutdown.
- Tasks being fetched, queued, and actively processed must share the concurrency budget. Release reservations on rejected handoffs, exceptions, and cancellation to prevent duplicate consumption, lost tasks, and leaked capacity.
- Range-task recovery must preserve scan bounds, checkpoints, forwarding destinations, and topics. Single-message recovery must retain the owning account; use the Bot account to download and refresh Bot inbox media.
- Handle FLOOD_WAIT using shared cooldown timestamps and asynchronous waiting. Do not use blocking `time.sleep()` inside the event loop or request handlers. Bound retries and notification waits.

### Parts and Data Lifecycle

- `max_download_task` limits simultaneous files; `file_download_concurrency` controls workers per file; `download_part_size_mb` sets the fixed part size, with the final part containing the remaining bytes. Keep these controls distinct.
- Each file has its own part queue. Each worker owns an independent Telegram download connection and reuses it for its subsequent parts. Do not share download connections between workers or files. Start connections one at a time, at least one second apart. Keep at most one unconfirmed connection per file: only that worker writing its first valid block may unlock expansion; progress on older healthy connections must not unlock another worker's startup slot. On failure or cancellation, close the starting connection before releasing its slot. Retire failed connections and return their unfinished parts to the queue without stopping healthy workers. Only three consecutive failures of the last available worker exhaust the download; do not multiply this budget with file-level retries. FLOOD_WAIT blocks new connections during the shared cooldown while healthy existing connections continue. Resume automatically after cooldown, including long waits, while preserving manual pauses and cancellation.
- Bootstrap foreign non-CDN download keys on the regular DC endpoint, then open each worker's independent media connection. Cache foreign key bytes in memory on the owning client, scoped by DC/test mode and protected by per-key locks; reuse them across workers/files without sharing media sessions or transports. Cancellation must release key-creation locks without stopping unrelated files. Clear the cache after client shutdown cleanup and evict matching rejected keys without deleting newer replacements. Reuse stored home-DC keys and keep CDN endpoint selection separate. Bound auth-key creation to 60 seconds and close interrupted transports before retrying; never apply that limit to normal media transfers.
- New media staging paths use flat `downloads/.temp/timestamp_guid.tmp` names derived from message time and stable account/message/media identity. Never generate a new path per retry or use channel titles in new staging paths. Keep exact staging paths in shared download records for deletion. Old part/sequential checkpoint formats are unsupported and must be preserved with an error until explicitly removed; never import or infer their progress.
- Write ranges into one preallocated staging `.temp` file, never separate `.part` files or a merge copy. Use `pwrite` where available and independent binary file handles otherwise; loop over short writes and drain in-flight disk operations before closing, reassigning ranges, publishing or deleting.
- Save version-three part plans and committed range prefixes in `.parts/manifest.json`. Synchronize data before atomically committing metadata. Preallocated length is never progress. Publish through same-filesystem atomic renames and acknowledge completion only after the final output exists; preserve checkpoints on storage errors and do not reconnect Telegram for them.
- Pauses, retries, and restarts reuse saved version-three ranges and worker counts; configuration changes must not repartition existing downloads or download completed parts again.
- Aggregate progress across parts into one file record. Bytes restored from checkpoints do not contribute to new transfer speed. Keep one progress row per file in WebUI.
- Pauses, network failures, and process shutdown preserve checkpoints. Explicit user cancellation first stops writers and closes connections; Web/API/MCP deletion removes that file's staging data and `.parts/` only with `delete_file=true` (Bot cancellation keeps its cleanup default). Do not affect other downloads or completed files.
- Changes to recovery or publication must preserve file identity, range validation, integrity checks, and version-three checkpoint recovery. Older layouts are intentionally unsupported.
- Store task, counter and download-history JSON directly in `data/` (`bot_tasks.json`, `task_counter.json`, `download_history.json`); `data/log/` is for logs only. Do not read legacy JSON paths under `data/log/`.
- Preserve locking and atomic replacement when writing tasks, counters, and part plans. Do not bypass the storage APIs to modify shared JSON files directly.

### WebUI and Async Boundaries

- Use the existing Flask templates and native frontend. Do not introduce a frontend framework or build pipeline for a local UI change.
- Flask request threads and the asyncio download loop have a thread boundary. Follow the existing thread-safe coroutine submission approach rather than manipulating main-loop tasks directly from a request thread.
- A non-empty `web_login_secret` protects pages and Web APIs using password-login sessions only. State changes require the session CSRF token. Never add a Bearer-token bypass to Web APIs, dashboard, login or logout. Legacy empty-password deployments retain public WebUI/API mode independently of MCP. New deployments require setup; show Management password followed by a Public downloads checkbox with concise password-free download help text. Public mode opens only the dashboard and download APIs, while Configuration retains management-password authentication. A blank password input preserves its saved value and must never enable public access implicitly. The logged one-time setup code is not a management password. Login offers an unchecked remember-me option: a seven-day persistent cookie when selected, otherwise a browser-session cookie. Persist the private signing key atomically in `data/web_session.key` with mode 0600 so both survive service restarts within their seven-day validity. Password changes invalidate other browser sessions while retaining the editor's session.
- When changing an API, check frontend requests, response shapes, and the real Flask route together. Mock UI checks do not replace production API validation.

### Agent and MCP Integration

- MCP lives in the main process/image on the same `web_host` / `web_port` as WebUI and APIs. Keep the existing Flask paths compatible; `create_web_app()` is the production ASGI entry point and must run the MCP session manager's lifespan. Do not add a sidecar, second listener or loopback Web API client.
- Web API and MCP call `DownloadService` directly. Keep business operations independent of Flask requests and responses. Preserve task persistence and the existing thread-safe submissions to the downloader loop; do not create another worker pool or Telegram session.
- `mcp_enabled` defaults to false and requires a non-empty `mcp_token` when enabled. Disabled `/mcp` returns 404. Every MCP request requires the MCP-only Bearer token, even if the browser password is empty; browser cookies never authenticate MCP, and MCP tokens never authenticate Web APIs. Use one `mcp_allowed_hosts` list for additional explicit hosts with optional ports, retaining loopback defaults. Derive the SDK HTTP/HTTPS Origin rules from that same list; do not add a separate origin setting. Reject paths, credentials and wildcards. This is an access allowlist, not a listener or connection URL. Tokens never belong in tool parameters, URLs, logs or error text.
- Expose typed, bounded tools with accurate read-only/destructive annotations. Use stable `task_id` values rather than `task_id_display`. Deletion defaults to preserving local files and partial data; explicit `delete_file=true` removes them after writers stop. `mcp_read_only` exposes only query tools and does not restrict independent browser sessions.
- Translate shared-operation failures (`code="0"`) into MCP tool errors. Preserve batch retry's partial failure details and distinguish queue acceptance from completed downloads. MCP `submit_download` accepts one validated HTTPS `t.me` post link, resolves chat metadata on the downloader loop and persists it before the existing consumer fetches media. Return acceptance only after an atomic write succeeds; deduplicate unfinished posts and preserve the main account identity and topic metadata across restarts. Return `task_id` for active/completed rows and `pending_task_id` for pending cancellation. WebUI and MCP submissions do not infer a Bot notification recipient and must not bypass concurrency or shared Telegram cooldowns. WebUI `/submit_download` shares this submission path and session/CSRF protection. Without `bot_token`, initialize the same task backend without a Bot session even when MCP is disabled, keep the service running after configured downloads finish, and await its consumer/reply tasks during shutdown.

## Implementation Conventions

- Prefer minimal changes. Follow the surrounding module's style, four-space indentation, and existing names. Do not bundle unrelated formatting, dependency upgrades, or architectural rewrites.
- Keep runtime Python, JS and HTML source in English, including comments and diagnostic text. Chinese UI text belongs in `lang/`; tests may retain language fixtures. Do not add historical-message aliases: unrecognized records stay literal. Persist canonical English error text and translate it at display time. Bot and WebUI share `lang/*.json` message keys. Keep one file per language: English lists canonical message keys once, while translated catalogs map English keys to values. Generate current-message matching rules from English templates; reuse shared vocabulary, preserve named placeholders, and use English fallback. `zh` aliases `zh-cn`; `zh-tw` is Traditional Chinese. New catalogs are discovered at startup; see `docs/languages.md`.
- Use English identifiers and explain the intent of public functions and complex async flows. Comments should explain state, concurrency, or compatibility decisions rather than restate code.
- CI and Docker use Python 3.14. Manage all downloader and integrated MCP dependencies through `requirements.txt`; the main image includes them even when MCP is disabled. Pin directly imported ASGI and validation libraries (Starlette, Uvicorn and Pydantic) explicitly instead of relying on MCP transitive resolution. Kurigram is pinned in the downloader requirements and supplies the `pyrogram` import namespace; do not co-install Pyrogram or Pyrofork. Import it through `module.telegram` to scope its import-time loop on Python 3.14. Worker-owned media/CDN sessions use Kurigram endpoint and authorization interfaces. Existing version-six session databases migrate in place; back up sessions before migration and restore that backup when rolling back.
- Write application file logs only to `data/log/tdl.log` with the existing rotation, retention and configured level. Do not add a duplicate download log sink.
- Use the existing Loguru logging setup for important lifecycle events. Preserve connection-open and connection-close logs used by download acceptance checks.
- Configuration changes must update `module/app.py`, the schema/defaults in `module/settings.py`, and relevant documentation together. Bot command or Web API changes must be reflected in both README languages.
- Configuration is managed through the native web Configuration page and persisted in `data/settings.json` with a lock, private permissions and atomic replacement. Start the WebUI without credentials or a session. Claim first setup with a one-time code from startup logs and a CSRF-protected request. Newly created or changed web passwords require at least 8 characters with uppercase and lowercase ASCII letters and a digit; preserve existing passwords and keep frontend/backend validation aligned; Configuration always requires the management session after setup, even in public Web mode. Basic configuration requires API ID, API Hash and Bot Token for new login. Default downloads use the Bot; `use_user_account` reveals phone authorization. Verify the command Bot credentials on its existing session before reporting success. Publish credential-free authorization/startup stages, distinguish a saved login from backend readiness, and retain authorized sessions for reconnecting after network startup failures without another phone code. Offer logout only when a saved login exists and show connection results beside a dedicated test button. A failed probe of a running backend must not stop downloads. Keep legacy Bot-free backend compatibility. Use native selection/add controls for list settings, place the structured proxy in Advanced, and retain channel settings for Bot management without exposing a channel editor. Group the introductory login text, credentials, account switch and authorization controls in one panel. Allowed users default to an empty list; human download accounts are authorized automatically, while legacy `me` must not authorize the Bot itself as a human operator. Preserve existing saved allowlists during migration. Label only settings requiring restart. Never echo saved secrets or persist Telegram codes/two-step verification passwords. All SDK authorization operations belong to the downloader loop. Default startup, Web operations and reconnects never fall back to stdin; explicit `python media_downloader.py --login` may authorize interactively before the WebUI starts, then must disable terminal input and clear SDK code/password fields.
- `config.yaml` is optional for first-start import; web Configuration is preferred. YAML presence must not select terminal login; only explicit `--login` does so. Import it only if settings do not exist and the old file exists, carrying over prior runtime language, concurrency and ad rules. Existing settings always win; never read, write or delete the old file afterward.
- Generate `data/data.json` automatically for scan checkpoints and retry IDs. Use `Application.save_app_data()` for runtime snapshots and Bot setting saves; it routes changed language, concurrency and ad rules to settings. Keep settings separate from task checkpoints, preserve numeric ad-map chat IDs, match saved chats by ID and retain unrelated runtime data. Saved restart-pending settings must not be overwritten by checkpoint saves. Persist before acknowledging success.
- Password, public Web mode, language, allowlists and download settings apply live. Credentials, account mode, proxy and connection timeout apply through safe Telegram reconnection without restarting the service. Keep saved-session state separate from the connection test result and keep secret inputs masked during verification. Listener/logging/MCP, startup scan and cloud-adapter changes require restart. Preserve existing part plans, tasks and sessions. Stop consumers and writers before Telegram logout or session removal.
- Filtering uses Lark in `module/filter.py` and does not generate parser tables. Do not edit or depend on legacy `module/parsetab.py` files. Preserve saved-expression precedence, byte/date literals, eager boolean evaluation and missing-value behavior when changing the grammar; verify with `tests/test_filter.py`.
- Configuration download filters are an OR list in `download_filters`: any true rule matches, and an empty list adds no filter. Explicit task/channel expressions take precedence. Snapshot the resolved expression before persisting a new task and retain it during recovery. `/add_filter` appends to the same settings list. The management-session/CSRF-protected `/api/configuration/validate-filter` checks drafts without saving or Telegram access, using the same validation as settings writes. Advertisement keyword matching remains OR and separate from download rules.
- Expose `filter_advertisement_list` as add/remove keyword tags in Configuration Downloads, sharing live persistence with `/add_ad` and `/remove_ad`. Caption matching remains case-sensitive literal substring OR; do not interpret keyword text as download expressions or regexes.
- Persist skipped messages and canonical reasons in download history. Explicit skipped-record downloads use the existing Pending consumer, preserving the owning account and a durable filter/type/format bypass. Deletion removes history only. Verified existing files count as completed in Bot reports without new traffic or duplicate uploads.
- Never commit real settings, legacy configuration, `data/data.json`, sessions, Bot Tokens, API credentials, runtime logs, downloaded files, or checkpoints. Keep these out of debug output and PR descriptions as well.

## Documentation Synchronization

- For future project changes, update `README.md`, Chinese `DEPLOY_CN.md` and English `DEPLOY.md` together in the same change set. Keep the overview and both deployment guides consistent with the final behavior, configuration and supported usage.
- `README.md` owns the project overview, features, architecture, Bot commands, Web API and short startup summaries. Deployment entries must link to the guide for the corresponding language.
- `DEPLOY_CN.md` (Chinese) and `DEPLOY.md` (English) own prerequisites, credentials, complete installation and login steps, configuration examples and defaults, parallel-download settings and checkpoints, image updates, local builds, persistence, proxies and troubleshooting.
- Maintain both language sections in `README.md` and keep the two deployment guides synchronized. Use the display name `TG Porter`, repository name `TGPorter`, and lowercase image/service/container name `tgporter`.
- Keep summaries and links aligned with the detailed guide instead of duplicating complete commands, configuration blocks or persistence tables in README.
- Maintain `AGENTS.md` when entry points, authentication, dependency management or deployment conventions change. `docs/mcp.md` owns the MCP tool reference; complete MCP Token and setup instructions belong in both deployment guides.
- Keep `skills/tgporter/` aligned with deployed MCP behavior and independent of a particular agent client or installer. Runtime tool usage must inspect the connected MCP definitions instead of depending on GitHub documentation or fixed schema limits in the skill. Keep full deployment commands in the deployment guides; installed skills must include their own supporting resources or link to version-appropriate published deployment documentation.
- Release skill ZIPs use an explicit allowlist for the English `DEPLOY.md`, setup examples and license. Exclude the Chinese guide, developer instructions, test plans, design drafts and repository overviews. Copy Skill and English deployment documents unchanged; keep optional references as public URLs. Define release-specific images and URLs once in generated release metadata, render its companion guide from that data, and set only the Compose service image structurally. Skill setup must prefer those release values over generic guide examples. Never package runtime configuration, data or sessions.
- Before finishing, check relative links and heading anchors, run `git diff --check`, and verify commands, image tags, configuration defaults and paths against `module/settings.py`, `docker-compose.yaml`, the workflows and relevant source code.

## Development and Validation

Run commands from the repository root and use an isolated Python environment:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

- For documentation-only changes, check links, examples, and `git diff --check`. For Compose changes, also run `docker compose -f docker-compose.yaml config --quiet`.
- For runtime changes, start with the relevant existing tests and add meaningful regression coverage for behavior changes. Run the full offline suite for changes spanning scheduling, recovery, or part downloads.
- Tests use `unittest`, `IsolatedAsyncioTestCase`, mocks, and temporary directories. Do not connect to Telegram or write real task/session data in offline tests. Restore patched module state after each test.

```bash
# Full offline regression suite.
# Workflow script tests also require Node.js 24, which CI installs automatically.
python -B -m unittest discover -s tests -v

# Part-download and connection tests.
python -B -m unittest discover -s tests -p 'test_parallel*.py' -v
python -B -m unittest discover -s tests -p 'test_download_connections.py' -v

# Web authentication and real MCP protocol tests against the single-port production app.
python -B -m unittest discover -s tests -p 'test_web*.py' -v
python -B -m unittest discover -s tests -p 'test_mcp*.py' -v

# Mock UI development.
python run_local.py

# Real application; use /configuration for initial setup and Telegram login.
python media_downloader.py
```

- Follow `docs/parallel-download-testing.md` for real download acceptance checks. Use isolated directories and compare SHA-256 values. File length, Mock progress, or part counts alone do not prove file integrity or network speed improvements.
- Distinguish static checks, offline tests, published-image Mock deployments, and real Telegram acceptance checks in reports. Clearly identify checks that were not run.

## Docker and Publishing

- The project display name is `TG Porter` and the repository is `TGPorter`. Docker image, Compose service and container names use lowercase `tgporter`. Preserve upstream project names and attribution links.
- The image repository is `ghcr.io/smilekevin/tgporter`. Docker uses a multi-stage Alpine build with `python media_downloader.py` as the runtime command.
- The main image includes MCP. Enable it in Configuration and restart; reuse port 5088 and the existing Compose service and mounts. No separate MCP image, sidecar, optional dependency file or port mapping is required. Main-image CI and Release builds include the integrated MCP dependencies automatically.
- Persist writable download and configuration/data directories only. Deployment and Compose host paths are explicit user-replaced absolute-path placeholders, independent of the working directory; keep container paths `/app/downloads` and `/app/data` stable. Settings, runtime JSON, `data/log/`, `data/sessions/` and `data/bot.yaml` are managed by the application. Deploy directly in the background; initial and subsequent Telegram login defaults to Configuration without a TTY. Explicit `--login` is an optional foreground terminal flow; user-account login requires a TTY and an isolated instance using the same persistent mounts after stopping the original service. Removing a container must not delete host data.
- Staging defaults to `save_path/.temp/`, or `/app/downloads/.temp` with the example configuration. Do not add a separate Compose temp mount or temp configuration setting. Session files contain Telegram authorization, not application preferences or ad rules.
- Before deployment tests, check host ports and proxy reachability. Do not assume the example proxy address works on the current machine. Keep test overrides and temporary data in an isolated directory and out of the PR.
- `.github/workflows/tests.yml` runs tests and amd64/arm64 builds for PRs, `main` pushes, and manual runs. Publishable CI runs push only `sha-<full-commit-sha>` tags and do not update `latest`.
- `.github/workflows/docker-build.yml` publishes version, SHA, and `latest` tags when a GitHub Release is published, then updates the Release notes with image references. Do not make ordinary PRs publish or overwrite `latest`.
- Release builds pass the Git tag as Docker build arg `APP_VERSION`; CI builds use the nearest reachable version tag plus a short commit hash, such as `v0.3.2+92afcd8`, or `sha-<short-commit-sha>` when no tag is reachable. Fetch complete history and tags for CI version resolution. Published CI image tags remain `sha-<full-commit-sha>`. The runtime image preserves `APP_VERSION` in its environment and OCI version label. Keep `utils.__version__` as the shared version export: environment, then local Git description, then `unknown`. Compose must not override the baked version with `latest`; local builds must pass their source version explicitly.
- For workflow changes, run the tests in `tests/test_docker_workflow.py` and check tag rules, permissions, digests, Summary output, and repeatable Release notes updates.
- Release builds also generate `TGPorterSkills.zip` after image publication and upload it to the same Release. Reruns replace the same asset and update its link in the managed Release notes section. Run `tests/test_skill_release.py` for packaging changes; verify bundled links, image/source version pinning and file integrity without publishing a real Release.

- Publishable PR, main and manual CI runs also upload `TGPorterSkills.zip` as a 14-day Actions artifact after image publication. CI metadata must use the actual SHA image/digest and commit-based source URLs, link to the Actions run instead of an invented Release, and identify the bundle as a CI preview. Keep fork/Dependabot image and preview publication disabled.
