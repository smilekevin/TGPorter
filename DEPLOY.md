# TG Porter - 部署指南 / Deployment Guide

安装、配置、登录和运维步骤统一维护在本文件。功能介绍、Bot 命令、Web API 和架构见 [README 中文说明](./README.md#功能特性)。

Installation, configuration, login and operations are maintained here. English instructions start at [Prerequisites](#prerequisites). See the [README](./README.md#features) for features, Bot commands, Web APIs and architecture.

中文：[前置条件](#前置条件) · [凭据准备](#部署前准备api-凭据与-bot-token) · [Docker 镜像](#docker-镜像部署) · [Compose](#docker-compose-部署) · [手动安装](#手动安装) · [本地开发](#本地开发模式) · [配置说明](#配置说明) · [WebUI 登录](#webui-登录) · [MCP](#mcp-agent-接入) · [运维](#docker-管理) · [持久化](#docker-数据持久化) · [代理](#代理配置) · [故障排查](#故障排查)

English: [Prerequisites](#prerequisites) · [Credentials](#before-you-deploy-api-credentials-and-bot-token) · [Docker Image](#docker-image-deployment) · [Compose](#docker-compose-deployment) · [Manual Install](#manual-install) · [Local Dev](#local-dev-mode) · [Configuration](#configuration) · [WebUI Login](#webui-login) · [MCP](#mcp-agent-integration) · [Operations](#docker-management) · [Persistence](#docker-data-persistence) · [Proxy](#proxy-configuration) · [Troubleshooting](#troubleshooting)

---

## 前置条件

Telegram SDK 使用 `requirements.txt` 中固定版本的 Kurigram（仍通过 `pyrogram` 导入）。不要在同一虚拟环境中混装 Pyrogram、Pyrofork 和 Kurigram；从旧版本手动安装迁移时，请重新创建虚拟环境，再安装完整依赖。

WebUI 使用原生 HTML/CSS/JS，无需安装 Node.js 或运行 npm 构建；CI 工作流脚本测试单独使用 Node.js 24。工作流使用 `actions/checkout@v5`、`actions/setup-node@v5` 和 `actions/setup-python@v6`，以及 `docker/build-push-action@v7`、`docker/login-action@v4`、`docker/setup-buildx-action@v4` 和 `docker/setup-qemu-action@v4`；这些 action 均原生运行于 Node.js 24。

主服务通过 `requirements.txt` 安装依赖，直接使用的 Starlette、Uvicorn、Pydantic 固定为已验证版本。过滤表达式由固定版本的 Lark 解析，不再使用 PLY 或生成 `parsetab.py`。不再需要 Flask-Login、Requests 或 PyCryptodome；从旧环境升级时重新创建虚拟环境，可避免残留包掩盖缺失依赖。

首次使用此版本前，先停止服务并备份 `data/sessions/`、`config.yaml`、`data/data.json`、`downloads/` 和 `downloads/.temp/`。Kurigram 会原地升级旧 Session 数据库；回滚到旧 SDK 时，应停服并恢复升级前的 Session 备份。任务与 `.parts/` 检查点格式保持不变；离线迁移测试不替代真实 Telegram 下载验收。

1. Docker 镜像部署需要 Docker；Compose 部署还需 Docker Compose。手动安装需要 Python 3.14 和 pip。
2. 有 Telegram API 凭证（api_id + api_hash），从 https://my.telegram.org 获取
3. 已创建 Bot Token（通过 @BotFather）

本 fork 提供自动构建的预构建镜像：`ghcr.io/smilekevin/tgporter:latest`，支持 amd64/arm64。[CI 验证镜像](https://github.com/smilekevin/TGPorter/actions/workflows/tests.yml) / [Build Release Docker Image](https://github.com/smilekevin/TGPorter/actions/workflows/docker-build.yml)。上游 MangoIsIllegal 项目不提供预构建镜像。

项目展示名为 **TG Porter**，仓库名为 `TGPorter`；GHCR 镜像、Compose 服务和容器名使用小写 `tgporter`。

升级已有部署时，先停止旧容器，保留并复用原有数据挂载，再使用新服务名启动，避免两个实例同时访问 Session 和任务数据。GitHub 仓库需完成远端重命名，新 GHCR 镜像需由 CI 或 Release 工作流发布后才能拉取。

镜像发布规则：本仓库 PR、合并/推送 main 或手动运行 CI 后，测试通过即发布 `:sha-完整提交号`，Summary 提供可复制的拉取命令和 Compose 配置，不更新 `latest`。**Build Release Docker Image** 只在发布 GitHub Release 后运行，发布同名版本镜像并更新 `:latest`，例如 `v1.0.1` 对应 `:v1.0.1`。不再定时构建。README 的 Version 徽章自动跟随最新正式 GitHub Release 标签，Python 徽章自动读取 `.github/workflows/tests.yml` 中的 `python-version`，徽章缓存刷新后生效。

Release 镜像推送成功后，工作流会自动在对应 Release notes 中补充版本拉取命令、digest 地址和支持的平台。原有说明会保留，重新运行时更新同一区块。

本地 Mock 开发模式无需 Telegram 账号或凭证。下文使用 `docker-compose`；使用 Compose 插件的环境可替换为 `docker compose`。

## 快速开始

Docker 部署使用本 fork 发布的 `ghcr.io/smilekevin/tgporter:latest`，由 GitHub Release 发布流程更新。

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

启动 Bot 后，发送 `/help` 或 `/start`，可通过 “Join us” 按钮加入 [Telegram 群组](https://t.me/tg_porter)。

服务器或 NAS 已安装 Docker 后，可以直接运行本 fork 的预构建镜像。

1. **准备配置和数据目录**：从本仓库下载 [config.yaml.example](./config.yaml.example)，复制为 `config.yaml`，并创建 `downloads/` 和 `data/` 目录。无需预建数据文件，程序首次启动自动生成 `data/data.json`。

```bash
mkdir -p tgporter
cd tgporter
mkdir -p downloads data

curl -fL https://raw.githubusercontent.com/smilekevin/TGPorter/main/config.yaml.example \
  -o config.yaml.example
[ -f config.yaml ] || cp config.yaml.example config.yaml
```

2. **填写配置**：使用文本编辑器打开 `config.yaml`，填写 `api_id`、`api_hash` 和 `bot_token`，并将 `web_login_secret` 改为自己的密码，按[配置说明](#配置说明)设置媒体类型、权限和并发数。模板自带示例代理，请按自己的环境修改 `proxy`；无需代理时设为 `proxy: {}`。使用下面的挂载和端口映射时，保留 `save_path: /app/downloads`、`web_host: 0.0.0.0` 和 `web_port: 5000`。

3. **拉取镜像并首次启动**：挂载配置文件、`data/` 和下载目录，以交互模式完成 Telegram 登录。

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

4. **首次登录（或需要重新登录时，比如 session 失效）**：**此处登录的账号为实际下载用户**，程序以该 Telegram 用户或 Bot 账号的身份访问并下载消息，下载权限取决于该账号。在终端出现 `Enter phone number or bot token:` 时，选择一种方式输入，并在确认提示时输入 `y`：

- **手机号**：输入带国家区号的手机号（例如 `+8613812345678`），再输入 Telegram 发送的验证码；启用两步验证时还需输入密码。
- **Bot Token**：输入在[部署前准备](#部署前准备api-凭据与-bot-token)中获取的完整 Token，无需手机号验证码。

登录成功后，按 `Ctrl+C` 停止当前前台运行。session 保存在挂载的 `data/sessions/`，保留有效 session 后重启无需再次登录。任务与下载历史保存在 `data/log/`；各挂载目录的用途见[Docker 数据持久化](#docker-数据持久化)。

5. **后台启动并访问 WebUI**：执行以下命令，停止容器后用 [`docker start`](https://docs.docker.com/reference/cli/docker/container/start/) 在后台启动已有容器：

```bash
docker stop tgporter
docker start tgporter
```

启动后，在浏览器中访问 `http://服务器IP:5000`，使用配置的密码登录，见 [WebUI 登录](#webui-登录)。

### Docker Compose 部署

使用仓库中提供的 `docker-compose.yaml` 部署：

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter

# 创建配置和持久化目录，保留已有配置与数据
[ -f config.yaml ] || cp config.yaml.example config.yaml
mkdir -p downloads data
# 编辑 config.yaml，填入 api_id / api_hash / bot_token，并修改 WebUI 密码
```

仅 `config.yaml` 的挂载源需要预先准备为文件。`data/` 挂载为可写目录，程序会自动创建 JSON、日志和 session；临时数据默认保存在 `save_path/.temp/`（示例为 `/app/downloads/.temp`），无需单独挂载。仓库 Compose 默认启用了示例代理环境变量，请按实际环境修改；无需代理时删除或注释这两项。下面是无需环境变量代理的完整配置示例：

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
      # 下载目录 (修改为你的实际路径)
      - "./downloads:/app/downloads/"
      # 配置文件
      - "./config.yaml:/app/config.yaml:ro"
      - "./data:/app/data"
    restart: unless-stopped
```

下载目录可改为 `/your/download/path:/app/downloads/`；`config.yaml` 中的 `save_path` 仍使用容器路径 `/app/downloads`。修改 `web_port` 时同步调整 Compose 端口映射。预构建镜像只需配置和数据挂载，避免用本地源码挂载覆盖镜像内代码。

**首次登录（或需要重新登录时，比如 session 失效）**：**此处登录的账号为实际下载用户**，程序以该 Telegram 用户或 Bot 账号的身份访问并下载消息，下载权限取决于该账号。使用交互终端启动一次性容器；如果后台服务正在运行，先停止它：

```bash
# 拉取已发布的 Release 镜像
docker-compose pull
docker-compose stop tgporter
docker-compose run --rm tgporter
```

出现 `Enter phone number or bot token:` 时，输入带国家区号的手机号或完整 Bot Token，并在确认提示时输入 `y`。手机号登录还需输入验证码，以及两步验证密码（如已启用）；Bot Token 登录无需手机号验证码。登录成功后按 `Ctrl+C` 停止此次前台运行，再启动后台服务：

```bash
docker-compose up -d

# 查看日志
docker-compose logs -f
```

Compose 将 session 保存在宿主机的 `data/sessions/`；`--rm` 只删除一次性容器，不会删除挂载目录。保留有效 session 后，后续启动无需再次登录。

### 手动安装

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
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

出现 `Enter phone number or bot token:` 时，输入带国家区号的手机号或完整 Bot Token，并在确认提示时输入 `y`。手机号登录还需输入验证码，以及两步验证密码（如已启用）；Bot Token 登录无需手机号验证码。登录成功后程序继续运行，session 保存在项目的 `data/sessions/`。保留有效 session 后，后续启动无需再次登录。

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

网页（含登录页）和 Bot 共用 `lang/*.json` 词典，支持简体中文（`zh-cn`）、繁体中文（`zh-tw`）、英语（`en`）、日语（`ja`）、西班牙语（`es`）、法语（`fr`）、俄语（`ru`）和乌克兰语（`uk`）。每种语言一个文件，共用词汇只翻译一次；英语 `messages` 只列出英文词条，其他语言使用英文词条到翻译的映射。网页可随时切换，首次访问按浏览器偏好依次匹配；`zh` 回退 `zh-cn`，`zh-TW` / `zh-HK` / `zh-Hant` 使用繁体中文，不支持的语言回退英语。手动选择保存在当前浏览器中并优先使用，不依赖 IP 定位。Bot 的 `language` 配置与网页互不影响，例如 `language: zh-tw` 或 `/set_language ja`；旧配置 `ZH` 和命令 `zh` 仍代表简体中文，`UA` / `ua` 仍代表乌克兰语。Bot 语言随运行状态以规范代码写入 `data/data.json`，不会修改 `config.yaml`。运行代码、配置示例注释和 PWA 应用名称使用英语；测试可保留必要的语言数据。失败记录保存英文词条，在 Bot/网页显示时才翻译，无法识别的历史记录保留原文。缺失词条回退英语，再回退原文；原有俄语和乌克兰语保留已有翻译，新增且未翻译的词条使用英语。

添加语言只需复制 `lang/fr.json` 等翻译文件为新语言文件，修改 `code`、`name`、`aliases` 和 `messages`，重启后网页和 Bot 自动识别，无需新增依赖或构建前端。完整步骤及占位符规则见[语言包指南](docs/languages.md)。

文件按 `download_part_size_mb` 切成固定大小的切片，最后一片为剩余大小。
例如每片 64 MiB 时，40 MiB 文件为 1 片，100 MiB 为 2 片，500 MiB 为 8 片。
每个文件从一个连接开始，当前新连接收到并写入首块有效数据后，才允许启动下一个连接，且启动间隔至少一秒，最多达到 `file_download_concurrency` 个 worker。握手或首块数据卡住时不会继续扩展连接；启动失败后必须先关闭该连接并将未完成分片交回队列，再按现有失败预算逐个尝试；已有健康连接继续下载，不必等整片完成才扩展。每个 worker 下载完一片后从队列领取下一片，直到全部完成，再按切片顺序合并。
每个 worker 使用独立的 Telegram 下载 TCP 连接，并在连续下载后续切片时沿用自己的连接；不同 worker 和文件不共用下载连接。
暂停时保留连接，完成、取消或客户端重启时关闭连接。
取消下载先关闭连接，再按文件删除选项处理该文件在 `downloads/.temp/` 中的临时文件和 `.parts/` 目录。活动和失败任务的删除确认只提示是否存在缓存文件（临时文件或分片目录），不列出缓存路径；仅在文件和已记录路径的缓存均不存在时提示未找到。网页取消和删除失败任务默认勾选“同时删除本地文件和缓存”，已完成任务默认不勾选，批量操作一致；不勾选则保留文件和断点。Web API/MCP 的 `delete_file` 默认 `false`，Bot 取消继续默认清理临时数据。旧失败记录若未保存临时路径，仅处理可定位的文件。
文件任务入队时按需启动文件 worker，队列清空后退出，空队列启动不创建文件 worker。调高并发只为等待任务增加 worker；调低时正在下载的文件继续完成，再收缩并发。同时下载的文件数仍由 `max_download_task` 控制；5 个文件各 4 个 worker 时，最多有 20 个并发下载 worker，没有额外的总并发配置。
WebUI、Web API 和 MCP 调整的并发任务数立即生效并保存到 `data/data.json` 的 `max_download_task`，重启后继续使用；`config.yaml` 保持只读。未保存运行值时才使用配置中的初始值（未配置时为 `5`）。需要恢复配置初始值时，先停服并删除 JSON 中的 `max_download_task`。
切片大小和 worker 数在文件首次开始时保存，暂停、重试和程序重启后继续使用该计划，不重复下载已完成切片。
连接下载失败时关闭该连接并将未完成分片交回队列，保留其他健康连接。只要还有可用 worker，单个 worker 失败不会结束文件任务；仅最后一个 worker 连续失败 3 次才算下载失败，不再叠加文件级重试。完成一个分片会重置计数。最后一个 worker 重试使用最多 50% 随机退避，上限 30 秒，前两次等待分别为 10–15 秒和 20–30 秒。磁盘写入和文件计划校验错误不作为连接错误重试。明确的文件引用过期或无效错误会重新获取原消息，并检查文件身份和大小后续传；普通 BadRequest 保留真实异常类型。FLOOD_WAIT / FLOOD_PREMIUM_WAIT 不消耗失败次数，也没有累计等待或 600 秒手动恢复门槛。受限连接关闭并保留断点，冷却期间不创建新连接，已有健康连接继续领取分片；全部连接受限时等待最长的共享冷却截止时间，到期后自动从断点恢复并逐步增加连接。UI 显示共享 FLOOD_WAIT 倒计时；仍可手动暂停、恢复或取消，手动暂停不会因冷却结束而解除。
进度条汇总所有切片的已下载字节，续传恢复的已有字节不计入新传输速度。
WebUI 顶部显示当前下载流数总和，文件卡片、Bot 进度播报和 `/query` 显示各文件的当前下载流数。该数值统计正在执行下载请求（含等待数据）的流，不是配置的 worker 上限；等待启动、空闲、退避、冷却等待和本地合并不计入。暂停的文件显示 0，全局暂停时总数为 0；恢复后继续统计，结束、失败、取消或重启后不会保留旧计数。
Bot 的 `/query` 默认查询全部下载记录与 Pending 任务，每页 6 条，附各分类总数；支持分类、翻页和刷新按钮，也可输入 `/query active`、`/query pending`、`/query completed 2` 或 `/query failed`。下载中列表包含暂停任务。命令和按钮沿用 `allowed_user_ids` 白名单，查询不会修改任务。
Bot 在内容变化时约每 10 秒编辑原进度消息，不再等待 20% 里程碑。FLOOD_WAIT 提示剩余秒数；冷却结束后自动恢复，手动暂停仍保持暂停。若 Bot 消息接口本身被限流，冷却期间停止播报，结束后补报，并保留未送达的完成状态以便重试。单次播报最多等待 10 秒，失败不阻塞下载。
旧版分片断点仍可续传。旧 `download_min_part_size_mb` 在没有新字段时作为切片大小的兼容值读取，不会自动重写配置；旧 `download_concurrency` 已移除。

新下载的临时主文件统一为 `downloads/.temp/timestamp_guid.tmp`，不包含频道名或原文件名。`timestamp` 使用消息时间的 Unix 秒数（无日期时为 `0`），GUID 根据消息所属客户端、聊天 ID、消息 ID 和媒体身份稳定生成；同一媒体重试、恢复和重启复用同一路径，不需要额外的索引文件。分片保存在对应的 `timestamp_guid.tmp.parts/manifest.json` 和分片文件中，SDK/合并过程可能使用 `timestamp_guid.tmp.temp`。已有旧路径断点继续沿用，直至完成或明确删除；最终下载文件名仍由现有命名配置决定。部署时应持久化整个 `downloads/.temp` 目录。
多分片合并期间临时空间最高约为文件大小的两倍，单分片直接移动文件，无需额外完整副本。
离线验证和真实下载验收步骤见 [分片下载测试说明](docs/parallel-download-testing.md)。
普通媒体下载跨 DC 时，首次通过目标 DC 的普通端点创建密钥，同一账号、DC 和测试模式下的后续 worker／文件复用内存中的密钥，并用锁避免重复协商。每个 worker 仍建立自己的媒体会话和 TCP 连接，并执行现有授权流程；当前账号主 DC 继续复用已保存的密钥，CDN 继续使用自己的端点。缓存不跨账号、不写入日志或磁盘；客户端停止／重启时清空。服务端明确拒绝密钥时仅丢弃匹配的旧缓存，下一次尝试重新协商。密钥创建最多等待 60 秒，超时会关闭该连接并保留断点，交给现有连接重试机制；此上限不限制正常文件传输。DEBUG 日志包含媒体端点和密钥协商端点的地址、端口及 CDN 标记，不记录密钥或授权内容。
应用仅写入 `data/log/tdl.log`，每个日志文件达到 10MB 时轮转，保留 30 天；不再生成 `download.log`，已有旧日志不会自动删除。其中的 `Waiting for first data` 记录独立下载连接开始等待数据，包含文件名、worker 编号和 DC；`Opened independent download connection #…` 表示已收到首块数据，`Confirmed independent download startup` 表示首块有效数据已写入，允许增加下一个连接；`Closed independent download connection #…` 表示连接资源已关闭。首块数据前取消也会记录资源释放。设置 `log_level: DEBUG` 可查看 DC 解析、存储读取、密钥创建、会话启动、授权导出/导入和首个媒体请求阶段；等待期间每 30 秒输出当前阶段及该阶段已等待秒数的 WARNING，不改变请求超时或重试策略。SDK 日志级别独立于此设置，应用诊断日志不输出授权内容。

---

## WebUI 登录

列表上方的搜索框按任务 ID 或文件名筛选当前标签，切换标签时保留关键词；清空搜索恢复完整列表。下载中/失败筛选当前完整数据，Pending/已完成由服务端先搜索再分页，不受已加载页数限制。搜索后全选仅选择匹配任务。Downloading 内暂停文件仍保留在原标签，可按状态筛选，并快捷选择全部、下载中或已暂停的匹配任务；显示匹配数和该分类总数。独立的全局暂停按钮已移除，暂停/恢复通过所选任务操作；仅选下载中可批量暂停，仅选已暂停可批量恢复，混选时暂停和恢复均禁用；Pending 的“取消任务”终止尚未开始的队列项，不会启动下载，重新下载需再次提交链接。已完成只能批量删除，失败支持批量重试和删除。首屏请求并行加载；可见页面按轮次刷新，上一轮结束后才安排下一轮。隐藏页面暂停轮询，返回后立即刷新；已发出的请求允许结束。状态/活动任务每轮刷新，Pending/失败至少间隔 5 秒，完成计数至少间隔 10 秒。只读请求最多等待 15 秒，修改操作保持原有等待方式；慢连接可能延长刷新间隔。系统开启“减少动态效果”时关闭装饰动画。

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
docker compose restart tgporter
```

首次部署、更新镜像和从源码构建仍使用前文的主服务步骤。无需额外镜像、容器、端口、环境变量或依赖文件；本地安装只需 `pip install -r requirements.txt`。已有持久化挂载保持不变。

### Agent 客户端

通过 Streamable HTTP 连接服务器的 `/mcp` 地址。支持 `url`、`headers` 的客户端可参考以下格式，具体字段以客户端为准：

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
docker logs -f tgporter
docker restart tgporter
docker stop tgporter
docker start tgporter
```

更新镜像时需使用新镜像重新创建容器，并保留原来的配置、端口和数据挂载；仅重启旧容器不会切换镜像。镜像选择见 [前置条件](#前置条件)。

## 旧镜像迁移与本地构建

旧 package `ghcr.io/smilekevin/hermes-telegram-downloader` 保留历史镜像。迁移时将 Compose 的 `image` 改为新地址，使用新构建的版本或 digest；历史 digest 仍需搭配旧地址。

如需从当前本地源码构建，先克隆仓库并完成配置，将 Compose 服务的 `image` 改为 `tgporter:local` 并添加 `build: .`，然后执行：

```bash
docker-compose build
# 首次登录前停止已有后台服务
docker-compose stop tgporter
docker-compose run --rm tgporter
# 登录成功后 Ctrl+C 退出，再启动后台服务
docker-compose up -d
```

更新本地源码后执行 `docker-compose up -d --build`。首次登录说明见 [Docker Compose 部署](#docker-compose-部署)。

## Docker 数据持久化

| 路径 | 说明 |
|------|------|
| `./downloads/` | 下载的文件 |
| `./config.yaml` | 只读启动配置；程序不会改写 |
| `./data/` | 整体挂载到 `/app/data`，下列运行内容由程序自动创建 |
| `./data/data.json` | 频道扫描位置、重试 ID、广告规则、Bot 语言及并发数 |
| `./data/log/` | 日志 + 任务持久化（bot_tasks.json / task_counter.json / download_history.json） |
| `./data/sessions/` | Telegram 登录会话 |
| `./data/bot.yaml` | Bot 过滤配置，由程序管理 |
| `./downloads/.temp/` | 下载临时文件和 `.parts/` 分片断点，随下载目录持久化 |

程序仅读取 `config.yaml`，扫描、Bot 命令和退出均不会改写它；Docker 示例以 `:ro` 挂载该文件，`data/` 则以目录方式挂载并保持可写。首次启动自动创建 `data/data.json`，无需预建数据文件。JSON 通过锁和临时文件原子替换保存；`chat[].chat_id` 标识频道，`ids_to_retry` 保存重试消息 ID，`last_read_message_id` 保存下一条待扫描位置。启动时取配置初始位置与已保存位置中较大的值。未在本次配置启用的频道记录及其他数据键会保留。

广告相关的 `filter_advertisement_list`、`replace_advertisement_list`、`group_add_advertisement` 和 Bot 的 `language` 保存于 `data/data.json` 顶层。已有配置中的对应值仍作为初始值读取；一旦 `data/data.json` 有对应键，就优先使用该值，包括空列表和空字典。Bot 语言、广告规则和并发设置在修改成功后立即保存，保存完成后才报告成功；网页语言仍只保存在浏览器。需要恢复配置中的初始规则或语言时，先停服并删除 `data/data.json` 中对应的键；不要删除整个文件，以免丢失下载状态。需要从更早的位置重新扫描时，同样应停服后调整该频道的数据检查点和配置初始位置。

只需持久化 `config.yaml`、`data/` 和下载目录。程序会创建 `data/log/`、`data/sessions/` 和下载目录下的 `.temp/`，不需要单独挂载日志、session 或 temp。容器移除或重建不会删除这些宿主机数据；自定义的宿主机下载路径对应表中的 `./downloads/`。

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
5. **session 丢失** — 确保 `./data/sessions/` 目录已正确挂载
6. **FLOOD_WAIT** — 频道消息过多触发了 Telegram 限速，等待冷却后自动恢复

### Bot 心跳与消息诊断

网页顶部自动刷新 Bot 状态，显示连接正常、连接中、已断开、无响应、重连中、限流等待、已停止或未配置，并显示成功心跳的往返延迟和最近检查时间。`GET /get_bot_status` 只读取内存快照，遵循现有 Web 登录保护，不触发额外 Telegram 请求。MCP `get_status` 的 `bot_status` 返回同一快照，使用独立的 MCP Token 鉴权，只读模式也可查询；字段见 [MCP 工具参考](docs/mcp.md#结果与错误)。`connected` 仅表示近期收到匹配的 Pong；心跳启动后立即探测，首次 Pong 返回前显示连接中，不再先等待 60 秒；成功探测超过 70 秒未更新显示未知，网页请求失败也显示未知。未配置 `bot_token`（包括 Mock 模式）显示未配置；该状态只描述 Bot 会话，不代表主下载账号或各下载连接的状态。

配置了 Bot 时，应用会独立于下载和进度通知，在心跳启动时立即发送一次 MTProto Ping，之后每 60 秒发送一次并等待匹配的 Pong，最多等待 10 秒。每轮仅尝试一次；Kurigram 的 `retries=1` 表示总共一次尝试，设为 `0` 不会发送请求，会立即产生超时误报。无需新增配置。Kurigram 自身约每 5 秒发送一次不等待回复的保活 Ping；应用心跳额外验证 Telegram 确实返回了响应。连续 3 次失败会尝试重连 Bot，成功重启后 300 秒内不再重连；重启失败则最早在 60 秒后重试。已有的进度通知连接错误阈值也由该独立循环处理。Bot 的已知 FLOOD_WAIT 期间暂停探测；探测遇到 FLOOD_WAIT 时记录并遵守冷却，不计为网络失败。关闭服务时取消并等待心跳任务；无 Bot 模式不启动心跳。

使用项目默认容器名查看日志：

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

这些日志同时写入 `data/log/tdl.log`，遵守 `log_level`、轮转和保留设置。`INFO` 可看到 `Bot heartbeat OK`、往返耗时 `rtt_ms`、本地连接标记 `connected`、待处理更新数 `update_queue`、存活处理 worker 数 `workers_alive`、活动处理数 `active_handlers`、最长处理耗时和函数名，以及距离上次收到消息的秒数。`Bot heartbeat failed` 包含异常类型、等待耗时与连续失败数；重连尝试、结果与冷却也会记录；连接恢复显示 `Bot heartbeat recovered`，事件循环延迟超过 10 秒显示 `event_loop_delay_s`。消息入口日志 `Bot message received` 显示消息编号、类型、`authorized` 与消息发送至今的秒数 `message_age_s`，随后 `Bot handler started` / `Bot handler finished` 显示匹配处理函数、完成状态与耗时。日志不记录消息正文、命令参数、回调数据或 Token。

`message.forward_from_chat`、`message.forward_from_message_id` 和发送消息时的 `reply_to_message_id` 弃用警告不是网络断线证据。Bot 已改用 Kurigram 的 `forward_origin` 和 `ReplyParameters`，保留频道来源、Bot 私聊回退和回复目标。

Pong 证明 Bot 会话能与 Telegram 往返通信，不能证明消息解析、授权和命令回复均正常。心跳正常但 `/help` 无回复时，先看 `authorized=False`，再检查消息入口日志、`update_queue`、`workers_alive` 和长时间未完成的 handler。长期没有心跳日志也可能是下载事件循环阻塞、进程停止或日志级别过滤；WebUI 正常并不能排除这些情况。心跳不向用户发送测试消息，不创建额外 Telegram session，也不重启主下载客户端。

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

The WebUI (including sign-in) and Bot share `lang/*.json` catalogs: Simplified Chinese (`zh-cn`), Traditional Chinese (`zh-tw`), English (`en`), Japanese (`ja`), Spanish (`es`), French (`fr`), Russian (`ru`) and Ukrainian (`uk`). Each language has one file and shared vocabulary is translated once. English `messages` lists canonical keys; other languages map those keys to translations. The browser matches language preferences in order; `zh` falls back to `zh-cn`, while `zh-TW` / `zh-HK` / `zh-Hant` select Traditional Chinese. Unsupported languages fall back to English. Manual choices are saved in the current browser and take priority; no IP geolocation is used. Configure the Bot independently, for example `language: zh-tw` or `/set_language ja`. Legacy `ZH` / `zh` still mean Simplified Chinese and `UA` / `ua` still mean Ukrainian. Bot language is saved with runtime state in `data/data.json` using canonical codes, leaving `config.yaml` unchanged. Runtime code, configuration example comments and the PWA app name use English; tests may retain required language fixtures. Failure records store English keys and translate when displayed by the Bot or browser; unrecognized historical records retain their original text. Missing messages fall back to English, then the original text; existing Russian and Ukrainian translations are preserved, with English fallback for untranslated additions.

To add a language, copy a translated catalog such as `lang/fr.json` to a new language file, edit `code`, `name`, `aliases` and `messages`, then restart. Both Bot and browser discover it automatically; no extra dependency or frontend build is required. See the [catalog guide](docs/languages.md) for steps and placeholder rules.

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
See the [download test guide](docs/parallel-download-testing.md) for offline and live acceptance checks.
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

The dashboard header automatically refreshes Bot health, showing connected, connecting, disconnected, unresponsive, reconnecting, rate limited, stopped or not configured, with successful heartbeat round-trip latency and the last check time. `GET /get_bot_status` reads an in-memory snapshot under the existing Web login protection and sends no additional Telegram requests. MCP `get_status.bot_status` returns the same snapshot under the independent MCP token authentication, including in read-only mode; see the [MCP tool reference](docs/mcp.md#mcp-tool-reference) for fields. `connected` requires a recent matching Pong; the heartbeat probes immediately on startup and shows connecting until the first Pong arrives, without an initial 60-second wait; a successful probe older than 70 seconds becomes unknown, and failed Web reads also show unknown. Without `bot_token` (including Mock mode), it shows not configured. This describes the Bot session only, independently of the main account and media connections.

When a Bot is configured, an independent loop sends an MTProto Ping immediately when the heartbeat starts, then every 60 seconds, and waits up to 10 seconds for a matching Pong. Each probe makes one attempt: Kurigram counts total attempts in `retries`, so `retries=1` sends once while `0` sends nothing and immediately reports a false timeout. No new configuration is required. Kurigram also sends its own keepalive Ping about every 5 seconds without waiting for a response; the application probe additionally verifies that Telegram responds. Three consecutive failures trigger a Bot reconnect attempt. A successful restart has a 300-second reconnect cooldown; a failed restart can retry after 60 seconds. The same independent loop handles the existing progress-report connection-error threshold. Probes defer during a known Bot FLOOD_WAIT; a FLOOD_WAIT from the probe records and respects the cooldown instead of counting as a network failure. Shutdown cancels and awaits the heartbeat; operation without a Bot does not start it.

For the default container name:

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

The same events go to `data/log/tdl.log` under the configured `log_level`, rotation and retention. At `INFO`, `Bot heartbeat OK` reports `rtt_ms`, the local `connected` flag, pending `update_queue`, `workers_alive`, `active_handlers`, the oldest handler's age and function name, and seconds since the last received message. `Bot heartbeat failed` includes the exception type, elapsed time and consecutive failure count. Reconnect attempts, outcomes and cooldowns are logged too; recovery emits `Bot heartbeat recovered`, and event-loop delays of at least 10 seconds report `event_loop_delay_s`. `Bot message received` records the message ID, type, `authorized` result and age since sending (`message_age_s`), followed by `Bot handler started` / `Bot handler finished` with the matching function, outcome and elapsed time. Logs exclude message bodies, command arguments, callback data and tokens.

Deprecation warnings for `message.forward_from_chat`, `message.forward_from_message_id` or the `reply_to_message_id` sending argument do not indicate network loss. The Bot now uses Kurigram `forward_origin` and `ReplyParameters`, preserving channel origins, Bot-inbox fallback and reply targets.

A Pong confirms round-trip communication on the Bot session; it does not prove message parsing, authorization or command replies work. If heartbeats succeed but `/help` does not respond, check for `authorized=False`, message receipt logs, `update_queue`, `workers_alive` and handlers that never finish. Missing heartbeats can also indicate a blocked downloader event loop, a stopped process or log-level filtering; a responsive WebUI does not rule these out. Probes send no test messages to users, create no additional Telegram sessions and do not restart the main download client.
