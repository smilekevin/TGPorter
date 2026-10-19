# TG Porter - 部署指南

[English deployment guide](./DEPLOY.md)

功能、Bot 命令和 Web API 见 [README](./README.md)。优先使用网页 **Configuration** 交互配置与登录（首选），首次用启动日志中的初始化码设置管理密码；`config.yaml`（可选），命令行交互登录需显式添加 `--login`。

## 前置条件

- Docker；使用 Compose 时需要 Docker Compose 插件。手动安装需要 Python 3.14。
- Telegram API ID、API Hash 和用于基础配置的 Bot Token；使用用户账号下载时，还需要该账号的手机号。
- 可访问 Telegram 的网络；需要代理时在网页填写，默认不启用代理。

Docker 镜像已包含 MCP 和全部运行依赖。手动安装使用 `requirements.txt`，不要额外混装 Pyrogram 或 Pyrofork。升级旧手动安装时，建议重新创建虚拟环境。

镜像为 `ghcr.io/smilekevin/tgporter`，支持 amd64 和 arm64。

升级前停服并备份 `data/`、`downloads/`，包括 `.temp/`。Kurigram 会原地升级旧 session 数据库；回滚 SDK 时需要恢复升级前的 session 备份。不要让两个服务实例同时使用同一份 session 和任务数据。

## 快速开始

### 部署前准备：API 凭据与 Bot Token

1. 在 [my.telegram.org](https://my.telegram.org) 登录已有账号，打开 [API development tools](https://my.telegram.org/apps) 创建应用，取得 API ID 和 API Hash。[Telegram 官方说明](https://core.telegram.org/api/obtaining_api_id)。
2. 在 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 并保存返回的 Token。[BotFather 说明](https://core.telegram.org/bots/features#botfather)。
3. 启动 TG Porter 后将凭据填入 Configuration。基础配置必须填写 API ID、API Hash 和 Bot Token，默认以 Bot 账号下载，也可勾选使用用户账号。

### Docker 镜像部署

先指定两个可写的宿主机绝对路径，替换以下所有命令和 Compose 示例中的占位符，再启动：

| 宿主机路径占位符 | 用途 | 容器内路径 |
| --- | --- | --- |
| `/path/to/tgporter/data` | TG Porter 配置、session、日志和任务数据目录 | `/app/data` |
| `/path/to/downloads` | 下载目录，包含临时文件与断点 | `/app/downloads` |

例如可使用 `/volume1/docker/tgporter/data` 和 `/volume1/downloads/telegram`。两个目录可以位于不同位置，不依赖当前工作目录，也不要求放在仓库内。升级或重新登录时继续使用原目录。

```bash
mkdir -p "/path/to/tgporter/data" "/path/to/downloads"
docker pull ghcr.io/smilekevin/tgporter:latest
docker run -d \
  --name tgporter \
  --restart unless-stopped \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v "/path/to/tgporter/data:/app/data" \
  -v "/path/to/downloads:/app/downloads" \
  ghcr.io/smilekevin/tgporter:latest
docker logs tgporter
```

打开 `http://服务器IP:5000/configuration`，从日志中找到 `First-time setup code`，按[首次设置与登录](#webui-登录)完成初始化。不需要交互终端，也不需要预建任何数据文件。

### Docker Compose 部署

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
```

先编辑 `docker-compose.yaml`，把 `volumes` 左侧的两个宿主机路径占位符替换为自己选择的绝对路径。仓库的 `docker-compose.yaml` 默认如下，`config.yaml` 挂载已注释，不是启动必需项；默认使用网页配置，未配置示例代理：

```yaml
services:
  tgporter:
    image: ghcr.io/smilekevin/tgporter:latest
    container_name: tgporter
    network_mode: bridge
    ports:
      - "5000:5000"
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - "/path/to/downloads:/app/downloads/"
      - "/path/to/tgporter/data:/app/data"
      # Optional first-start import only; web Configuration is preferred.
      # - "/path/to/config.yaml:/app/config.yaml:ro"
    restart: unless-stopped
```

修改并确认宿主机路径后，创建对应目录并启动（下面的目录也要替换为同样的实际路径）：

```bash
mkdir -p "/path/to/tgporter/data" "/path/to/downloads"
docker compose pull
docker compose up -d
docker compose logs tgporter
```

打开 `http://服务器IP:5000/configuration` 完成设置和 Telegram 登录。挂载冒号左侧是宿主机实际路径，右侧是容器内路径；网页中的下载路径填写容器内的 `/app/downloads`。更改容器监听端口时，要同步更改 Compose 端口映射；宿主机挂载和端口映射仍由 Docker 管理。

### 手动安装

```bash
git clone https://github.com/smilekevin/TGPorter.git
cd TGPorter
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python media_downloader.py
```

打开 `http://localhost:5000/configuration`，使用启动日志的初始化码设置管理密码，然后填写 API 凭据并登录 Telegram。下载路径默认是项目目录下的 `downloads/`。后续启动会复用保存的配置和有效 session。

### 本地开发模式

仅预览使用示例数据的仪表盘时，可运行：

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install Flask Werkzeug
python run_local.py
```

Mock 不连接 Telegram，也不保存设置。需要测试 Configuration 和登录时，请运行[真实应用](#手动安装)。

## 配置说明

### 网页配置与交互登录（首选）

通过页头图标切换下载页和 Configuration；**退出**用于结束网页登录。[界面截图](./README.md#webui-preview)见 README。

通过 **Configuration** 管理 Telegram 登录、下载、网页访问和 MCP。设置保存到 `data/settings.json`，请勿在服务运行时手动编辑。已保存的密码和 Token 留空即可保留原值。Telegram 验证码和两步验证密码不会保存。

### config.yaml (可选)

也可以在首次启动前复制 [config.yaml.example](./config.yaml.example) 为工作目录中的 `config.yaml`，按下方参数表填写 API 凭据、账号模式、下载设置及所需代理：

```bash
cp config.yaml.example config.yaml
```

请在首次启动前准备 YAML。仅当 `data/settings.json` 不存在时，程序才导入一次；后续修改请用 Configuration。已有设置优先，YAML 文件保持不变。不要删除 settings、session 或任务文件来强制重新导入。

Docker 默认不挂载 YAML。如需首次导入，在现有 `docker run` 命令中额外添加 `-v "/path/to/config.yaml:/app/config.yaml:ro"`；Compose 则取消 `tgporter.volumes` 下 `config.yaml` 挂载行的注释。先确认宿主机文件存在，仍保留原有 `data/` 和 `downloads/` 挂载。导入后可重新注释该可选挂载。

使用 YAML 不会自动启用命令行提示，仍默认在网页交互登录。只有显式添加 `--login` 才使用[命令行登录](#命令行登录)。`use_user_account: true` 选择用户手机号账号，`false` 使用 Bot Token 作为下载账号；示例文件和下方参数表说明各配置项。

### 配置参数说明

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `api_id` | Telegram API ID；完成账号登录时需要 | `0` |
| `api_hash` | Telegram API Hash；不回显保存值 | `""` |
| `proxy` | 代理配置（scheme / hostname / port） | `{}` |
| `bot_token` | 基础配置必填；用于命令/通知 Bot，默认也作为下载账号（[@BotFather](https://t.me/BotFather)） | `""` |
| `use_user_account` | 勾选后使用用户手机号登录下载，不勾选则使用 Bot | `false` |
| `allowed_user_ids` | 允许使用 Bot 命令的用户 ID；用户下载账号自动获准，旧 `me` 在 Bot 模式下不能授权真人 | `[]` |
| `language` | Bot 与日志语言；与浏览器语言独立，立即保存生效 | `"en"` |
| `max_download_task` | 同时下载文件数，1–6，修改后立即保存并生效 | `5` |
| `file_download_concurrency` | 单文件并发下载 worker 数 | `4` |
| `download_part_size_mb` | 固定切片大小，单位 MiB，必须为正整数 | `64` |
| `save_path` | 文件保存路径 | `"./downloads"` |
| `media_types` | 下载的媒体类型列表 | `["audio", "document", "photo", "video", "voice", "animation"]` |
| `file_formats` | audio/document/video 的扩展名列表，all 接受全部格式 | `{"audio": ["all"], "document": ["pdf", "epub"], "video": ["mp4"]}` |
| `file_path_prefix` | 目录命名字段，空列表不添加目录字段 | `["chat_title", "media_datetime"]` |
| `file_name_prefix` | 文件命名字段，空列表使用消息 ID 回退 | `["file_name"]` |
| `file_name_prefix_split` | 文件名各字段之间的分隔符 | `" - "` |
| `hide_file_name` | 隐藏文件名（UI 显示 `****.mp4`） | `false` |
| `date_format` | 媒体日期目录格式；仅勾选目录命名中的“媒体日期”后显示，取消勾选保留格式 | `"%Y_%m"` |
| `drop_no_audio_video` | 已读取但目前不参与媒体过滤的兼容字段 | `false` |
| `enable_download_txt` | 纯文本消息保存为 .txt | `false` |
| `chat` | 可选的启动历史扫描配置列表，嵌套字段见下表；仅通过 MCP 提交单条任务时可省略 | `[]` |
| `filter_advertisement_list` | 转发时跳过匹配内容的关键词列表 | `[]` |
| `replace_advertisement_list` | 从转发说明文字中删除的文本列表 | `[]` |
| `group_add_advertisement` | 按目标聊天 ID 映射的追加说明文字 | `{}` |
| `forward_limit` | 转发调用的每 60 秒本地预算；不覆盖 Telegram 自身的 FLOOD_WAIT | `33` |
| `after_upload_telegram_delete` | Telegram 重新上传结束后删除本地文件，上传失败时也会删除 | `true` |
| `web_login_secret` | 首次设置时创建的网页管理密码；公共模式也保护 Configuration | `""` |
| `public_web` | 仅公开仪表盘和下载 API；配置页仍需管理密码 | `false` |
| `web_host` | WebUI/API/MCP 共用的监听地址；Docker 内通常保持 `0.0.0.0` | `"0.0.0.0"` |
| `web_port` | WebUI/API/MCP 共用的监听端口 | `5000` |
| `mcp_enabled` | 开启同端口的 `/mcp` Streamable HTTP 入口，始终要求 Token | `false` |
| `mcp_token` | 仅供 `/mcp` 使用的 Bearer Token；可打印 ASCII 字符串，不含空白，MCP 开启时不能为空 | `""` |
| `mcp_read_only` | MCP 仅暴露查询工具，不影响独立的网页登录会话 | `false` |
| `mcp_allowed_hosts` | 额外允许的 Host，域名或 IP，可带端口；默认已允许 localhost / loopback | `[]` |
| `log_level` | 高级设置下拉选择：`TRACE`、`DEBUG`、`INFO`、`SUCCESS`、`WARNING`、`ERROR`、`CRITICAL`；保存后需重启 | `"INFO"` |
| `start_timeout` | Telegram 主会话启动超时，单位秒；不是正常媒体传输的超时 | `60` |
| `max_concurrent_transmissions` | 0 自动计算；非零为 SDK 传输容量覆盖值 | `0` |
| `upload_drive` | 可选云盘上传设置，嵌套字段见下表 | `{}` |
| `debug_web` | Flask 调试状态及 ASGI 调试日志 | `false` |

#### 嵌套配置

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `chat[].chat_id` | 扫描的聊天/频道 ID 或可解析的名称；每条扫描配置需要此字段 | — |
| `chat[].last_read_message_id` | 扫描起点的已读消息 ID；运行数据中更靠后的检查点优先 | `0` |
| `chat[].download_filter` | 此聊天的下载过滤表达式；空字符串不额外过滤 | `""` |
| `chat[].upload_telegram_chat_id` | 下载后重新上传的 Telegram 目标；需有发送权限 | `null` |
| `upload_drive.enable_upload_file` | 开启下载后的云盘上传 | `false` |
| `upload_drive.rclone_path` | rclone 可执行文件路径；主镜像已内置到默认路径，手动安装需另行准备，启用前配置 remote | `./rclone/rclone`，Windows 带 `.exe` |
| `upload_drive.remote_dir` | 云盘目标，例如 `drive:/telegram` | `""` |
| `upload_drive.before_upload_file_zip` | 上传前将单个文件压缩为 ZIP | `false` |
| `upload_drive.after_upload_file_delete` | 云盘上传成功后删除本地文件；可设置为 false 保留 | `true` |
| `upload_drive.upload_adapter` | 上传适配器；默认 `rclone`；旧 `aligo` 适配器需要额外安装与登录，主依赖未包含它 | `rclone` |
| `proxy.scheme` | Telegram 代理协议，例如 `http` 或 `socks5`；不是 Compose 的环境变量代理 | — |
| `proxy.hostname` | Telegram 代理主机，不含协议 | — |
| `proxy.port` | Telegram 代理端口，整数 | — |

媒体类型、扩展名和命名字段通过勾选配置；可添加自定义扩展名，用箭头调整命名顺序。使用 Bot 命令时，请在**允许的用户**中添加自己的 Telegram 数字用户 ID。已登录的用户下载账号自动获准；Bot 模式下的 `me` 不能授权真人。频道通过 Bot 命令管理。

#### 设置生效

- 管理密码、公开下载、语言、允许用户和下载设置保存后生效。新的命名、目录和分片设置用于后续下载。
- 修改 API 凭据、Bot Token、账号模式、代理或连接超时后，在 Configuration 验证登录或重连，无需重启服务。
- 监听地址/端口、MCP、日志、启动频道扫描和云上传设置需要重启，页面会标注。

#### 并行下载与断点

`max_download_task` 控制同时下载文件数（1–6），`file_download_concurrency` 控制每文件连接数（1–32），`download_part_size_mb` 控制分片大小（1–4096 MiB）。默认同时下载 5 个文件，每文件 4 个连接，分片 64 MiB。传输容量保持 `0` 即可自动计算。

每个下载预分配一个 `downloads/.temp/timestamp_guid.tmp.temp` 数据文件，各 worker 按分片 offset 写入，不再创建独立 `.part` 文件或最终合并副本。`.parts/manifest.json` 使用版本 3，保存文件身份、范围、worker 数和各片已同步落盘的字节数；预分配后的文件长度不代表完成进度。数据先同步，再原子更新断点。通常每新增 8 MiB 或每秒提交一次；分片完成和正常停止会强制提交。异常终止可能重新下载最后一次提交之后的数据。

暂停、网络失败和重启会保留新版断点；已有新版下载继续使用保存的分片计划，不受新设置影响。旧 `.part`、旧版 manifest 和没有新版 manifest 的顺序 `.temp` 不迁移、不续传；请在升级前完成旧下载，或明确删除对应任务的缓存后重新下载。应用遇到旧缓存会报错并保留它，不会把旧数据当成新版进度；旧版本也不能接管新版断点。

支持的系统优先预约磁盘空间；不支持时仅设置逻辑长度，后续仍可能磁盘满或超过配额。正常下载只需要一份文件数据及少量断点元数据，无需为合并准备第二份完整副本。Linux/macOS 使用 `pwrite`，缺少该接口的平台使用独立文件句柄定位写入。NAS 的同步和空间预约能力取决于文件系统。

全部分片完成、文件同步并关闭句柄后，先生成 staging 成品，再原子 rename 到最终路径，之后才记录完成。临时目录与最终目录必须位于同一文件系统；跨设备移动会报错并保留 staging，不自动复制。不要为下载目录的子目录设置跨设备挂载。

删除任务时，只有同时选择删除文件，才会移除本地文件和临时数据；删除前会等待连接和正在执行的磁盘写入停止。

Bot/日志语言在 Configuration 设置；页头语言选择器控制网页语言。支持 `en`、`zh-cn`、`zh-tw`、`ja`、`es`、`fr`、`ru`、`uk`，`zh` 对应简体中文。添加翻译见[语言包指南](./docs/languages.md)。

## WebUI 登录

1. 打开 `/configuration`，输入启动日志中的 **First-time setup code**，创建管理密码。新建或修改的密码至少 8 位，包含大小写字母和数字；已有密码仍可使用。初始化前重启会生成新码。
2. 在**基础配置**填写 API ID、API Hash 和 Bot Token，点击**验证并登录**。默认使用 Bot 下载。
3. 如需使用个人账号，勾选**使用用户账号下载**，填写带国家区号的手机号，再输入 Telegram 验证码和必要的账户密码。
4. 等待页面显示绿勾。仅收到 Telegram 登录通知不代表服务已启动完成。

已保存的 Telegram session 会在重启后复用。使用**测试连接**检查已保存的登录；断网恢复后可点**重新连接会话**。登录流程取消或验证码过期后可重新开始。**退出登录**用于退出 Telegram 账号，任务和断点会保留。

**网页访问**：默认下载页和 Configuration 都需要管理密码。勾选**公开下载页面**后，任何能访问服务的人都可以免登录查看、提交和管理下载；Configuration 仍需要管理密码。密码留空表示保留原值。旧无密码部署保留公开下载，可用初始化码创建 Configuration 密码。

**记住登录 7 天**为可选项：勾选后登录保留七天，浏览器或服务重启后仍有效。未勾选时使用浏览器会话 Cookie，服务重启不会使其失效，最长有效期同样为七天。签名密钥自动生成在 `data/web_session.key`，权限为 0600；升级或重建容器时需要保留该文件。修改密码会使其他浏览器登录失效；在配置页修改密码时，当前登录会保留。Web API 使用同一网页登录会话，修改操作需要 CSRF Token；MCP Token 仅用于 `/mcp`。

从仅在内存中保存签名密钥的旧版升级后，需要重新登录一次；之后服务重启会保留新的登录。

### 命令行登录

推荐网页登录。如需命令行登录，先在 Configuration 保存凭据和账号模式，或在首次启动前准备可选 YAML，再添加 `--login`。

手动安装：先停止原进程，在原工作目录、原虚拟环境中运行：

```bash
python media_downloader.py --login
```

按提示输入手机号、Telegram 验证码和必要的账户密码；Bot 模式使用已保存的 Token。验证码和密码不会保存。登录后服务继续运行，以后正常启动可省略 `--login`。

Docker Compose：先停止原服务，再用同样的持久化挂载启动交互实例；不要让两个实例同时使用同一份 session：

```bash
docker compose stop tgporter
docker compose run --rm -it --service-ports tgporter python media_downloader.py --login
# After successful login, press Ctrl+C to stop the foreground instance.
docker compose up -d tgporter
```

直接使用镜像时，把下面的路径占位符替换为原实例正在使用的同一组宿主机目录；如使用可选 YAML，给临时容器额外添加前述只读挂载：

```bash
docker stop tgporter
docker run --rm -it --name tgporter-login \
  -v "/path/to/tgporter/data:/app/data" \
  -v "/path/to/downloads:/app/downloads" \
  ghcr.io/smilekevin/tgporter:latest python media_downloader.py --login
# After successful login, press Ctrl+C to stop the foreground instance.
docker start tgporter
```

沿用原实例的镜像版本、数据/下载目录挂载和网络设置。用户账号首次命令行登录需要交互终端；无人值守部署请使用 Configuration。

## MCP Agent 接入

### 开启 MCP

MCP 使用现有 WebUI 端口的 `/mcp`，默认关闭，无需额外容器。

生成随机 Token，例如：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

1. 在 **Configuration → 网页与 MCP** 填入 **MCP Token**，开启 MCP，保存并重启。
2. 使用域名、反向代理或远程 IP 时，将客户端请求使用的主机名加入 **MCP服务主机名**，例如 `nas.example.com` 或 `192.168.1.20:5000`。默认已允许 localhost。填写主机名/IP 和可选端口（1–65535），不要包含协议、路径、凭据或通配符；格式错误会显示提示。此列表不会改变监听地址。

### Agent 客户端

```json
{
  "mcpServers": {
    "tgporter": {
      "url": "http://127.0.0.1:5000/mcp",
      "headers": {"Authorization": "Bearer YOUR_MCP_TOKEN"}
    }
  }
}
```

MCP 使用独立 Token，不使用网页管理密码。**只读模式**仅允许查询。工具说明见 [MCP 参考](./docs/mcp.md)。

### 安装可选 Skill

从 [GitHub Release 附件](https://github.com/smilekevin/TGPorter/releases) 下载 **`TGPorterSkills.zip`**，交给支持 Skill 的 Agent。若客户端要求目录，先解压，再导入 `tgporter/`；安装方式以客户端文档为准。

也可从源码导入完整的 [`skills/tgporter/` 目录](https://github.com/smilekevin/TGPorter/tree/main/skills/tgporter)，保留 `SKILL.md` 与 `references/`。

安装后可让 Agent 帮助部署 TG Porter、连接 MCP 或管理下载。安装 Skill 本身不会启动服务或配置 MCP，需要使用你自己的实例地址和 Token；客户端需支持 Streamable HTTP 和 Bearer Token。使用下载包部署时，以包内版本信息选择对应的镜像和源码。

CI 预览包位于对应 Actions 运行的 **Artifacts**，需登录 GitHub 下载，保留 14 天；它使用该次运行的 SHA 镜像。

在[手动安装](#手动安装)的 Python 环境中，可本地生成预览包：

```bash
python tools/build_skill.py --tag v1.2.3 --commit "$(git rev-parse HEAD)" --output-dir dist
```

`v1.2.3` 仅为本地预览标签示例，该命令不会发布 Release。

### 提交下载

通过 MCP `submit_download` 或下载页提交单条 HTTPS `t.me` 消息链接，例如 `https://t.me/channel/123`、`https://t.me/c/123456/789`，也支持主题链接。下载账号必须有权访问该消息。提交成功表示任务已入队，不代表下载完成；重复提交未完成的消息会返回原任务。

已有无 Bot 部署仍受支持。网页和 MCP 提交不会自动发送 Bot 通知。

## Docker 管理

```bash
docker compose logs -f tgporter
docker compose stop tgporter
docker compose up -d
docker compose restart tgporter
docker compose pull
docker compose up -d
docker compose ps
```

只有 GitHub Release 更新 `latest`，普通 CI 发布 `sha-完整提交号` 镜像。需要固定版本时，在 Compose 中使用版本或 SHA 标签；镜像地址和 digest 见 Release notes。

镜像已包含应用版本，请勿在 Compose 中覆盖 `APP_VERSION`。

## 旧镜像迁移与本地构建

旧部署首次在没有 `data/settings.json` 的情况下启动时，会导入可选 YAML 以及原有语言、并发和广告规则，之后通过 Configuration 修改。详见[可选 YAML 导入](#configyaml-可选)。

升级时停服，备份并复用原有 `data/` 和下载目录。导入成功后后续升级只需这两个目录；不要删除任务或 session 来“重置配置”。

本地构建使用当前源码版本：

```bash
docker build --build-arg APP_VERSION="$(git describe --tags --always --dirty)" -t tgporter:local .
```

将 Compose 的 image 改为 `tgporter:local` 后执行 `docker compose up -d`。本地版本不要用远端 `latest` 标签冒充正式 Release。

## Docker 数据持久化

| 路径 | 用途 |
| --- | --- |
| `data/settings.json` | 配置、凭据、Bot/日志语言、并发与广告规则；自动创建，权限为 0600 |
| `data/web_session.key` | 浏览器会话签名私钥；保留后服务重启不影响登录 |
| `data/data.json` | 频道扫描位置、重试消息 ID；保留未启用频道与无关字段 |
| `data/sessions/` | Telegram 授权 session |
| `data/bot.yaml` | 既有 Bot 任务/监听元数据 |
| `data/log/tdl.log` | 应用日志，10 MB 轮转，保留 30 天，使用配置日志级别 |
| `data/bot_tasks.json` | 下载和转发任务 |
| `data/task_counter.json` | 每日任务序号 |
| `data/download_history.json` | 完成和失败的下载记录 |
| `downloads/` | 已完成文件 |
| `downloads/.temp/` | 临时文件与 `.parts/manifest.json` 断点 |

挂载可写的 `data/` 和下载目录，运行文件会自动创建，断点保存在下载目录的 `.temp/` 中，无需额外临时目录挂载。重建容器会保留宿主机挂载数据。备份包含凭据和 Telegram 授权，请妥善保管。JSON 数据文件直接保存在 `data/`，`data/log/` 仅保存日志。旧部署请在停服后将 `data/log/` 中的 JSON 文件移到 `data/` 再启动，程序不再读取旧位置。

## 代理配置

在 **Configuration → 高级 → Telegram 代理** 打开开关，选择 `socks5`、`socks4` 或 `http`，填写主机和端口。Docker Desktop 可使用实际可达的 `host.docker.internal:1080` SOCKS5 代理。用户名和密码可选，留空保留原值；关闭开关即可移除代理。保存后验证登录或重连以应用修改。

容器中的 `127.0.0.1` 指向容器本身。Docker Desktop 可按环境使用 `host.docker.internal`，Linux 需选择容器实际可达的宿主机地址；不要直接照抄其他机器的网关。Docker 拉镜像代理与 Telegram SDK 代理是不同配置。

## 故障排查

- **网页打不开**：检查端口占用、映射、防火墙和监听地址；Telegram 不通不会阻止首次配置页启动。
- **找不到初始化码**：查看当前服务启动日志；仅未完成初始化时输出，重启会产生新码。旧无密码部署需打开 `/configuration`。
- **登录失败**：检查 API 凭据、代理和账号访问权限；验证码过期后重新获取，限流按页面倒计时等待。无需在终端交互。
- **session 失效**：在 Configuration 重新连接；仍失败时退出 Telegram 后重新登录。任务和断点保留。
- **修改未生效**：查看 Configuration 的重启提示；端口变化还需同步 Docker 映射。
- **MCP 404 / 401 / Host 拒绝**：分别检查启用开关与重启、独立 Token、实际 Host 与允许列表。
- **配置保存失败**：检查 data 挂载权限和磁盘空间；失败不会替换原文件。

### Bot 心跳与消息诊断

网页显示 Bot 连接状态、延迟和最近检查时间；没有 Bot Token 时显示未配置。此状态仅说明 Bot 连接，不代表下载账号有访问权限或 Bot 命令已获授权。服务会定期检查并在连续失败后尝试重连，遇到 Telegram 限流时等待。

查看近期日志：

```bash
docker logs --since 30m --timestamps tgporter
docker logs -f --since 5m --timestamps tgporter
```

日志也写入 `data/log/tdl.log`，10 MB 轮转，保留 30 天。正常使用选择 `INFO`，排查连接问题时可改为 `DEBUG`。

- **Bot 断开连接**：检查 Telegram 网络、代理和近期的 `Bot heartbeat failed` 错误。
- **Bot 已连接但命令无响应**：检查 Configuration 的**允许的用户**。日志出现 `authorized=False` 表示发送者没有权限。
- **收到消息但回复卡住**：查看 `Bot handler started` / `Bot handler finished` 及重连错误。
- **状态未知**：检查最近检查时间、服务是否运行，以及 Telegram 是否可达。

MCP 返回同样的 Bot 状态，见[工具参考](https://github.com/smilekevin/TGPorter/blob/main/docs/mcp.md#结果与错误)。需要验证实际下载完整性时，参照[并行下载测试指南](./docs/parallel-download-testing.md)。
