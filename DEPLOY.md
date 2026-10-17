# TG Porter - 部署指南

[English deployment guide](./DEPLOY_EN.md)

安装、配置、登录和运维步骤统一维护在本文件。功能介绍、Bot 命令、Web API 和架构见 [README 中文说明](https://github.com/smilekevin/TGPorter/blob/main/README.md#功能特性)。

中文：[前置条件](#前置条件) · [凭据准备](#部署前准备api-凭据与-bot-token) · [Docker 镜像](#docker-镜像部署) · [Compose](#docker-compose-部署) · [手动安装](#手动安装) · [本地开发](#本地开发模式) · [配置说明](#配置说明) · [WebUI 登录](#webui-登录) · [MCP](#mcp-agent-接入) · [运维](#docker-管理) · [持久化](#docker-数据持久化) · [代理](#代理配置) · [故障排查](#故障排查)

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

打开后，在右上角语言菜单选择 **English**，即可预览 [README 中的英文主界面](https://github.com/smilekevin/TGPorter/blob/main/README.md#webui-preview)。README 提供 2560 × 1440 高清截图，来自本地 Mock 模式，下载数据和 Bot 已连接状态均为演示，不执行真实下载。

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
| `api_id` | 必填，Telegram API ID（[my.telegram.org](https://my.telegram.org/apps)） | — |
| `api_hash` | 必填，Telegram API Hash | — |
| `bot_token` | 命令交互和通知 Bot 的 Token（[@BotFather](https://t.me/BotFather)）；MCP 独立运行时可留空 | `""` |
| `media_types` | 必填，下载的媒体类型列表；例如 `audio`、`document`、`photo`、`video`、`voice`、`animation`、`video_note` | — |
| `file_formats` | 必填，为 `audio`、`document`、`video` 设置非空格式列表；首项为 `all` 时接受该类型的所有格式，示例仅下载 PDF/EPUB 文档和 MP4 视频 | — |
| `save_path` | 文件保存路径 | `./downloads` |
| `file_path_prefix` | 子目录字段，按列表顺序组合；支持 `chat_title`、`media_datetime`、`media_type`；省略、`null` 或空列表使用默认规则 | `[chat_title, media_datetime]` |
| `file_name_prefix` | 文件名字段，按列表顺序组合；支持 `message_id`、`file_name`、`caption`；缺失字段跳过，全空时退回消息 ID；省略、`null` 或空列表使用默认规则 | `[message_id, file_name]` |
| `file_name_prefix_split` | 文件名各字段之间的分隔符 | `" - "` |
| `chat` | 可选的启动历史扫描配置列表，嵌套字段见下表；仅通过 MCP 提交单条任务时可省略 | 不扫描 |
| `upload_drive` | 可选云盘上传设置，嵌套字段见下表 | 关闭 |
| `max_download_task` | 并发下载 worker 数 | `5` |
| `file_download_concurrency` | 单文件并发下载 worker 数 | `1` |
| `download_part_size_mb` | 固定切片大小，单位 MiB，必须为正整数 | `64` |
| `web_host` | WebUI/API/MCP 共用的监听地址；Docker 内通常保持 `0.0.0.0` | `0.0.0.0` |
| `web_port` | WebUI/API/MCP 共用的监听端口 | `5000` |
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

#### 嵌套配置与可选功能

`chat` 是对象列表；`upload_drive` 和 `proxy` 是映射。以下字段按层级填写，不是新的顶层配置。仅在使用对应功能时配置：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `chat[].chat_id` | 扫描的聊天/频道 ID 或可解析的名称；每条扫描配置需要此字段 | — |
| `chat[].last_read_message_id` | 扫描起点的已读消息 ID；运行数据中更靠后的检查点优先 | `0` |
| `chat[].download_filter` | 此聊天的下载过滤表达式；空字符串不额外过滤 | `""` |
| `chat[].upload_telegram_chat_id` | 下载后重新上传的 Telegram 目标；需有发送权限 | `null` |
| `upload_drive.enable_upload_file` | 开启下载后的云盘上传 | `false` |
| `upload_drive.rclone_path` | rclone 可执行文件路径；主镜像已内置到默认路径，手动安装需另行准备，启用前配置 remote | `./rclone/rclone`，Windows 带 `.exe` |
| `upload_drive.remote_dir` | 云盘目标，例如 `drive:/telegram` | `""` |
| `upload_drive.before_upload_file_zip` | 上传前将单个文件压缩为 ZIP | `false` |
| `upload_drive.after_upload_file_delete` | 云盘上传成功后删除本地原文件；当前加载器只接受真值，配置 `false` 不能覆盖默认 `true` | `true` |
| `upload_drive.upload_adapter` | 上传适配器；默认 `rclone`；旧 `aligo` 适配器需要额外安装与登录，主依赖未包含它 | `rclone` |
| `proxy.scheme` | Telegram 代理协议，例如 `http` 或 `socks5`；不是 Compose 的环境变量代理 | — |
| `proxy.hostname` | Telegram 代理主机，不含协议 | — |
| `proxy.port` | Telegram 代理端口，整数 | — |
| `proxy.username` / `proxy.password` | 代理认证信息，仅在代理要求时填写 | 不使用认证 |

#### 高级与兼容配置

以下选项通常无需填写。广告相关配置只是初始值，已有 `data/data.json` 中的广告规则优先；Bot 修改立即保存运行数据。`drop_no_audio_video` 当前只读取配置，没有对应的执行逻辑，不应依赖它过滤媒体。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `start_timeout` | Telegram 主会话启动超时，单位秒；不是正常媒体传输的超时 | `60` |
| `max_concurrent_transmissions` | SDK 传输容量覆盖值，通常应省略；保存的并发值及 WebUI/API/MCP 并发调整会重新计算它 | `max_download_task * max(5, file_download_concurrency)` |
| `debug_web` | Flask 调试状态及 ASGI 调试日志 | `false` |
| `forward_limit` | 转发调用的每 60 秒本地预算；不覆盖 Telegram 自身的 FLOOD_WAIT | `33` |
| `after_upload_telegram_delete` | Telegram 重新上传流程结束后删除本地文件；清理位于 `finally`，也会在上传异常后执行 | `true` |
| `filter_advertisement_list` | 转发时跳过匹配内容的关键词列表 | `[]` |
| `replace_advertisement_list` | 从转发说明文字中删除的文本列表 | `[]` |
| `group_add_advertisement` | 按目标聊天 ID 映射的追加说明文字 | `{}` |
| `drop_no_audio_video` | 已读取但目前不参与媒体过滤的兼容字段 | `false` |
| `restart_program` | 内部运行控制标记；`true` 会使主等待循环退出，正常部署保持关闭 | `false` |
| `download_min_part_size_mb` | 旧分片大小字段，仅在未填写 `download_part_size_mb` 时读取 | `64` |
| `chat_id` | 旧版单聊天扫描入口，新配置使用 `chat` 列表 | 未设置 |
| `last_read_message_id` | 配合顶层 `chat_id` 使用的旧扫描起点；旧格式需要提供 | — |
| `download_filter` | 旧版按聊天 ID 映射的表达式；新配置使用 `chat[].download_filter` | 未设置 |
| `ids_to_retry` | 旧版单聊天重试消息 ID 列表；新任务通过共享队列和运行数据恢复 | `[]` |

`api_id`、`api_hash`、`media_types` 和 `file_formats` 必须存在；MCP 开启时还必须提供非空 `mcp_token`。布尔选项使用 YAML 的 `true` / `false`，不要写成带引号的字符串。示例值不等于默认值：示例 `file_download_concurrency` 为 `4`、`file_name_prefix` 仅用 `file_name`、`date_format` 为空；代理地址也只是环境示例。`config.yaml` 修改后需重启，运行中的任务状态、语言、广告规则和并发值通过应用接口管理，不直接编辑运行 JSON。

网页（含登录页）和 Bot 共用 `lang/*.json` 词典，支持简体中文（`zh-cn`）、繁体中文（`zh-tw`）、英语（`en`）、日语（`ja`）、西班牙语（`es`）、法语（`fr`）、俄语（`ru`）和乌克兰语（`uk`）。每种语言一个文件，共用词汇只翻译一次；英语 `messages` 只列出英文词条，其他语言使用英文词条到翻译的映射。网页可随时切换，首次访问按浏览器偏好依次匹配；`zh` 回退 `zh-cn`，`zh-TW` / `zh-HK` / `zh-Hant` 使用繁体中文，不支持的语言回退英语。手动选择保存在当前浏览器中并优先使用，不依赖 IP 定位。Bot 的 `language` 配置与网页互不影响，例如 `language: zh-tw` 或 `/set_language ja`；旧配置 `ZH` 和命令 `zh` 仍代表简体中文，`UA` / `ua` 仍代表乌克兰语。Bot 语言随运行状态以规范代码写入 `data/data.json`，不会修改 `config.yaml`。运行代码、配置示例注释和 PWA 应用名称使用英语；测试可保留必要的语言数据。失败记录保存英文词条，在 Bot/网页显示时才翻译，无法识别的历史记录保留原文。缺失词条回退英语，再回退原文；原有俄语和乌克兰语保留已有翻译，新增且未翻译的词条使用英语。

添加语言只需复制 `lang/fr.json` 等翻译文件为新语言文件，修改 `code`、`name`、`aliases` 和 `messages`，重启后网页和 Bot 自动识别，无需新增依赖或构建前端。完整步骤及占位符规则见[语言包指南](https://github.com/smilekevin/TGPorter/blob/main/docs/languages.md)。

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
离线验证和真实下载验收步骤见 [分片下载测试说明](https://github.com/smilekevin/TGPorter/blob/main/docs/parallel-download-testing.md)。
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

MCP 集成在下载器主服务中，复用主镜像、容器和 `web_port`（默认 `5000`），入口为 `/mcp`。工具直接调用内部任务管理逻辑。[工具与参数参考](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md)。13 个工具支持提交和管理下载任务；`submit_download` 接受单条 Telegram 消息链接，持久化到现有 Pending 队列。

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

`mcp_read_only: true` 只暴露三个查询工具，不开放提交下载。修改 Token 后重启下载器，并同步更新 Agent 配置，旧 Token 随即失效。默认允许写操作，删除工具的 `delete_file` 默认 `false`。[工具列表与删除语义](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md)。

### 安装可选 Skill

仓库提供 [tgporter Skill](https://github.com/smilekevin/TGPorter/blob/main/skills/tgporter/SKILL.md)，用于引导部署、连接 MCP 和管理下载，面向支持 `SKILL.md` 的 Agent 客户端。通过 MCP 管理任务还需要客户端支持 Streamable HTTP 和预配置 Bearer Token；Skill 不绑定特定厂商的客户端、插件格式或安装器。

从本仓库源码或 Release 源码包中取得整个 `skills/tgporter/` 目录，保留 `SKILL.md` 和 `references/` 的相对位置。按所用客户端的文档导入该目录，或放入该客户端指定的 Skill 目录。若客户端支持通过 GitHub URL 导入，可使用 [Skill 目录链接](https://github.com/smilekevin/TGPorter/tree/main/skills/tgporter)。需要固定版本时，将 URL 中的 `main` 替换为包含 Skill 的 Release 标签或提交号；更新已有 Skill 前保留自己的修改。

推荐从 [GitHub Release](https://github.com/smilekevin/TGPorter/releases) 的附件下载 **`TGPorterSkills.zip`**，直接交给能够读取 ZIP 的 Agent；需要目录导入的客户端先解压，再导入其中的 `tgporter/`。包内仅包含 Skill、英文部署与配置参数指南 `DEPLOY_EN.md`、配置/Compose 示例、版本信息及许可证；部署所需说明可在包内读取，Agent 仍可用用户的语言解释。中文指南、开发指引、测试计划、设计草案、README 和截图不打包。依赖安装、拉取镜像和 Telegram 登录仍需网络与用户自己的凭据。

Release 工作流在镜像推送成功后生成并上传该 ZIP，重跑时覆盖同名附件。`release.json` 集中记录标签、镜像、digest、源码提交和下载 URL，并包含各文件 SHA-256；`references/release.md` 从同一份版本数据生成准确的镜像拉取命令、源码检出命令及下载地址。Compose 通过 YAML 字段设置该 Release 的镜像。Skill、部署指南和配置示例原样打包，不按标题、命令文本或 URL 格式执行替换。Agent 部署前先读包内 Release 信息，使用其中的版本值和命令，再参考本指南中的通用步骤及参数说明；本指南的 `latest` / `main` 示例不代表该 ZIP 的固定版本。部署所需说明可本地读取，其他可选参考仍为公开链接；MCP 实例 URL 由用户配置。

同仓库 PR、合并后的 `main` 和手动 CI 在验证镜像发布成功后，也会把 **`TGPorterSkills.zip`** 上传到该次 Actions 的 **Artifacts**，并在 **Agent skill CI preview** Summary 中提供下载链接。预览包绑定实际发布的 `sha-<完整提交号>` 镜像及 digest，源码和配置下载 URL 固定到同一提交；`release.json` 的 `kind` 为 `ci`，不生成不存在的 Release 标签链接。附件需登录 GitHub 下载，保留 14 天。Fork 和 Dependabot PR 不发布镜像，也不上传可部署预览包，但仍运行离线打包测试。

安装位置、加载或刷新方式、调用语法以客户端文档为准。例如可请求“使用 tgporter Skill 帮我部署并连接 MCP”或“使用 tgporter Skill 查询失败任务”；这些是使用意图示例，不是统一的客户端命令。

安装 Skill 不会自动部署服务或配置 MCP。按前文在客户端配置自己的实例地址和 Token；Skill 不包含公共服务地址或真实凭据。部署引导会读取对应版本的本地文档，或访问 GitHub 上的部署指南；无本地文档时需要能够访问 GitHub。客户端缺少部署所需的文件或终端操作能力时，可按照引导手动完成；日常下载管理通过客户端发现并检查已连接 MCP 的工具描述、参数 Schema 和注解，以其定义和实际返回结果为准，无需读取 GitHub 工具参考。批量大小和分页限制由实际 Schema 决定。

Skill 随本仓库源码和版本标签分发；维护者发布包含该目录的提交或 Release 后，用户即可取得并导入，无需另建 Skill 仓库。各客户端的插件打包或市场上架属于可选的分发方式。

维护者可在仓库根目录、已按[手动安装](#手动安装)安装 `requirements.txt` 的 Python 3.14 虚拟环境中预览发布包（无需真实 Telegram 凭据，也不会发布 Release）：

```bash
python tools/build_skill.py --tag v1.2.3 --commit "$(git rev-parse HEAD)" --output-dir dist
```

正式 Release 由工作流传入实际标签、源码提交和已发布镜像 digest；本地命令中的 `v1.2.3` 只是预览示例。

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

网页顶部自动刷新 Bot 状态，显示连接正常、连接中、已断开、无响应、重连中、限流等待、已停止或未配置，并显示成功心跳的往返延迟和最近检查时间。`GET /get_bot_status` 只读取内存快照，遵循现有 Web 登录保护，不触发额外 Telegram 请求。MCP `get_status` 的 `bot_status` 返回同一快照，使用独立的 MCP Token 鉴权，只读模式也可查询；字段见 [MCP 工具参考](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#结果与错误)。`connected` 仅表示近期收到匹配的 Pong；心跳启动后立即探测，首次 Pong 返回前显示连接中，不再先等待 60 秒；成功探测超过 70 秒未更新显示未知，网页请求失败也显示未知。未配置 `bot_token`（包括 Mock 模式）显示未配置；该状态只描述 Bot 会话，不代表主下载账号或各下载连接的状态。

配置了 Bot 时，应用会独立于下载和进度通知，在心跳启动时立即发送一次 MTProto Ping，之后每 60 秒发送一次并等待匹配的 Pong，最多等待 10 秒。每轮仅尝试一次；Kurigram 的 `retries=1` 表示总共一次尝试，设为 `0` 不会发送请求，会立即产生超时误报。无需新增配置。Kurigram 自身约每 5 秒发送一次不等待回复的保活 Ping；应用心跳额外验证 Telegram 确实返回了响应。连续 3 次失败会尝试重连 Bot，成功重启后 300 秒内不再重连；重启失败则最早在 60 秒后重试。已有的进度通知连接错误阈值也由该独立循环处理。Bot 的已知 FLOOD_WAIT 期间暂停探测；探测遇到 FLOOD_WAIT 时记录并遵守冷却，不计为网络失败。关闭服务时取消并等待心跳任务；无 Bot 模式不启动心跳。

使用项目默认容器名查看日志：

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

这些日志同时写入 `data/log/tdl.log`，遵守 `log_level`、轮转和保留设置。`INFO` 可看到 `Bot heartbeat OK`、往返耗时 `rtt_ms`、本地连接标记 `connected`、待处理更新数 `update_queue`、存活处理 worker 数 `workers_alive`、活动处理数 `active_handlers`、最长处理耗时和函数名，以及距离上次收到消息的秒数。`Bot heartbeat failed` 包含异常类型、等待耗时与连续失败数；重连尝试、结果与冷却也会记录；连接恢复显示 `Bot heartbeat recovered`，事件循环延迟超过 10 秒显示 `event_loop_delay_s`。消息入口日志 `Bot message received` 显示消息编号、类型、`authorized` 与消息发送至今的秒数 `message_age_s`，随后 `Bot handler started` / `Bot handler finished` 显示匹配处理函数、完成状态与耗时。日志不记录消息正文、命令参数、回调数据或 Token。

`message.forward_from_chat`、`message.forward_from_message_id` 和发送消息时的 `reply_to_message_id` 弃用警告不是网络断线证据。Bot 已改用 Kurigram 的 `forward_origin` 和 `ReplyParameters`，保留频道来源、Bot 私聊回退和回复目标。

Pong 证明 Bot 会话能与 Telegram 往返通信，不能证明消息解析、授权和命令回复均正常。心跳正常但 `/help` 无回复时，先看 `authorized=False`，再检查消息入口日志、`update_queue`、`workers_alive` 和长时间未完成的 handler。长期没有心跳日志也可能是下载事件循环阻塞、进程停止或日志级别过滤；WebUI 正常并不能排除这些情况。心跳不向用户发送测试消息，不创建额外 Telegram session，也不重启主下载客户端。
