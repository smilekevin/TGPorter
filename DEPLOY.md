# Telegram Media Downloader - 部署指南 / Deployment Guide

安装、配置、登录和运维步骤统一维护在本文件。功能介绍、Bot 命令、Web API 和架构见 [README 中文说明](./README.md#功能特性)。

Installation, configuration, login and operations are maintained here. English instructions start at [Prerequisites](#prerequisites). See the [README](./README.md#features) for features, Bot commands, Web APIs and architecture.

中文：[前置条件](#前置条件) · [凭据准备](#部署前准备api-凭据与-bot-token) · [Docker 镜像](#docker-镜像部署) · [Compose](#docker-compose-部署) · [手动安装](#手动安装) · [本地开发](#本地开发模式) · [配置说明](#配置说明) · [WebUI 登录](#webui-登录) · [MCP](#mcp-agent-接入) · [运维](#docker-管理) · [持久化](#docker-数据持久化) · [代理](#代理配置) · [故障排查](#故障排查)

English: [Prerequisites](#prerequisites) · [Credentials](#before-you-deploy-api-credentials-and-bot-token) · [Docker Image](#docker-image-deployment) · [Compose](#docker-compose-deployment) · [Manual Install](#manual-install) · [Local Dev](#local-dev-mode) · [Configuration](#configuration) · [WebUI Login](#webui-login) · [MCP](#mcp-agent-integration) · [Operations](#docker-management) · [Persistence](#docker-data-persistence) · [Proxy](#proxy-configuration) · [Troubleshooting](#troubleshooting)

---

## 前置条件

Telegram SDK 使用 `requirements.txt` 中固定版本的 Kurigram（仍通过 `pyrogram` 导入）。不要在同一虚拟环境中混装 Pyrogram、Pyrofork 和 Kurigram；从旧版本手动安装迁移时，请重新创建虚拟环境，再安装完整依赖。

主服务通过 `requirements.txt` 安装依赖，直接使用的 Starlette、Uvicorn、Pydantic 固定为已验证版本。过滤表达式由固定版本的 Lark 解析，不再使用 PLY 或生成 `parsetab.py`。不再需要 Flask-Login、Requests 或 PyCryptodome；从旧环境升级时重新创建虚拟环境，可避免残留包掩盖缺失依赖。

首次使用此版本前，先停止服务并备份 `sessions/`、`config.yaml`、`data.yaml`、`downloads/` 和 `temp/`。Kurigram 会原地升级旧 Session 数据库；回滚到旧 SDK 时，应停服并恢复升级前的 Session 备份。任务与 `.parts/` 检查点格式保持不变；离线迁移测试不替代真实 Telegram 下载验收。

1. Docker 镜像部署需要 Docker；Compose 部署还需 Docker Compose。手动安装需要 Python 3.14 和 pip。
2. 有 Telegram API 凭证（api_id + api_hash），从 https://my.telegram.org 获取
3. 已创建 Bot Token（通过 @BotFather）

本 fork 提供自动构建的预构建镜像：`ghcr.io/smilekevin/telegram-media-downloader:latest`，支持 amd64/arm64。[CI 验证镜像](https://github.com/smilekevin/telegram-media-downloader/actions/workflows/tests.yml) / [Build Release Docker Image](https://github.com/smilekevin/telegram-media-downloader/actions/workflows/docker-build.yml)。上游 MangoIsIllegal 项目不提供预构建镜像。

项目、仓库和 GHCR 镜像统一使用 **Telegram Media Downloader** 名称；Compose 服务/容器名使用 `telegam-media-downloader`。

镜像发布规则：本仓库 PR、合并/推送 master 或手动运行 CI 后，测试通过即发布 `:sha-完整提交号`，Summary 提供可复制的拉取命令和 Compose 配置，不更新 `latest`。**Build Release Docker Image** 只在发布 GitHub Release 后运行，发布同名版本镜像并更新 `:latest`，例如 `v1.0.1` 对应 `:v1.0.1`。不再定时构建。README 的 Version 徽章自动跟随最新正式 GitHub Release 标签，Python 徽章自动读取 `.github/workflows/tests.yml` 中的 `python-version`，徽章缓存刷新后生效。

Release 镜像推送成功后，工作流会自动在对应 Release notes 中补充版本拉取命令、digest 地址和支持的平台。原有说明会保留，重新运行时更新同一区块。

本地 Mock 开发模式无需 Telegram 账号或凭证。下文使用 `docker-compose`；使用 Compose 插件的环境可替换为 `docker compose`。

## 快速开始

Docker 部署使用本 fork 发布的 `ghcr.io/smilekevin/telegram-media-downloader:latest`，由 GitHub Release 发布流程更新。

### 部署前准备：API 凭据与 Bot Token

以下说明适用于镜像、Docker Compose 和手动部署；本地 Mock 开发模式无需 Telegram 登录。

**获取 `api_id` 和 `api_hash`**

1. 使用已有 Telegram 账号的手机号登录 [my.telegram.org](https://my.telegram.org)。
2. 打开 [API development tools](https://my.telegram.org/apps)，填写应用信息并创建应用。
3. 将页面显示的 `api_id` 和 `api_hash` 填入本地 `config.yaml`。手机号登录和 Bot Token 登录都需要这两个字段，请妥善保存，不要提交到公开仓库。详见 [Telegram 官方说明](https://core.telegram.org/api/obtaining_api_id)。

**获取 Bot Token**

1. 在 Telegram 中打开 [@BotFather](https://t.me/BotFather)，发送 `/newbot`。
2. 按提示设置 Bot 名称和用户名，创建成功后复制 BotFather 返回的完整 Token。
3. 将 Token 填入 `config.yaml` 的 `bot_token`，用于命令交互和通知 Bot。详见 [Telegram 官方说明](https://core.telegram.org/bots/features#botfather)。

主下载客户端可以选择手机号或 Bot Token 登录；仅填写配置中的 `bot_token` 不会代替主下载客户端的首次登录。使用 Bot 身份下载时，下载范围取决于该 Bot 对目标消息的访问权限。各部署方式的首次登录和 session 丢失后的重新登录步骤见下文，两种登录方式的背景说明见 [Kurigram 文档](https://docs.kurigram.icu/start/auth)。

### Docker 镜像部署

服务器或 NAS 已安装 Docker 后，可以直接运行本 fork 的预构建镜像。

1. **准备配置和数据目录**：从本仓库下载 [config.yaml.example](./config.yaml.example)，复制为 `config.yaml`，并创建运行数据文件和持久化目录。下面的命令会保留已有的 `config.yaml` 和 `data.yaml`。

```bash
mkdir -p telegram-media-downloader
cd telegram-media-downloader
mkdir -p downloads log sessions temp

curl -fL https://raw.githubusercontent.com/smilekevin/telegram-media-downloader/master/config.yaml.example \
  -o config.yaml.example
[ -f config.yaml ] || cp config.yaml.example config.yaml
[ -f data.yaml ] || printf '{}\n' > data.yaml
```

2. **填写配置**：使用文本编辑器打开 `config.yaml`，填写 `api_id`、`api_hash` 和 `bot_token`，并将 `web_login_secret` 改为自己的密码，按[配置说明](#配置说明)设置媒体类型、权限和并发数。模板自带示例代理，请按自己的环境修改 `proxy`；无需代理时设为 `proxy: {}`。使用下面的挂载和端口映射时，保留 `save_path: /app/downloads`、`web_host: 0.0.0.0` 和 `web_port: 5000`。

3. **拉取镜像并首次启动**：挂载配置、运行数据、下载、日志、session 和临时目录，以交互模式完成 Telegram 登录。

```bash
docker pull ghcr.io/smilekevin/telegram-media-downloader:latest

docker run -it \
  --name telegam-media-downloader \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/config.yaml:/app/config.yaml" \
  -v "$PWD/data.yaml:/app/data.yaml" \
  -v "$PWD/downloads:/app/downloads" \
  -v "$PWD/log:/app/log" \
  -v "$PWD/sessions:/app/sessions" \
  -v "$PWD/temp:/app/temp" \
  ghcr.io/smilekevin/telegram-media-downloader:latest
```

4. **首次登录（或需要重新登录时，比如 session 失效）**：**此处登录的账号为实际下载用户**，程序以该 Telegram 用户或 Bot 账号的身份访问并下载消息，下载权限取决于该账号。在终端出现 `Enter phone number or bot token:` 时，选择一种方式输入，并在确认提示时输入 `y`：

- **手机号**：输入带国家区号的手机号（例如 `+8613812345678`），再输入 Telegram 发送的验证码；启用两步验证时还需输入密码。
- **Bot Token**：输入在[部署前准备](#部署前准备api-凭据与-bot-token)中获取的完整 Token，无需手机号验证码。

登录成功后，按 `Ctrl+C` 停止当前前台运行。session 保存在挂载的 `sessions/`，保留有效 session 后重启无需再次登录。任务与下载历史保存在 `log/`；各挂载目录的用途见[Docker 数据持久化](#docker-数据持久化)。

5. **后台启动并访问 WebUI**：执行以下命令，停止容器后用 [`docker start`](https://docs.docker.com/reference/cli/docker/container/start/) 在后台启动已有容器：

```bash
docker stop telegam-media-downloader
docker start telegam-media-downloader
```

启动后，在浏览器中访问 `http://服务器IP:5000`，使用配置的密码登录，见 [WebUI 登录](#webui-登录)。

### Docker Compose 部署

使用仓库中提供的 `docker-compose.yaml` 部署：

```bash
git clone https://github.com/smilekevin/telegram-media-downloader.git
cd telegram-media-downloader

# 创建配置和持久化目录，保留已有配置与数据
[ -f config.yaml ] || cp config.yaml.example config.yaml
[ -f data.yaml ] || printf '{}\n' > data.yaml
mkdir -p downloads log sessions temp
# 编辑 config.yaml，填入 api_id / api_hash / bot_token，并修改 WebUI 密码
```

配置文件和 `data.yaml` 的挂载源必须是文件，提前创建可避免 Docker 将缺失文件创建成目录。仓库 Compose 默认启用了示例代理环境变量，请按实际环境修改；无需代理时删除或注释这两项。下面是无需环境变量代理的完整配置示例：

```yaml
services:
  telegam-media-downloader:
    image: ghcr.io/smilekevin/telegram-media-downloader:latest
    container_name: telegam-media-downloader
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
      # 下载目录 (修改为你的实际路径)
      - "./downloads:/app/downloads/"
      # 配置文件
      - "./config.yaml:/app/config.yaml"
      - "./data.yaml:/app/data.yaml"
      # 日志
      - "./log:/app/log/"
      # Telegram session
      - "./sessions:/app/sessions"
      # 临时文件
      - "./temp:/app/temp"
    restart: unless-stopped
```

下载目录可改为 `/your/download/path:/app/downloads/`；`config.yaml` 中的 `save_path` 仍使用容器路径 `/app/downloads`。修改 `web_port` 时同步调整 Compose 端口映射。预构建镜像只需配置和数据挂载，避免用本地源码挂载覆盖镜像内代码。

**首次登录（或需要重新登录时，比如 session 失效）**：**此处登录的账号为实际下载用户**，程序以该 Telegram 用户或 Bot 账号的身份访问并下载消息，下载权限取决于该账号。使用交互终端启动一次性容器；如果后台服务正在运行，先停止它：

```bash
# 拉取已发布的 Release 镜像
docker-compose pull
docker-compose stop telegam-media-downloader
docker-compose run --rm telegam-media-downloader
```

出现 `Enter phone number or bot token:` 时，输入带国家区号的手机号或完整 Bot Token，并在确认提示时输入 `y`。手机号登录还需输入验证码，以及两步验证密码（如已启用）；Bot Token 登录无需手机号验证码。登录成功后按 `Ctrl+C` 停止此次前台运行，再启动后台服务：

```bash
docker-compose up -d

# 查看日志
docker-compose logs -f
```

Compose 将 session 保存在宿主机的 `sessions/`；`--rm` 只删除一次性容器，不会删除挂载目录。保留有效 session 后，后续启动无需再次登录。

### 手动安装

```bash
git clone https://github.com/smilekevin/telegram-media-downloader.git
cd telegram-media-downloader
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

[ -f config.yaml ] || cp config.yaml.example config.yaml
# 按配置说明填写凭证和密码，将 save_path 改为本机路径（例如 ./downloads）
# 按实际环境修改 proxy；无需代理时设为 proxy: {}
```

**首次登录（或需要重新登录时，比如 session 失效）**：**此处登录的账号为实际下载用户**，程序以该 Telegram 用户或 Bot 账号的身份访问并下载消息，下载权限取决于该账号。如果下载程序正在运行，先停止它，再从项目目录在交互终端中执行：

```bash
python media_downloader.py
```

出现 `Enter phone number or bot token:` 时，输入带国家区号的手机号或完整 Bot Token，并在确认提示时输入 `y`。手机号登录还需输入验证码，以及两步验证密码（如已启用）；Bot Token 登录无需手机号验证码。登录成功后程序继续运行，session 保存在项目的 `sessions/`。保留有效 session 后，后续启动无需再次登录。

### 本地开发模式

在仓库根目录使用 Python 3.14 虚拟环境；只需 Flask 和 Werkzeug，无需安装完整下载依赖或配置 Telegram 凭证。

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install Flask==3.1.3 Werkzeug==3.1.9
# 无需 Telegram 账号，Mock 数据启动 WebUI
python run_local.py
# 访问 http://localhost:5000
```

---

## 配置说明

下载过滤表达式保留比较、算术、`and` / `or`、`&&` / `||`、括号、单引号字符串、`r'...'` 正则和 `B` / `KB` / `MB` / `GB` / `TB` 单位。日期需要完整时间（如 `2024-01-01 00:00:00`）。正则比较执行完整匹配，可以放在比较式任一侧；非法字符不再被跳过，而是返回语法错误。缺失字段和运算优先级沿用原有规则。

### config.yaml

示例配置可按需调整。部署前修改 `web_login_secret`；示例中单文件并发为 `4`，未配置时程序默认值为 `1`。模板自带代理地址，无需代理时设置 `proxy: {}`。

```yaml
# Telegram API 凭证（必填）
api_id: your_api_id
api_hash: your_api_hash
bot_token: your_bot_token  # Bot Token，用于 Bot 命令交互

# 媒体类型
media_types:
  - audio
  - document
  - photo
  - video
  - voice
  - animation

# 文件格式过滤（all = 所有格式）
file_formats:
  audio:
    - all
  document:
    - pdf
    - epub
  video:
    - mp4

# 下载设置
save_path: /app/downloads
file_path_prefix:
  - chat_title
  - media_datetime
file_name_prefix:
  - file_name
hide_file_name: false

# 并发控制
max_download_task: 5  # worker 数量
file_download_concurrency: 4  # 每个文件的并发下载 worker 数
download_part_size_mb: 64  # 固定切片大小（MiB），最后一片按剩余大小下载

# WebUI
web_host: 0.0.0.0
web_port: 5000
web_login_secret: "replace-with-a-long-password"  # WebUI 访问密码；设为 "" 关闭登录
mcp_token: ""  # 仅供 /mcp 使用，与网页密码独立
mcp_enabled: false
mcp_read_only: false
mcp_allowed_hosts: []

# 权限
allowed_user_ids:
  - 'me'  # 'me' = 当前登录账号

# 其他
language: zh-cn
date_format: '%Y_%m'
enable_download_txt: false
log_level: DEBUG

# 代理（Docker 内访问宿主机代理）
proxy:
  scheme: http
  hostname: 172.17.0.1
  port: 20172
```

### 配置参数说明

下表为未配置选项时的程序默认值，可能与示例配置不同。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `api_id` | Telegram API ID（[my.telegram.org](https://my.telegram.org/apps)） | — |
| `api_hash` | Telegram API Hash | — |
| `bot_token` | 命令交互和通知 Bot 的 Token（[@BotFather](https://t.me/BotFather)）；MCP 独立运行时可留空 | — |
| `media_types` | 下载的媒体类型列表 | — |
| `file_formats` | 各类型文件格式过滤 | — |
| `save_path` | 文件保存路径 | `./downloads` |
| `max_download_task` | 并发下载 worker 数 | `5` |
| `file_download_concurrency` | 单文件并发下载 worker 数 | `1` |
| `download_part_size_mb` | 固定切片大小，单位 MiB，必须为正整数 | `64` |
| `web_port` | WebUI 端口 | `5000` |
| `web_login_secret` | WebUI / Web API 的 Session 登录密码；空字符串保留免登录模式，与 MCP 鉴权独立 | `""` |
| `mcp_token` | 仅供 `/mcp` 使用的 Bearer Token；可打印 ASCII 字符串，不含空白，MCP 开启时不能为空 | `""` |
| `mcp_enabled` | 开启同端口的 `/mcp` Streamable HTTP 入口，始终要求 Token | `false` |
| `mcp_read_only` | MCP 仅暴露查询工具，不影响独立的网页登录会话 | `false` |
| `mcp_allowed_hosts` | 额外允许的 Host，域名或 IP，可带端口；默认已允许 localhost / loopback | `[]` |
| `allowed_user_ids` | 允许使用 Bot 的用户 ID（`'me'` = 自己） | `[]` |
| `language` | Bot 默认语言及已翻译日志（`en` / `zh-cn` / `zh-tw` / `ja` / `es` / `fr` / `ru` / `uk`，兼容旧 `ZH` / `UA`），独立于网页语言 | `EN` |
| `proxy` | 代理配置（scheme / hostname / port） | `{}` |
| `log_level` | 日志级别 | `INFO` |
| `hide_file_name` | 隐藏文件名（UI 显示 `****.mp4`） | `false` |
| `enable_download_txt` | 纯文本消息保存为 .txt | `false` |
| `date_format` | 日期目录格式 | `%Y_%m` |

网页（含登录页）和 Bot 共用 `lang/*.json` 词典，支持简体中文（`zh-cn`）、繁体中文（`zh-tw`）、英语（`en`）、日语（`ja`）、西班牙语（`es`）、法语（`fr`）、俄语（`ru`）和乌克兰语（`uk`）。每种语言一个文件，共用词汇只翻译一次；英语 `messages` 只列出英文词条，其他语言使用英文词条到翻译的映射。网页可随时切换，首次访问按浏览器偏好依次匹配；`zh` 回退 `zh-cn`，`zh-TW` / `zh-HK` / `zh-Hant` 使用繁体中文，不支持的语言回退英语。手动选择保存在当前浏览器中并优先使用，不依赖 IP 定位。Bot 的 `language` 配置与网页互不影响，例如 `language: zh-tw` 或 `/set_language ja`；旧配置 `ZH` 和命令 `zh` 仍代表简体中文，`UA` / `ua` 仍代表乌克兰语。保存配置时使用规范语言代码。运行代码、配置示例注释和 PWA 应用名称使用英语；测试可保留必要的语言数据。失败记录保存英文词条，在 Bot/网页显示时才翻译，无法识别的历史记录保留原文。缺失词条回退英语，再回退原文；原有俄语和乌克兰语保留已有翻译，新增且未翻译的词条使用英语。

添加语言只需复制 `lang/fr.json` 等翻译文件为新语言文件，修改 `code`、`name`、`aliases` 和 `messages`，重启后网页和 Bot 自动识别，无需新增依赖或构建前端。完整步骤及占位符规则见[语言包指南](docs/languages.md)。

文件按 `download_part_size_mb` 切成固定大小的切片，最后一片为剩余大小。
例如每片 64 MiB 时，40 MiB 文件为 1 片，100 MiB 为 2 片，500 MiB 为 8 片。
每个文件启动最多 `file_download_concurrency` 个 worker；每个 worker 下载完一片后从队列领取下一片，直到全部完成，再按切片顺序合并。
每个 worker 使用独立的 Telegram 下载 TCP 连接，并在连续下载后续切片时沿用自己的连接；不同 worker 和文件不共用下载连接。
暂停时保留连接，完成、取消或客户端重启时关闭连接。
取消下载先关闭连接，再按文件删除选项处理该文件在 `temp/` 中的临时文件和 `.parts/` 目录。活动和失败任务的删除确认只提示是否存在缓存文件（临时文件或分片目录），不列出缓存路径；仅在文件和已记录路径的缓存均不存在时提示未找到。网页取消和删除失败任务默认勾选“同时删除本地文件和缓存”，已完成任务默认不勾选，批量操作一致；不勾选则保留文件和断点。Web API/MCP 的 `delete_file` 默认 `false`，Bot 取消继续默认清理临时数据。旧失败记录若未保存临时路径，仅处理可定位的文件。
同时下载的文件数仍由 `max_download_task` 控制；5 个文件各 4 个 worker 时，最多有 20 个并发下载 worker，没有额外的总并发配置。
切片大小和 worker 数在文件首次开始时保存，暂停、重试和程序重启后继续使用该计划，不重复下载已完成切片。
网络错误退避增加最多 50% 的随机等待，并统一限制在 30 秒以内；普通连接错误为 10–15 / 30 / 30 秒，超时每次等待 30 秒。每轮分片下载失败后先关闭所有 worker 连接，下一轮使用新的独立下载 session，并保留断点；同一轮内成功下载后继续复用 worker 连接。FLOOD_WAIT 按 Telegram 返回的秒数等待，不消耗下载尝试次数，没有累计等待上限。单次等待超过 600 秒时，该文件进入暂停状态并保留断点，不计为失败；手动恢复后仍须等服务端冷却结束。600 秒以内自动等待后继续。界面显示 FLOOD_WAIT 和剩余时间，等待期间可暂停或取消。
进度条汇总所有切片的已下载字节，续传恢复的已有字节不计入新传输速度。
旧版分片断点仍可续传。旧 `download_min_part_size_mb` 在没有新字段时作为切片大小的兼容值读取，保存配置时迁移为 `download_part_size_mb`；旧 `download_concurrency` 已移除。

分片断点保存在 `temp/…/文件名.parts/` 中，部署时应持久化整个 `temp` 目录。
多分片合并期间临时空间最高约为文件大小的两倍，单分片直接移动文件，无需额外完整副本。
离线验证和真实下载验收步骤见 [分片下载测试说明](docs/parallel-download-testing.md)。
`log/tdl.log` 和 `log/download.log` 中的 `Opened independent download connection #…` 记录独立下载连接的打开，包含文件名和 worker 编号；对应的 `Closed independent download connection #…` 记录连接关闭。

---

## WebUI 登录

浏览器打开 `http://你的服务器IP:5000`

配置了非空的 `web_login_secret` 时，会先显示登录页，输入配置的密码后进入控制面板。首页和所有下载接口都会校验访问凭据；右上角可退出。登录页可选勾选“记住我（7 天）”，默认不勾选。勾选后通过持久化 Cookie 保持登录，关闭并重新打开浏览器无需再次输入密码；不勾选时使用浏览器会话 Cookie（浏览器恢复会话时可能保留）。两者最长有效期均为 7 天；重启服务或修改密码后需要重新登录。

控制面板支持提交单条 HTTPS `t.me` 消息链接（公开、私有和话题消息）。使用服务端已有 Telegram 登录，无需在网页重新登录 Telegram；账号需有访问权限。提交后先持久化入队，再由现有消费者下载；重复未完成链接复用任务。未配置 `bot_token` 且未开启 MCP 时，也会启动任务后台，配置下载结束后服务继续运行以接收网页任务。

Web API 与网页共用密码登录 Session，修改状态时还需提供当前 Session 的 CSRF Token，WebUI 会自动携带。Web API 不接受 Bearer Token；Agent 使用下节的 `/mcp` 和独立 MCP Token。

要关闭密码登录，可在 `config.yaml` 中设置 `web_login_secret: ""`。修改配置后重启容器。已有部署需更新到包含此修复的镜像，仅修改旧版本的配置不会启用登录。

## MCP Agent 接入

MCP 集成在下载器主服务中，复用主镜像、容器和 `web_port`（默认 `5000`），入口为 `/mcp`。工具直接调用内部任务管理逻辑。[工具与参数参考](./docs/mcp.md)。13 个工具支持提交和管理下载任务；`submit_download` 接受单条 Telegram 消息链接，持久化到现有 Pending 队列。

### 开启 MCP

MCP 默认关闭。先生成一个随机 Token：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

在 `config.yaml` 将结果填入带引号的 `mcp_token`，并开启 MCP。Token 必须是可打印 ASCII 字符串，不含空白；空字符串或 `null` 表示未配置：

```yaml
mcp_token: "paste-your-generated-token-here"
mcp_enabled: true
mcp_read_only: false
mcp_allowed_hosts: ["nas.example.com"]
```

`mcp_enabled: true` 必须配置非空 `mcp_token`，否则启动报错。每个请求都需携带 Bearer Token。关闭时 `/mcp` 返回 `404`。

服务端使用 `mcp_allowed_hosts` 配置额外允许的域名或 IP，例如 `["nas.example.com", "192.168.1.20:5000"]`。端口须与请求的 Host 一致，不含协议或路径，不支持通配符。默认已允许 localhost / loopback；Origin 校验自动复用这份白名单，接受对应的 HTTP / HTTPS 来源。监听地址和端口仍由 `web_host`、`web_port` 决定，完整连接 URL 仅在客户端配置中填写。

更新到包含此功能的主镜像后，修改配置并重启现有容器即可：

```bash
docker compose restart telegam-media-downloader
```

首次部署、更新镜像和从源码构建仍使用前文的主服务步骤。无需额外镜像、容器、端口、环境变量或依赖文件；本地安装只需 `pip install -r requirements.txt`。已有持久化挂载保持不变。

### Agent 客户端

通过 Streamable HTTP 连接服务器的 `/mcp` 地址。支持 `url`、`headers` 的客户端可参考以下格式，具体字段以客户端为准：

```json
{
  "mcpServers": {
    "telegram-media-downloader": {
      "url": "https://nas.example.com/mcp",
      "headers": {
        "Authorization": "Bearer paste-the-token-from-config.yaml"
      }
    }
  }
}
```

本机可使用 `http://127.0.0.1:5000/mcp`，局域网可使用配置的 NAS 地址。公网访问使用 HTTPS 反向代理，保留原始 Host 请求头，并允许 `/mcp` 的 GET、POST、DELETE 请求通过。此实现采用预配置 Bearer Token，不提供 OAuth 登录页。Token 只放在受保护的配置和请求头中，不放在 URL 或工具参数中；错误或缺失 Token 返回 `401`。

`mcp_read_only: true` 只暴露三个查询工具，不开放提交下载。修改 Token 后重启下载器，并同步更新 Agent 配置，旧 Token 随即失效。默认允许写操作，删除工具的 `delete_file` 默认 `false`。[工具列表与删除语义](./docs/mcp.md)。

### 提交下载

调用 `submit_download`，参数例如：

```json
{"url": "https://t.me/channel_name/123"}
```

支持公开消息、`https://t.me/c/123456/789` 私有频道消息，以及带话题 ID 的消息链接。下载器当前登录的 Telegram 账号必须能访问该频道。仅通过 MCP 提交任务时，可将 `bot_token` 设为 `""`；服务会持续运行并启动现有队列与恢复机制，不创建额外 Bot 会话。仅接受 `https://t.me` 的单条消息链接，不接受频道首页、邀请链接、查询参数、整频道或范围任务。频道解析超时为 10 秒；服务未就绪或处于 Telegram 冷却期时返回工具错误。

返回 `status: pending` 表示已持久化入队，不表示下载完成。`task_id` 对应进行中/已完成记录，`pending_task_id` 对应待执行记录，可传给 `remove_pending_task` 取消；`task_id_display` 仅用于展示。同一消息仍在队列或下载中时，重复提交返回已有任务并设置 `duplicate: true`。任务复用现有并发限制和重启恢复，不额外发送 Bot 通知；后续使用 `list_downloads` 查询进度。

## Docker 管理

```bash
# 查看日志
docker-compose logs -f

# 后台启动
docker-compose up -d

# 停止
docker-compose down

# 重启
docker-compose restart

# 更新到 Compose 中指定版本的镜像
docker-compose pull
docker-compose up -d --force-recreate

# 查看容器状态
docker-compose ps
```

使用 `docker run` 创建的容器可用以下命令管理：

```bash
docker logs -f telegam-media-downloader
docker restart telegam-media-downloader
docker stop telegam-media-downloader
docker start telegam-media-downloader
```

更新镜像时需使用新镜像重新创建容器，并保留原来的配置、端口和数据挂载；仅重启旧容器不会切换镜像。镜像选择见 [前置条件](#前置条件)。

## 旧镜像迁移与本地构建

旧 package `ghcr.io/smilekevin/hermes-telegram-downloader` 保留历史镜像。迁移时将 Compose 的 `image` 改为新地址，使用新构建的版本或 digest；历史 digest 仍需搭配旧地址。

如需从当前本地源码构建，先克隆仓库并完成配置，将 Compose 服务的 `image` 改为 `telegram-media-downloader:local` 并添加 `build: .`，然后执行：

```bash
docker-compose build
# 首次登录前停止已有后台服务
docker-compose stop telegam-media-downloader
docker-compose run --rm telegam-media-downloader
# 登录成功后 Ctrl+C 退出，再启动后台服务
docker-compose up -d
```

更新本地源码后执行 `docker-compose up -d --build`。首次登录说明见 [Docker Compose 部署](#docker-compose-部署)。

## Docker 数据持久化

| 路径 | 说明 |
|------|------|
| `./downloads/` | 下载的文件 |
| `./config.yaml` | 配置文件 |
| `./data.yaml` | 运行时数据（ids_to_retry 等） |
| `./log/` | 日志 + 任务持久化（bot_tasks.json / task_counter.json / download_history.json） |
| `./sessions/` | Telegram session 文件 |
| `./temp/` | 下载临时文件和 `.parts/` 分片断点 |

保留所有挂载目录，容器移除或重建不会删除宿主机数据。迁移时同步保留配置、运行数据、任务日志、session 和分片断点；自定义的宿主机下载路径对应表中的 `./downloads/`。

主下载账号使用 Bot 身份时，请在 `allowed_user_ids` 中填写操作人的数字 Telegram 用户 ID。数字 ID 不依赖该 Bot 的联系人缓存；`me` 指向主下载账号本身，不会自动授权真人用户。Bot 身份不会接收启动帮助通知。

## 代理配置

Kurigram 连接 Telegram 使用 `config.yaml` 中的 `proxy`：

```yaml
proxy:
  scheme: socks5
  hostname: 172.17.0.1
  port: 1080
```

模板使用 HTTP 代理 `172.17.0.1:20172`；请按实际代理调整协议、地址和端口。Docker 中其他支持环境变量代理的请求，可在 Compose 中配置：

```yaml
environment:
  - http_proxy=socks5://你的代理IP:端口
  - https_proxy=socks5://你的代理IP:端口
```

容器中 `127.0.0.1` 指向容器自身，代理地址必须可从容器访问；`172.17.0.1` 需根据 Docker / NAS 网络实际情况调整。手动安装使用本机可访问的地址。无需代理时设置 `proxy: {}`，并删除或注释 Compose 中的代理环境变量。省略 `proxy` 或设为 `null` 也表示不使用 Telegram 代理；主客户端和 Bot 客户端都会将空配置转换为 Kurigram 所需的 `None`。

## 故障排查

1. **容器无法启动** — 检查 `docker-compose logs`
2. **无法连接 Telegram** — 检查代理配置，确保容器能访问 `149.154.167.50:443`
3. **下载失败** — 在 Web UI 的"失败"Tab 查看具体原因
4. **权限问题** — 确保挂载目录有写入权限
5. **session 丢失** — 确保 `./sessions/` 目录已正确挂载
6. **FLOOD_WAIT** — 频道消息过多触发了 Telegram 限速，等待冷却后自动恢复

---

## Prerequisites

The Telegram SDK is the Kurigram version pinned in `requirements.txt`, imported as `pyrogram`. Do not co-install Pyrogram, Pyrofork and Kurigram in one virtual environment. For an existing manual installation, recreate the virtual environment before installing the full requirements.

Install main-service dependencies from `requirements.txt`, which explicitly pins the tested Starlette, Uvicorn and Pydantic versions. Filter expressions use the pinned Lark parser; PLY and generated `parsetab.py` files are no longer needed. Flask-Login, Requests and PyCryptodome are no longer required. Recreate an older virtual environment when upgrading so leftover packages cannot hide missing dependencies.

Before starting this version for the first time, stop the service and back up `sessions/`, `config.yaml`, `data.yaml`, `downloads/` and `temp/`. Kurigram upgrades existing session databases in place. To roll back to the previous SDK, stop the service and restore the session backup from before migration. Task and `.parts/` checkpoint formats remain unchanged; offline migration tests do not replace real Telegram download acceptance checks.

- Image deployment requires Docker; Compose deployment also requires Docker Compose. Manual installation uses Python 3.14 and pip.
- Actual downloads require Telegram API credentials and access to the target messages. Mock development mode does not require Telegram login.
- The project and image use `telegram-media-downloader`; the Compose service and container intentionally use `telegam-media-downloader`.
- The prebuilt image is `ghcr.io/smilekevin/telegram-media-downloader`, supporting amd64 and arm64. Published GitHub Releases create version, SHA and `latest` tags. CI verification images use SHA tags and do not update `latest`. The README Version badge follows the latest stable GitHub Release tag; its Python badge reads `python-version` from `.github/workflows/tests.yml`. Badge updates appear after caches refresh.
- For PR or master verification, use the SHA tag or digest from **CI image for verification** in the [CI Summary](https://github.com/smilekevin/telegram-media-downloader/actions/workflows/tests.yml). The [Release workflow](https://github.com/smilekevin/telegram-media-downloader/actions/workflows/docker-build.yml) adds pull commands, digests and platforms to Release notes after a successful image push, preserving the existing description and updating the same section on reruns. Package visibility must be Public for unauthenticated pulls.

The commands below use `docker-compose`; replace it with `docker compose` if you use the Compose plugin.

## Quick Start

Docker deployment uses this fork's `ghcr.io/smilekevin/telegram-media-downloader:latest` image, updated when GitHub Releases are published.

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

With Docker installed on your server or NAS, run this fork's prebuilt image directly.

1. **Prepare configuration and persistent data**: download [config.yaml.example](./config.yaml.example) from this repository, copy it to `config.yaml`, and create the runtime data file and persistent directories. These commands preserve existing `config.yaml` and `data.yaml` files.

```bash
mkdir -p telegram-media-downloader
cd telegram-media-downloader
mkdir -p downloads log sessions temp

curl -fL https://raw.githubusercontent.com/smilekevin/telegram-media-downloader/master/config.yaml.example \
  -o config.yaml.example
[ -f config.yaml ] || cp config.yaml.example config.yaml
[ -f data.yaml ] || printf '{}\n' > data.yaml
```

2. **Edit the configuration**: open `config.yaml` in a text editor and enter `api_id`, `api_hash` and `bot_token`, and change `web_login_secret` to your own password. See [Configuration](#configuration) for media types, permissions and concurrency. The template includes an example proxy; adjust `proxy` for your environment or set `proxy: {}` if you do not need one. With the mounts and port mapping below, keep `save_path: /app/downloads`, `web_host: 0.0.0.0` and `web_port: 5000`.

3. **Pull the image and start it for the first time**: mount configuration, runtime data, downloads, logs, sessions and temporary files, and use interactive mode for Telegram login.

```bash
docker pull ghcr.io/smilekevin/telegram-media-downloader:latest

docker run -it \
  --name telegam-media-downloader \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/config.yaml:/app/config.yaml" \
  -v "$PWD/data.yaml:/app/data.yaml" \
  -v "$PWD/downloads:/app/downloads" \
  -v "$PWD/log:/app/log" \
  -v "$PWD/sessions:/app/sessions" \
  -v "$PWD/temp:/app/temp" \
  ghcr.io/smilekevin/telegram-media-downloader:latest
```

4. **First login (or when login is needed again, such as after session expiry)**: **The account signed in here performs the actual downloads**. The program accesses and downloads messages as that Telegram user or bot, using that account's permissions. When `Enter phone number or bot token:` appears in the terminal, choose either option and confirm with `y` when prompted:

- **Phone**: enter your phone number with its country code (for example, `+8613812345678`), then the verification code sent by Telegram. Enter your two-step verification password if enabled.
- **Bot Token**: enter the complete Token obtained in [Before You Deploy](#before-you-deploy-api-credentials-and-bot-token). No phone verification code is needed.

After login succeeds, press `Ctrl+C` to stop the current foreground run. The session persists in the mounted `sessions/` directory; a valid saved session avoids repeat login on restart. Tasks and download history persist in `log/`. See [Docker Data Persistence](#docker-data-persistence) for mount details.

5. **Start in the background and access WebUI**: run the following commands to stop the container and use [`docker start`](https://docs.docker.com/reference/cli/docker/container/start/) to start the existing container in the background:

```bash
docker stop telegam-media-downloader
docker start telegam-media-downloader
```

Once started, open `http://SERVER_IP:5000` and enter your configured password. See [WebUI Login](#webui-login).

### Docker Compose Deployment

Deploy with the repository's `docker-compose.yaml`:

```bash
git clone https://github.com/smilekevin/telegram-media-downloader.git
cd telegram-media-downloader

# Preserve existing configuration and runtime data.
[ -f config.yaml ] || cp config.yaml.example config.yaml
[ -f data.yaml ] || printf '{}\n' > data.yaml
mkdir -p downloads log sessions temp
# Edit config.yaml with your credentials and change the WebUI password.
```

Create configuration bind-mount sources as files before starting Docker. The repository Compose file enables example proxy environment variables; adjust or remove them for your network. A complete example without environment variable proxies is:

```yaml
services:
  telegam-media-downloader:
    image: ghcr.io/smilekevin/telegram-media-downloader:latest
    container_name: telegam-media-downloader
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
      - "./config.yaml:/app/config.yaml"
      - "./data.yaml:/app/data.yaml"
      # Logs
      - "./log:/app/log/"
      # Telegram sessions
      - "./sessions:/app/sessions"
      # Temporary files and part checkpoints
      - "./temp:/app/temp"
    restart: unless-stopped
```

You can change the host download directory to `/your/download/path:/app/downloads/`; keep `save_path: /app/downloads` inside the container. When changing `web_port`, update the Compose port mapping too. Prebuilt images need only configuration and data mounts; avoid mounting local source code over the image.

**First login (or when login is needed again, such as after session expiry)**: **The account signed in here performs the actual downloads**. The program accesses and downloads messages as that Telegram user or bot, using that account's permissions. Start a one-off container in an interactive terminal, stopping the background service first if it is running:

```bash
# Pull a published Release image.
docker-compose pull
docker-compose stop telegam-media-downloader
docker-compose run --rm telegam-media-downloader
```

At `Enter phone number or bot token:`, enter your phone number with its country code or a complete Bot Token, and confirm with `y`. Phone login also requires a verification code and your two-step verification password if enabled; Bot Token login does not require a phone verification code. After login succeeds, press `Ctrl+C` to stop this foreground run, then start the background service:

```bash
docker-compose up -d

# View logs
docker-compose logs -f
```

Compose saves the session in the host's `sessions/` directory. `--rm` removes only the one-off container, leaving mounted directories intact. A valid saved session avoids repeat login on later starts.

### Manual Install

```bash
git clone https://github.com/smilekevin/telegram-media-downloader.git
cd telegram-media-downloader
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

At `Enter phone number or bot token:`, enter your phone number with its country code or a complete Bot Token, and confirm with `y`. Phone login also requires a verification code and your two-step verification password if enabled; Bot Token login does not require a phone verification code. The program continues running after login. The session is saved in the project's `sessions/` directory. A valid saved session avoids repeat login on later starts.

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
| `api_id` | Telegram API ID ([my.telegram.org](https://my.telegram.org/apps)) | — |
| `api_hash` | Telegram API Hash | — |
| `bot_token` | Command and notification bot Token ([@BotFather](https://t.me/BotFather)); may be empty in MCP-only mode | — |
| `media_types` | Media types to download | — |
| `file_formats` | Format filter per type | — |
| `save_path` | File save path | `./downloads` |
| `max_download_task` | Concurrent download workers | `5` |
| `file_download_concurrency` | Concurrent download workers per file | `1` |
| `download_part_size_mb` | Fixed part size in MiB; positive integer | `64` |
| `web_port` | WebUI port | `5000` |
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

The WebUI (including sign-in) and Bot share `lang/*.json` catalogs: Simplified Chinese (`zh-cn`), Traditional Chinese (`zh-tw`), English (`en`), Japanese (`ja`), Spanish (`es`), French (`fr`), Russian (`ru`) and Ukrainian (`uk`). Each language has one file and shared vocabulary is translated once. English `messages` lists canonical keys; other languages map those keys to translations. The browser matches language preferences in order; `zh` falls back to `zh-cn`, while `zh-TW` / `zh-HK` / `zh-Hant` select Traditional Chinese. Unsupported languages fall back to English. Manual choices are saved in the current browser and take priority; no IP geolocation is used. Configure the Bot independently, for example `language: zh-tw` or `/set_language ja`. Legacy `ZH` / `zh` still mean Simplified Chinese and `UA` / `ua` still mean Ukrainian. Saved configuration uses canonical language codes. Runtime code, configuration example comments and the PWA app name use English; tests may retain required language fixtures. Failure records store English keys and translate when displayed by the Bot or browser; unrecognized historical records retain their original text. Missing messages fall back to English, then the original text; existing Russian and Ukrainian translations are preserved, with English fallback for untranslated additions.

To add a language, copy a translated catalog such as `lang/fr.json` to a new language file, edit `code`, `name`, `aliases` and `messages`, then restart. Both Bot and browser discover it automatically; no extra dependency or frontend build is required. See the [catalog guide](docs/languages.md) for steps and placeholder rules.

Files are split into fixed-size parts using `download_part_size_mb`; the final part contains the remaining bytes.
At 64 MiB per part, a 40 MiB file has one part, a 100 MiB file has two and a 500 MiB file has eight.
Each file starts up to `file_download_concurrency` workers. Finished workers take the next queued part until
all parts are complete, then the parts are merged in order. Each worker owns an independent Telegram download
TCP connection and uses it for successive parts; different workers and files do not share download connections.
Pauses retain connections; completion, cancellation and client restart close them. `max_download_task` limits
simultaneous files: five files with four workers each can run up to twenty concurrent workers, without an additional global concurrency setting.
Cancellation closes connections before applying the file deletion choice to staging files and `.parts/` in `temp/`. For active and failed tasks, the deletion dialog indicates whether staging files or part directories exist without listing cache paths; it reports no files only when neither output nor cache at recorded paths exists. WebUI checks the file and cache option by default for active/failed tasks and leaves it unchecked for completed tasks, including batch actions. Deselect it to retain files and checkpoints. Web API/MCP `delete_file` defaults to `false`; Bot cancellation still cleans staging by default. Older failed records without a recorded staging path can only clean files whose paths are known.
Network backoff adds up to 50% random jitter and caps every delay at 30 seconds: ordinary connection errors wait 10–15 / 30 / 30 seconds, and timeouts wait 30 seconds per attempt. Failed part-download attempts close all worker connections before the next attempt creates fresh independent download sessions, preserving checkpoints. Successful parts within an attempt reuse their worker connection. FLOOD_WAIT waits for the seconds returned by Telegram without consuming download attempts or imposing a cumulative wait limit. A single wait over 600 seconds pauses that file with checkpoints retained instead of marking it failed; manual resume is required and still honors the server cooldown. Waits of 600 seconds or less resume automatically. The UI shows FLOOD_WAIT and the remaining time; pause and cancellation remain available.
The initial part size and worker count are saved. Pauses, retries and process restarts reuse this plan and skip completed parts.
Progress sums downloaded bytes across all parts; resumed bytes do not count toward new transfer speed.
Legacy checkpoints remain resumable. If the new field is absent, `download_min_part_size_mb` is accepted as a
legacy part-size value and migrated to `download_part_size_mb` when saving. The earlier `download_concurrency` setting has been removed.

Persist the entire `temp` directory, including `filename.parts/` checkpoints. Merging multiple parts may
temporarily require twice the file size in disk space; a single part is moved without a full extra copy.
See the [download test guide](docs/parallel-download-testing.md) for offline and live acceptance checks.
In `log/tdl.log` and `log/download.log`, `Opened independent download connection #…` records a connection opening
with its file and worker number; the matching `Closed independent download connection #…` records its closure.

---

## WebUI Login

Open `http://SERVER_IP:5000`. A non-empty `web_login_secret` protects the dashboard and download APIs. Enter that password on the login page; use the top-right action to log out. The login page offers an unchecked “Remember me (7 days)” option. Selecting it stores a persistent cookie so reopening the browser does not require another password; otherwise it uses a browser-session cookie (browser session restore may retain it). Both expire after at most seven days and require login again after a service restart or password change. State-changing requests using browser sessions also require the session's CSRF token; the WebUI supplies it automatically. The Web API accepts only browser login sessions, not bearer tokens. Agents use `/mcp` with a separate MCP token.

The dashboard accepts one HTTPS `t.me` post link (public, private or topic). It uses the existing server Telegram login and requires access to the chat. Submission persists before the existing consumer fetches media; unfinished duplicate posts reuse their task. With no `bot_token` and MCP disabled, the same backend still starts and the service stays running after configured downloads finish to accept WebUI tasks.

Set `web_login_secret: ""` to disable password login and restart the service after changing configuration. Existing deployments must use an image containing the login implementation; changing an older image's configuration alone does not enable it.

## MCP Agent Integration

MCP is integrated into the downloader, reusing the main image, container and `web_port` (default `5000`). Its endpoint is `/mcp`. Tools call internal download-management operations directly. [Tool reference](./docs/mcp.md#mcp-tool-reference). The 13 tools submit and manage downloads; `submit_download` accepts a single Telegram message link and persists it in the existing Pending queue.

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
docker compose restart telegam-media-downloader
```

Initial deployment, image updates and source builds use the main-service instructions above. No additional image, container, port, environment variables or dependency file is needed. Local installs use `pip install -r requirements.txt`. Existing persistence mounts remain unchanged.

### Agent Client

Connect through Streamable HTTP at the server’s `/mcp` URL. Clients supporting `url` and `headers` can use this example; actual fields depend on the client:

```json
{
  "mcpServers": {
    "telegram-media-downloader": {
      "url": "https://nas.example.com/mcp",
      "headers": {
        "Authorization": "Bearer paste-the-token-from-config.yaml"
      }
    }
  }
}
```

Use `http://127.0.0.1:5000/mcp` locally or the configured NAS URL on a LAN. For public access, use an HTTPS reverse proxy that preserves the original Host header and forwards GET, POST and DELETE requests to `/mcp`. Authentication uses a preconfigured bearer token, without an OAuth login page. Store tokens in protected configuration and request headers, never URLs or tool parameters. Missing or invalid tokens return `401`.

`mcp_read_only: true` exposes only the three query tools, without download submission. After rotating a token, restart the downloader and update agent configuration; the previous token is revoked. Write tools are enabled by default; deletion's `delete_file` defaults to `false`. See the [tool reference](./docs/mcp.md#mcp-tool-reference).

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
docker logs -f telegam-media-downloader
docker restart telegam-media-downloader
docker stop telegam-media-downloader
docker start telegam-media-downloader
```

To update an image, recreate the container with the new image while preserving configuration, ports and mounts. Restarting the existing container does not change its image.

## Image Migration and Local Builds

The old `ghcr.io/smilekevin/hermes-telegram-downloader` package retains historical images. For migration, change Compose's `image` to the new package and select a newly built version or digest. Historical digests still require the old package address.

To build local source, clone the repository and prepare configuration, set `image: telegram-media-downloader:local` and add `build: .` to the Compose service, then run:

```bash
docker-compose build
# Stop any background service before first login.
docker-compose stop telegam-media-downloader
docker-compose run --rm telegam-media-downloader
# After login, press Ctrl+C and start in the background.
docker-compose up -d
```

After updating local source, run `docker-compose up -d --build`. See [Docker Compose Deployment](#docker-compose-deployment) for first-login details.

## Docker Data Persistence

| Path | Description |
|------|-------------|
| `./downloads/` | Downloaded files |
| `./config.yaml` | Config file |
| `./data.yaml` | Runtime data (ids_to_retry, etc.) |
| `./log/` | Logs + task persistence (bot_tasks.json / task_counter.json / download_history.json) |
| `./sessions/` | Telegram session files |
| `./temp/` | Download staging files and `.parts/` checkpoints |

Keep every data mount when recreating containers. Migration must retain configuration, runtime data, task logs, sessions and part checkpoints. A custom host download directory replaces `./downloads/` in this table.

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
5. **Session is lost**: check the `sessions/` mount and repeat interactive login.
6. **FLOOD_WAIT**: wait for the Telegram cooldown; pending tasks are retried automatically.
