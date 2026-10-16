# Project Guidelines for Agents

These instructions apply to the entire repository. Read this file, relevant source code, and the working tree diff before making changes. Also follow any more specific AGENTS.md in the directory you are editing. Explicit user instructions for the current task take precedence.

## Branches and Pull Requests

- **Branch names must not contain `codex`, including `codex/` and `codex-`. Do not use the tool's default Codex branch prefix.**
- Name branches by purpose: `feat/<feature>`, `fix/<issue>`, `docs/<topic>`, `refactor/<module>`, `test/<scope>`, or `chore/<task>`. Use short lowercase English names with hyphens, such as `docs/project-agent-guidelines`.
- Choose a compliant name before creating a branch. Run `git branch --show-current` before each commit, push, and PR creation. If the current branch violates the naming rule, move the task's changes to a compliant new branch before committing or pushing.
- Normally start from the target repository's latest `master`. If the current checkout contains another task or uncommitted changes, use an isolated checkout. Do not overwrite, stage, or commit unrelated user changes.
- This fork's PR target is `smilekevin/TGPorter`. The local `fork` remote normally points there, while `origin` normally points to upstream `MangoIsIllegal/hermes-telegram-downloader`. Verify the actual URLs with `git remote -v` before pushing; do not infer the target from the remote name alone.
- Write all PR titles and descriptions in English.
- Keep each PR focused on one problem. Commit messages and PR titles should describe the resulting behavior; prefixes such as `feat:`, `fix:`, and `docs:` are appropriate. Describe the reason for the change, validation results, and anything not verified in the PR body.
- When the user asks for a PR, complete the relevant commit, push, and PR creation. Merge or publish a Release only within the user's authorization; a request to create a PR does not automatically authorize merging it.

## Project Purpose and Source Map

This project downloads, forwards, and listens for Telegram media. It uses Python, asyncio, Kurigram, and Flask, with Telegram Bot and WebUI entry points. Tasks, download history, and partial downloads must survive process restarts.

| Path | Responsibility |
| --- | --- |
| `media_downloader.py` | Main application, file task queue, worker pool, download execution, startup, and shutdown |
| `module/app.py` | Configuration loading/saving, Application, TaskNode, and task status types |
| `module/bot.py` | Bot commands, message entry points, task recovery, Pending consumer, forwarding, and listening |
| `module/bot_query.py` | Read-only `/query` task lists and pagination using the shared download service |
| `module/task_store.py` | Persistent tasks and daily task sequence numbers, using locks and atomic file replacement |
| `module/download_stat.py` | Download progress, history, failures, pause, and cancellation state |
| `module/parallel_download.py` | Fixed-size parts, per-file part queues, checkpoint plans, and file merging |
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
| `README.md`, `DEPLOY.md`, `docs/` | Chinese/English usage documentation, deployment guidance, and focused design/test guides |

## Design Constraints

### Scheduling and Recovery

- Persist single-message tasks as Pending before the consumer fetches their messages and hands them to the shared file task queue. Batch history scans and live listeners also feed this queue.
- `recover_tasks()` in `module/bot.py` restores task state and sequence numbers. Incomplete tasks return to Pending to avoid a burst of network requests after a restart.
- Keep the Bot heartbeat independent of progress notifications. Require a bounded Telegram response, respect Bot FLOOD_WAIT, log dispatch metadata without message contents or credentials, and cancel and await the singleton monitor during shutdown.
- Start the Pending consumer even when there are no historical tasks. Repeated startup must not create duplicate consumers. Cancel and await background tasks during shutdown.
- Tasks being fetched, queued, and actively processed must share the concurrency budget. Release reservations on rejected handoffs, exceptions, and cancellation to prevent duplicate consumption, lost tasks, and leaked capacity.
- Range-task recovery must preserve scan bounds, checkpoints, forwarding destinations, and topics. Single-message recovery must retain the owning account; use the Bot account to download and refresh Bot inbox media.
- Handle FLOOD_WAIT using shared cooldown timestamps and asynchronous waiting. Do not use blocking `time.sleep()` inside the event loop or request handlers. Bound retries and notification waits.

### Parts and Data Lifecycle

- `max_download_task` limits simultaneous files; `file_download_concurrency` controls workers per file; `download_part_size_mb` sets the fixed part size, with the final part containing the remaining bytes. Keep these controls distinct.
- Each file has its own part queue. Each worker owns an independent Telegram download connection and reuses it for its subsequent parts. Do not share download connections between workers or files. Start connections one at a time, at least one second apart. Keep at most one unconfirmed connection per file: only that worker writing its first valid block may unlock expansion; progress on older healthy connections must not unlock another worker's startup slot. On failure or cancellation, close the starting connection before releasing its slot. Retire failed connections and return their unfinished parts to the queue without stopping healthy workers. Only three consecutive failures of the last available worker exhaust the download; do not multiply this budget with file-level retries. FLOOD_WAIT blocks new connections during the shared cooldown while healthy existing connections continue. Resume automatically after cooldown, including long waits, while preserving manual pauses and cancellation.
- Bootstrap foreign non-CDN download keys on the regular DC endpoint, then open each worker's independent media connection. Cache foreign key bytes in memory on the owning client, scoped by DC/test mode and protected by per-key locks; reuse them across workers/files without sharing media sessions or transports. Cancellation must release key-creation locks without stopping unrelated files. Clear the cache after client shutdown cleanup and evict matching rejected keys without deleting newer replacements. Reuse stored home-DC keys and keep CDN endpoint selection separate. Bound auth-key creation to 60 seconds and close interrupted transports before retrying; never apply that limit to normal media transfers.
- New media staging paths use flat `temp/timestamp_guid.tmp` names derived from message time and stable account/message/media identity. Never generate a new path per retry or use channel titles in new staging paths. Reuse existing legacy checkpoints without moving active writers; keep exact staging paths in shared download records for deletion.
- Save part plans in `.parts/manifest.json`. Pauses, retries, and restarts reuse saved ranges and worker counts; configuration changes must not repartition existing downloads or download completed parts again.
- Aggregate progress across parts into one file record. Bytes restored from checkpoints do not contribute to new transfer speed. Keep one progress row per file in WebUI.
- Pauses, network failures, and process shutdown preserve checkpoints. Explicit user cancellation first stops writers and closes connections; Web/API/MCP deletion removes that file's staging data and `.parts/` only with `delete_file=true` (Bot cancellation keeps its cleanup default). Do not affect other downloads or completed files.
- Changes to recovery or merging must preserve file identity, range validation, integrity checks, and compatibility with existing checkpoint formats.
- Preserve locking and atomic replacement when writing tasks, counters, and part plans. Do not bypass the storage APIs to modify shared JSON files directly.

### WebUI and Async Boundaries

- Use the existing Flask templates and native frontend. Do not introduce a frontend framework or build pipeline for a local UI change.
- Flask request threads and the asyncio download loop have a thread boundary. Follow the existing thread-safe coroutine submission approach rather than manipulating main-loop tasks directly from a request thread.
- A non-empty `web_login_secret` protects pages and Web APIs using password-login sessions only. State changes require the session CSRF token. Never add a Bearer-token bypass to Web APIs, dashboard, login or logout. An empty password preserves the existing public WebUI/API mode independently of MCP. Login offers an unchecked remember-me option: a seven-day persistent cookie when selected, otherwise a browser-session cookie. Service restart or password change invalidates both.
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
- Write application file logs only to `log/tdl.log` with the existing rotation, retention and configured level. Do not add a duplicate download log sink.
- Use the existing Loguru logging setup for important lifecycle events. Preserve connection-open and connection-close logs used by download acceptance checks.
- Configuration changes must update `module/app.py`, `config.yaml.example`, and relevant documentation together. Bot command or Web API changes must be reflected in both README languages.
- Filtering uses Lark in `module/filter.py` and does not generate parser tables. Do not edit or depend on legacy `module/parsetab.py` files. Preserve saved-expression precedence, byte/date literals, eager boolean evaluation and missing-value behavior when changing the grammar; verify with `tests/test_filter.py`.
- Never commit real `config.yaml`, `data.yaml`, sessions, Bot Tokens, API credentials, runtime logs, downloaded files, or checkpoints. Keep these out of debug output and PR descriptions as well.

## Documentation Synchronization

- For future project changes, update `README.md` and `DEPLOY.md` together in the same change set. Keep both documents consistent with the final behavior, configuration and supported usage; do not update only one of them.
- `README.md` owns the project overview, features, architecture, Bot commands, Web API and short startup summaries. Deployment entries must link to `DEPLOY.md`.
- `DEPLOY.md` owns prerequisites, credentials, complete installation and login steps, configuration examples and defaults, parallel-download settings and checkpoints, image updates, local builds, persistence, proxies and troubleshooting.
- Maintain both Chinese and English sections when updating either document. Use the display name `TG Porter`, repository name `TGPorter`, and lowercase image/service/container name `tgporter`.
- Keep summaries and links aligned with the detailed guide instead of duplicating complete commands, configuration blocks or persistence tables in README.
- Maintain `AGENTS.md` when entry points, authentication, dependency management or deployment conventions change. `docs/mcp.md` owns the MCP tool reference; complete MCP Token and setup instructions belong in both language sections of `DEPLOY.md`.
- Before finishing, check relative links and heading anchors, run `git diff --check`, and verify commands, image tags, configuration defaults and paths against `config.yaml.example`, `docker-compose.yaml`, the workflows and relevant source code.

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

# Real application; requires local configuration and initial interactive login.
python media_downloader.py
```

- Follow `docs/parallel-download-testing.md` for real download acceptance checks. Use isolated directories and compare SHA-256 values. File length, Mock progress, or part counts alone do not prove file integrity or network speed improvements.
- Distinguish static checks, offline tests, published-image Mock deployments, and real Telegram acceptance checks in reports. Clearly identify checks that were not run.

## Docker and Publishing

- The project display name is `TG Porter` and the repository is `TGPorter`. Docker image, Compose service and container names use lowercase `tgporter`. Preserve upstream project names and attribution links.
- The image repository is `ghcr.io/smilekevin/tgporter`. Docker uses a multi-stage Alpine build with `python media_downloader.py` as the runtime command.
- The main image includes MCP. Enable it in `config.yaml`; reuse port 5000 and the existing Compose service and mounts. No separate MCP image, sidecar, optional dependency file or port mapping is required. Main-image CI and Release builds include the integrated MCP dependencies automatically.
- Persist `downloads/`, `log/`, `sessions/`, `temp/`, `config.yaml`, and `data.yaml`. Configuration bind-mount sources must be files. Removing a container must not delete this host data.
- Before deployment tests, check host ports and proxy reachability. Do not assume the example proxy address works on the current machine. Keep test overrides and temporary data in an isolated directory and out of the PR.
- `.github/workflows/tests.yml` runs tests and amd64/arm64 builds for PRs, `master` pushes, and manual runs. Publishable CI runs push only `sha-<full-commit-sha>` tags and do not update `latest`.
- `.github/workflows/docker-build.yml` publishes version, SHA, and `latest` tags when a GitHub Release is published, then updates the Release notes with image references. Do not make ordinary PRs publish or overwrite `latest`.
- Release builds pass the Git tag as Docker build arg `APP_VERSION`; CI builds use `sha-<full-commit-sha>`. The runtime image preserves it in its environment and OCI version label. Keep `utils.__version__` as the shared version export: environment, then local Git description, then `unknown`. Compose must not override the baked version with `latest`; local builds must pass their source version explicitly.
- For workflow changes, run the tests in `tests/test_docker_workflow.py` and check tag rules, permissions, digests, Summary output, and repeatable Release notes updates.
