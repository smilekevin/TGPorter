# Deployment and MCP connection

Use this reference when the user requests installation, upgrades or connection
setup. Skill installation alone does not deploy TG Porter or authenticate MCP.

Use the chosen client's documented skill import and MCP connection mechanisms.
Do not assume a particular installation directory, skill invocation syntax or
credential store. If the client lacks filesystem, shell or document-fetching
capabilities needed for setup, guide the user through those steps and report
what remains unverified.

## Read the maintained deployment guide

Release and CI ZIPs include the matching deployment guide, configuration example and
Compose file.
When `release.md` and `project/DEPLOY_EN.md` are present beside this reference, read
`release.md` and `../release.json` first, then read `project/DEPLOY_EN.md` and
`project/config.yaml.example` locally. The guide is an unchanged general reference;
use the exact image, tag, download URLs and commands from the build metadata in
place of generic `latest` or `main` examples. Use the bundled
`project/docker-compose.yaml`, whose service image is set to this release.
Consult `project/DEPLOY_EN.md#parameters` for configuration meanings and defaults.
No GitHub document fetch is needed for this bundle.

Otherwise, if a matching source checkout is available, read its `DEPLOY_EN.md` and
`config.yaml.example`. Otherwise fetch the
[deployment guide](https://github.com/smilekevin/TGPorter/blob/main/DEPLOY_EN.md#prerequisites)
and [configuration example](https://github.com/smilekevin/TGPorter/blob/main/config.yaml.example).
These links follow the main branch. For an existing instance, prefer its release
tag or commit when known. If documentation cannot be read, explain that limitation
before proposing version-specific installation commands.

Keep complete commands and configuration defaults in `DEPLOY_EN.md`; follow its
Docker image, Compose or Python installation section for the user's environment.
Consult its parameter reference for required fields, defaults, naming rules,
scan settings, proxies and optional uploads before writing configuration. Keep
example values distinct from application defaults and configure optional features
only when requested.

## Establish the target and prepare

- Use the user's chosen host, installation directory and deployment method.
  Resolve missing details that affect execution; do not assume the agent's
  machine is the intended server.
- Check existing services, persistent data, port availability and the actual
  proxy configuration before creating or replacing a deployment. The example
  proxies are environment-specific.
- Reuse the main TG Porter image/service. MCP runs at `/mcp` on the existing
  `web_port` (default 5000); it needs no additional container or listener.
- Preserve `downloads/`, `data/` and `config.yaml`. Mount configuration read-only
  and data writable. Before an upgrade, follow the guide's backup and session
  migration instructions; avoid concurrent processes using the same sessions.

## Configure and connect

1. Have the user provide Telegram API credentials in protected local
   configuration and complete the documented interactive Telegram login. Keep
   verification codes, two-step passwords and session files private. The main
   download account needs access to the intended posts. A command/notification
   Bot is optional for MCP-only operation (`bot_token: ""`).
2. Enable `mcp_enabled` with a non-empty random `mcp_token`. Choose read-only mode
   if the user only needs queries; download submission requires write tools.
   Change startup configuration only as part of the authorized setup and restart
   the existing service when needed. Leave runtime JSON to the application.
3. Configure a reachable `/mcp` URL in the user's agent client using Streamable
   HTTP and the MCP-only bearer token. Store the token through the client's
   protected credential mechanism where available. Do not place it in this skill,
   committed examples, URL parameters or chat output.
4. Use the actual domain/IP and optional port in `mcp_allowed_hosts` when needed;
   localhost/loopback are already allowed. Entries have no scheme, path or
   wildcard. This allowlist does not configure the listener or client URL.
   Public access follows the guide's HTTPS reverse-proxy instructions.

TG Porter currently uses a preconfigured bearer token, without an OAuth login
page. Check that the chosen client supports this authentication and transport.
WebUI passwords/cookies and MCP tokens are separate authentication mechanisms.
Do not hard-code a universal server URL in skill metadata: each user hosts their
own instance. `127.0.0.1` is appropriate only when the connecting client runs on
the downloader host, not automatically for a remote or cloud client.

## Verify and diagnose

- Discover the selected server's tools and inspect their descriptions, schemas
  and annotations. Use those definitions to call `get_status` and report the
  observed version and available read/write capabilities. Then query
  `list_downloads`; an empty list is a valid result.
- Submit a real download only when requested. A Mock WebUI or successful HTTP
  response alone does not verify a real MCP session or Telegram download.
- For `401`, check MCP credentials; for `404`, check MCP enablement, URL and
  deployed version; for Host/Origin rejection, check the explicit allowlist and
  proxy forwarding. For unreachable endpoints, check the client/server network
  path and listener before changing credentials.
- If reconnecting or restarting is necessary, explain why and preserve existing
  sessions, tasks and partial files. A missing write tool may simply mean
  read-only mode, not a broken installation.

Finish with what was configured, what was verified and any remaining user action,
without reproducing secrets. Installing this skill and configuring MCP are
separate steps; never claim connection success before a real tool call succeeds.
