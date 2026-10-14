# 项目协作规范

本文件适用于整个仓库。开始工作时先阅读本文件、相关代码和工作区差异；子目录如有更具体的 `AGENTS.md`，同时遵守其约定。用户在当前任务中的明确要求优先。

## Git 分支与 PR

- **分支名不得包含 `codex`，包括 `codex/` 和 `codex-`。不要使用工具默认生成的 Codex 分支前缀。**
- 按改动目的命名：`feat/<功能>`、`fix/<问题>`、`docs/<主题>`、`refactor/<模块>`、`test/<范围>`、`chore/<事项>`；主题使用简短的小写英文和连字符。例如 `docs/project-agent-guidelines`。
- 创建分支前确定名称；提交、推送和创建 PR 前，分别运行 `git branch --show-current` 核对。若当前分支不符合规则，先将本次改动放到符合规则的新分支，再提交或推送。
- 通常从目标仓库最新的 `master` 创建分支。已有工作区有其他任务或未提交改动时，使用独立工作区，不覆盖、暂存或提交用户的无关修改。
- 本 fork 的 PR 目标是 `smilekevin/telegram-media-downloader`。本地 `fork` 通常指向该仓库，`origin` 通常指向上游 `MangoIsIllegal/hermes-telegram-downloader`；推送前用 `git remote -v` 核对实际地址，不根据远程名猜测目标。
- 一个 PR 聚焦一个问题。标题和提交信息说明最终行为，可使用 `feat:`、`fix:`、`docs:` 等前缀；PR 描述说明改动原因、验证结果和未验证的部分。
- 用户要求发 PR 时，完成相关提交、推送和 PR 创建；合并或发布 Release 按用户授权执行，不将创建 PR 视为自动授权合并。

## 项目定位和文件导航

这是 Telegram 媒体下载、转发和实时监听工具，使用 Python、asyncio、Pyrogram 和 Flask，提供 Telegram Bot 和 WebUI 两个交互入口。任务、下载历史和断点需要跨进程重启保留。

| 路径 | 职责 |
| --- | --- |
| `media_downloader.py` | 主程序、文件任务队列、worker 池、下载执行、启动和关闭流程 |
| `module/app.py` | 配置读取和保存、Application、TaskNode 及任务状态类型 |
| `module/bot.py` | Bot 命令、消息入口、任务恢复、Pending 消费者、转发和监听 |
| `module/task_store.py` | 任务和每日任务编号持久化，使用锁和原子文件替换 |
| `module/download_stat.py` | 下载进度、历史、失败记录、暂停和取消状态 |
| `module/parallel_download.py` | 固定大小切片、切片队列、断点计划和文件合并 |
| `module/download_connection.py` | 每个下载 worker 的独立连接及资源清理 |
| `module/pyrogram_extension.py` | Telegram 下载/上传适配、文件引用刷新、限速状态和 SDK 扩展 |
| `module/web.py` | 真实 Flask 页面、Web API、登录和 CSRF 校验 |
| `module/templates/`、`module/static/` | WebUI 模板、原生 HTML/CSS/JS 和静态资源 |
| `module/filter.py`、`module/language.py`、`module/cloud_drive.py` | 消息过滤、多语言和可选云盘上传 |
| `run_local.py` | 独立的 Mock WebUI，仅用于界面开发，不执行真实下载 |
| `tests/` | 无需 Telegram 账号的 unittest 回归测试 |
| `tools/benchmark_download.py` | 真实 Telegram 下载测速及中断续传验收工具 |
| `README.md`、`DEPLOY.md`、`docs/` | 中英文使用说明、部署指南和专项设计/验收文档 |

## 设计约束

### 调度与恢复

- 单条任务先持久化为 Pending，再由消费者获取消息并交给共享文件任务队列；批量历史扫描和监听也使用该队列。
- `module/bot.py` 的 `recover_tasks()` 恢复状态和任务编号；未完成任务回到 Pending，避免重启后一次性恢复大量网络请求。
- 即使没有历史任务，也要启动 Pending 消费循环；重复启动不得产生多个消费者，关闭时取消并等待后台任务结束。
- 消费中、已入队和实际运行的任务必须统一计入并发限制。拒绝入队、异常或取消后释放预约，防止重复消费、漏任务和并发计数泄漏。
- 区间任务恢复需保留扫描范围、进度、转发目标和 topic；单消息任务恢复需保留消息所属账号，Bot 收件箱媒体用 Bot 账号下载及刷新引用。
- FLOOD_WAIT 使用共享冷却时间和异步等待，不在事件循环或请求处理中使用阻塞式 `time.sleep()`；重试和通知等待应有边界。

### 分片与数据生命周期

- `max_download_task` 限制同时下载的文件数；`file_download_concurrency` 是单文件 worker 数；`download_part_size_mb` 是固定切片大小，尾片使用剩余字节。不要将三个参数混为同一个并发限制。
- 每个文件使用自己的切片队列。每个 worker 拥有独立的 Telegram 下载连接，并用于其后续切片；不同 worker 和文件不共享下载连接。
- 切片计划保存在 `.parts/manifest.json`。暂停、重试和重启沿用已有范围和 worker 数，不因配置变化重新分片或重复下载已完成数据。
- 进度按文件汇总各片的已下载字节；恢复的已有字节不计入新传输速度，WebUI 保持一个文件一条进度记录。
- 暂停、断网和程序退出保留断点。用户明确取消下载时，先结束写入并关闭连接，再清理该文件的临时数据和 `.parts/`；不影响其他下载和已完成文件。
- 涉及恢复或合并的修改要保持文件身份、范围和完整性校验，并考虑旧断点格式的兼容性。
- 持久化任务、计数器和分片计划时保留锁与原子替换策略，不绕过现有存储接口直接修改共享 JSON。

### WebUI 与异步边界

- WebUI 使用现有 Flask 模板和原生前端，不为局部改动引入新前端框架或构建链。
- Flask 请求线程与 asyncio 下载循环有线程边界；需要调度协程时沿用线程安全提交方式，不直接在请求线程操作主循环任务。
- `web_login_secret` 非空时，页面和 API 受登录保护，状态变更校验 CSRF；新增接口遵守同样规则。空值关闭登录是现有配置行为。
- 修改页面接口时同时核对前端请求、返回结构和实际 Flask 路由；Mock 页面不能代替真实接口验证。

## 实现规范

- 优先最小改动，沿用所在模块的风格、四空格缩进和现有命名；不顺带做大范围格式化、依赖升级或架构重写。
- Python 标识符以英文为主，公共函数和复杂异步流程写清意图；注释解释状态、并发和兼容性原因，避免只重复代码。
- CI 和 Docker 使用 Python 3.11；依赖通过 `requirements.txt` 管理。Pyrogram 来自该文件指定的 fork，SDK 扩展不能按官方发行版假定行为。
- 使用现有 Loguru 日志入口记录关键生命周期，修改下载连接行为时保留可用于验收的打开/关闭日志。
- 改配置项时同步 `module/app.py`、`config.yaml.example` 和相关说明；改 Bot 命令或 Web API 时核对 README 中英文两部分。
- `module/parsetab.py` 是过滤解析器生成文件，不将其当作主要实现入口；过滤逻辑优先修改 `module/filter.py`。
- 不提交真实 `config.yaml`、`data.yaml`、session、Bot Token、API 凭据、运行日志、下载文件及断点。调试输出和 PR 描述也不包含这些数据。

## 开发与验证

从仓库根目录运行，使用隔离 Python 环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

- 只改文档时核对链接、示例和 `git diff --check`；修改 Compose 配置时增加 `docker compose -f docker-compose.yaml config --quiet`。
- 修改运行逻辑时先运行对应的现有测试，针对行为变化补充有意义的回归用例；涉及跨模块调度、恢复或分片行为时运行完整离线回归。
- 测试使用 `unittest`、`IsolatedAsyncioTestCase`、Mock 和临时目录，避免连接 Telegram 或写入真实任务/session 数据。测试中更换模块状态后及时恢复。

```bash
# 完整离线回归；工作流脚本测试还需要 Node.js 24，CI 会自动安装
python -B -m unittest discover -s tests -v

# 分片与连接专项回归
python -B -m unittest discover -s tests -p 'test_parallel*.py' -v
python -B -m unittest discover -s tests -p 'test_download_connections.py' -v

# Mock 界面开发
python run_local.py

# 真实应用；需要本地配置和首次交互登录
python media_downloader.py
```

- 真实下载验收遵循 `docs/parallel-download-testing.md`，使用独立目录并比较 SHA-256；不要仅凭文件长度、Mock 进度或切片数声称文件正确或网络加速。
- 报告时区分静态检查、离线测试、发布镜像的 Mock 部署和真实 Telegram 验收；未执行的检查明确说明。

## Docker 与发布

- 项目和镜像名称为 `telegram-media-downloader`；部署的 Compose 服务名和容器名为 **`telegam-media-downloader`**，按用户指定拼写保留。不要把该拼写替换进镜像、仓库地址或上游链接。
- 镜像地址是 `ghcr.io/smilekevin/telegram-media-downloader`。Docker 使用多阶段 Alpine 构建，运行入口为 `python media_downloader.py`。
- 持久化挂载包括 `downloads/`、`log/`、`sessions/`、`temp/` 以及 `config.yaml`、`data.yaml`；配置文件挂载源必须是文件。删除容器不应删除这些宿主机数据。
- 部署测试先核对宿主机端口和代理可达性，不假定示例中的代理地址适用于当前机器；测试覆盖文件和临时数据放在独立目录，不混入 PR。
- `.github/workflows/tests.yml` 在 PR、`master` 推送或手动触发时运行测试及 amd64/arm64 构建；允许发布的 CI 仅推送 `sha-<完整提交号>`，不更新 `latest`。
- `.github/workflows/docker-build.yml` 在 GitHub Release 发布时推送版本、SHA 和 `latest` 标签，并更新 Release notes 的镜像信息。不要为普通 PR 改成发布或覆盖 `latest`。
- 修改工作流时运行 `tests/test_docker_workflow.py` 对应的测试，核对标签规则、权限、digest、Summary 和 Release notes 的重复执行行为。
