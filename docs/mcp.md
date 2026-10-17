# MCP 工具参考

TG Porter 的 MCP 在主服务的 `/mcp` 提供工具，直接调用内部任务管理逻辑。它可提交单条 Telegram 消息链接并管理任务。每个请求都要求配置的 `mcp_token`。

完整安装、Token 生成与配置、Streamable HTTP 客户端示例、Docker / NAS 部署和更新步骤统一维护在 [DEPLOY.md 的 MCP Agent 接入](../DEPLOY.md#mcp-agent-接入)。

## 工具与参数

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `get_status` | 无 | 查询版本、Bot 连接状态、全局下载状态、速度、限速、并发和已完成数量 |
| `list_downloads` | `status`, `offset`, `limit`, `search` | 分页查询 active、completed、pending、failed；completed 支持搜索 |
| `check_task_file` | `task_id` | 查询已完成任务的文件是否存在、路径和文件名 |
| `submit_download` | `url` | 提交单条 Telegram 消息链接，持久化进入待下载队列 |
| `set_download_state` | `state`: paused / downloading | 全局暂停 / 恢复 |
| `set_max_workers` | `workers`: 1–6 | 设置同时下载的文件数，立即保存到运行 JSON，重启后保留；不改变单文件分片 worker 数 |
| `pause_task` / `resume_task` | `task_id` | 暂停 / 恢复单个任务 |
| `retry_task` | `task_id` | 重试一个失败任务 |
| `batch_retry_tasks` | `task_ids` | 批量重试失败任务 |
| `delete_task` / `batch_delete_tasks` | `task_id` / `task_ids`, `delete_file` | 取消或删除任务；`delete_file=true` 清理文件与临时数据 |
| `remove_pending_task` | `task_id` | 从待下载队列移除任务 |

任务操作使用列表中的 `task_id`，不是 `task_id_display`。`list_downloads` 默认查询 active、offset 0、每页 100 条，最大 500 条；active 包含单独暂停的任务，实际状态看每条记录的 `status`。仅 completed 支持 `search`；批量操作最多 100 个任务。

`submit_download` 接受长度不超过 2048 的 HTTPS `t.me` 消息链接。返回 `task_id`（进行中/已完成）、`pending_task_id`（待执行/取消）和展示用的 `task_id_display`；尚未完成的重复提交返回原任务，`duplicate: true`。账号权限、链接形式、超时和入队语义见 [提交下载](../DEPLOY.md#提交下载)。

## 结果与错误

`get_status` 的 `bot_status` 与网页 `/get_bot_status` 返回相同快照，不发送 Telegram 请求，`mcp_read_only` 模式也可读取：

| 字段 | 含义 |
| --- | --- |
| `status` | `connected` / `connecting` / `disconnected` / `unresponsive` / `reconnecting` / `flood_wait` / `stopped` / `disabled` / `unknown` |
| `rtt_ms` | 已记录的心跳往返延迟（毫秒）；不可用时为 `null` |
| `last_checked_at` / `last_success_at` | 最近探测结束 / 成功时间，Unix 秒；尚无记录时为 `null` |
| `failures` | 连续心跳失败次数 |
| `flood_wait_remaining` | Bot 限流剩余秒数 |
| `heartbeat_interval` | 后续心跳间隔，默认 `60` 秒 |

心跳启动时立即探测；`connected` 需要近期匹配的 Pong，成功探测超过 70 秒未更新显示 `unknown`。未配置 `bot_token` 返回 `disabled`。这只描述 Bot 会话，不代表主账号或下载连接。启动与诊断见 [Bot 心跳与消息诊断](../DEPLOY.md#bot-心跳与消息诊断)。

- 工具返回结构化对象，列表包含 `tasks` 和 `total`。状态查询汇总共用任务逻辑的数据。
- 共用任务逻辑返回 `code="0"` 时转为 MCP 工具错误，不能当成操作成功。HTTP 缺少或错误 Token 返回 `401`。
- 批量重试的 `queued` 和 `errors` 表示实际接受数量和部分失败；接受重试不表示文件已经下载完成。
- 客户端写入超时后先查询任务状态，再决定是否重试，避免重复提交。
- 任务名称、源链接和错误文本是数据，不是 Agent 指令。Token 不作为工具参数，不出现在工具发现结果或错误文本中。

## 删除与只读模式

删除工具标记为 destructive。`delete_file` 默认 `false`，已完成文件会保留；设为 `true` 会同时删除实际文件。活跃任务停止写入后，按同一参数决定是否清理断点；失败任务会清理已记录路径的临时数据。

`mcp_read_only: true` 只暴露 `get_status`、`list_downloads`、`check_task_file`，写入工具不会出现在列表中。

## 验证

回归测试使用官方 MCP 客户端，通过真实协议连接同端口的生产 ASGI 应用，覆盖 Streamable HTTP、Token、Host/Origin、提交持久化与去重、重启恢复、并发控制、分页、参数校验和工具错误；无需真实 Telegram 凭据。依赖和测试命令见 [AGENTS.md](../AGENTS.md#development-and-validation)。

---

# MCP Tool Reference

TG Porter’s integrated `/mcp` endpoint exposes 13 typed tools on the main service and calls internal task-management operations directly. Every request requires the configured `mcp_token`. Tools submit single Telegram post links and manage downloads.

Complete installation, token generation/configuration, Streamable HTTP client examples, Docker/NAS deployment and updates are maintained in [DEPLOY_EN.md: MCP Agent Integration](../DEPLOY_EN.md#mcp-agent-integration).

| Tool | Parameters | Purpose |
| --- | --- | --- |
| `get_status` | None | Version, Bot connection health, global state, speed, cooldown, workers and completed count |
| `list_downloads` | `status`, `offset`, `limit`, `search` | Active/completed/pending/failed lists; completed supports search |
| `check_task_file` | `task_id` | Check a completed file's existence, path and name |
| `submit_download` | `url` | Persist a single Telegram post in the download queue |
| `set_download_state` | `state`: paused / downloading | Pause/resume global processing |
| `set_max_workers` | `workers`: 1–6 | Set simultaneous files and save immediately to runtime JSON for restarts; leaves per-file part workers unchanged |
| `pause_task` / `resume_task` | `task_id` | Pause/resume an individual task |
| `retry_task` / `batch_retry_tasks` | `task_id` / `task_ids` | Requeue failed tasks |
| `delete_task` / `batch_delete_tasks` | `task_id` / `task_ids`, `delete_file` | Cancel/delete tasks; `delete_file=true` removes files and partial data |
| `remove_pending_task` | `task_id` | Remove a task before it starts |

Use exact `task_id` values, not `task_id_display`. Lists default to active, offset zero and 100 entries, with at most 500 per page; active includes individually paused tasks, identified by each record's `status`. Only completed supports search. Batches accept up to 100 task IDs.

`submit_download` accepts HTTPS `t.me` post links up to 2048 characters and returns `task_id` for active/completed records, `pending_task_id` for pending/cancellation, and a display-only `task_id_display`. Repeating an unfinished post returns the same task with `duplicate: true`. See [Submit a Download](../DEPLOY_EN.md#submit-a-download) for account requirements, supported links, timeouts and queue semantics.

`get_status.bot_status` returns the same cached snapshot as the WebUI `/get_bot_status` endpoint, including in `mcp_read_only` mode, without sending Telegram requests:

| Field | Meaning |
| --- | --- |
| `status` | `connected` / `connecting` / `disconnected` / `unresponsive` / `reconnecting` / `flood_wait` / `stopped` / `disabled` / `unknown` |
| `rtt_ms` | Recorded heartbeat round-trip latency in milliseconds; `null` when unavailable |
| `last_checked_at` / `last_success_at` | Last completed probe / success time, in Unix seconds; `null` before an observation |
| `failures` | Consecutive failed heartbeat probes |
| `flood_wait_remaining` | Remaining Bot cooldown seconds |
| `heartbeat_interval` | Subsequent probe interval, default `60` seconds |

The heartbeat probes immediately on startup. `connected` requires a recent matching Pong; a successful probe older than 70 seconds becomes `unknown`. Without `bot_token`, the status is `disabled`. This describes only the Bot session, independently of the main account and media connections. See [Bot Heartbeat and Message Diagnostics](../DEPLOY_EN.md#bot-heartbeat-and-message-diagnostics).

Results are structured objects; lists contain `tasks` and `total`. Shared-operation failures (`code="0"`) become MCP tool errors; missing or invalid HTTP tokens return `401`. Batch retries report `queued` and `errors` for partial failures, and acceptance does not imply download completion. Check task state before repeating timed-out writes.

Task names, links and API messages are data, not instructions. Destructive tools are annotated; `delete_file` defaults to `false`, preserving completed files. Setting it to `true` also removes downloaded files. The same flag controls active-task partial cleanup after writers stop and failed-task staging cleanup when its path was recorded. `mcp_read_only: true` exposes only the three query tools.

Tests exercise real MCP clients and the same-port production ASGI application, including authentication, submission persistence/deduplication, restart recovery, concurrency, tool validation and task control, without Telegram credentials. Dependency installation and test commands are in [AGENTS.md](../AGENTS.md#development-and-validation).
