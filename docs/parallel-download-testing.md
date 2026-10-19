# 分片下载测试

离线测试不需要 Telegram 账号、API 凭据或测试 Bot。真实下载验收可以使用现有登录账号，
只有需要验证 Bot 收到文件后的下载路径时才需要 Bot，现有 Bot 即可。

## 离线回归

在已安装项目依赖的环境中，从仓库根目录运行：

```sh
python -B -m unittest discover -s tests -p 'test_partial_file.py' -v
python -B -m unittest discover -s tests -p 'test_parallel*.py' -v
python -B -m unittest discover -s tests -p 'test_download_connections.py' -v
python -B -m unittest discover -s tests -v
```

测试通过模拟 `get_file` 返回不同内容的 1 MiB 数据块，验证：

- 乱序 offset 写入后单文件的 SHA-256 与原始数据一致，尾部不足 1 MiB 也正确。
- 多个文件独立启动自己的固定大小切片，worker 完成切片后继续领取下一片，不再预留全局下载名额。
- 使用真实 Pyrogram 下载代码、模拟网络 Session，验证同一数据中心的两个文件、每文件四个 worker 创建八个独立连接，同一 worker 的后续切片沿用该连接；单分片文件之间也不复用连接。
- 暂停保留原连接，取消、启动中断和客户端重启会清理连接；跨数据中心授权和 CDN 文件哈希校验继续使用 Pyrogram 的处理逻辑。
- 暂停恢复不改变切片计划和 worker 数；取消可打断挂起的请求或等待并发名额的任务。
- 断流、短块、未提交的磁盘写入、程序重启和发布中断均能保留新版断点并正确续传。旧串行断点和旧 manifest 明确拒绝，保留原文件，不迁移。
- 现有下载入口能刷新过期文件引用，并使用消息所属账号刷新引用。
- UI 数据只有一个文件进度记录，已下载字节汇总全部分片。
- 网页删除使用的真实 Flask 接口、Web API 单删/批删及 Bot 停止回调均会清理对应临时数据；下载中、暂停中、预分配期间和最终发布前取消均有覆盖。
- 用户取消先关闭连接，再删除该下载的临时文件和 `.parts/`；程序关闭和异常中断保留断点。取消单个文件不影响同任务其他文件，取消后同消息可创建新任务重新下载。
- 固定大小切片包含不足一片的尾部，小文件只创建必要的 worker，配置保存和非法配置校验正确。
- 覆盖 `pwrite` 短写、零写、独立句柄 fallback、超过 4 GiB 的 offset、预分配失败和双重取消后等待磁盘操作完成。
- 预分配文件从开始就具有完整逻辑大小，但 manifest 的 `committed_bytes` 才是续传依据；缺失、截短或非法断点不得误报完成。
- 数据先同步，再提交 manifest；manifest 磁盘满保留旧提交，恢复时覆盖未提交的字节。
- staging rename 后或最终完成回调中断可只重试发布；`EXDEV` 保留 staging，目标冲突不覆盖其他文件。

## 真实下载验收

可以使用独立的本地测速入口，直接运行应用使用的分片下载核心。它需要项目 Python 依赖，
通过登录账号读取 Telegram 文件，不需要启动 Bot 或 WebUI；耗时不包含 WebUI/Bot 回调开销。

先在应用 Configuration 页面保存 API 凭据和代理，测速工具默认读取 `data/settings.json`。
并发设置与应用一致；测速使用独立目录与 session。首次登录在本地终端运行，可输入 Bot Token，或输入手机号、验证码及必要的两步验证密码：

```sh
python -B tools/benchmark_download.py --login
```

已有用户 session 时，可以直接指定配置和 session 路径。程序通过 SQLite 备份复制 session，
将测速登录数据单独保存在 `local_test/telegram-benchmark/sessions/`，之后运行时省略 `--session`：

```sh
python -B tools/benchmark_download.py --config /path/to/settings.json \
  --session /path/to/media_downloader.session \
  --message https://t.me/channel/123 --lanes 1 4
```

每次测速使用新目录，自动校验单路和多路结果的 SHA-256，保存各轮传输时间、含同步和原子发布的总耗时及平均速度：

```sh
python -B tools/benchmark_download.py --message https://t.me/channel/123 --lanes 1 4
# 若需要多轮对比：
python -B tools/benchmark_download.py --message https://t.me/channel/123 --lanes 1 4 4 1
# 收藏夹没有可用消息链接时，可以直接指定消息编号：
python -B tools/benchmark_download.py --chat me --message-id 123 --lanes 1 4
```

结果位于 `local_test/telegram-benchmark/runs/本次目录/results.json`。
有原始文件时增加 `--expected-file /path/to/original.bin`，同时检查与原始文件一致。
两轮成功且哈希一致时 `completed` 和 `hashes_match` 均为 `true`；中断或失败的结果不能作为成功测速。

如需通过本地完整应用进行下载和暂停/重启验收，可使用以下步骤：

1. 选一个至少 256 MiB 的已知文件，保存原始 SHA-256，将它上传到自己的“收藏夹”或私有频道。
   使用现有下载入口提交该消息；如果入口通过 Bot 接收消息链接，继续使用现有 Bot。
2. 第一轮使用 `file_download_concurrency: 1`；第二轮使用下面的配置。
   每轮使用新的保存目录和已完成前一轮的测试任务，避免已存在文件被跳过，或旧断点沿用原分片数。
   保持账号、文件、代理和其他下载任务一致，记录开始至完成的实际耗时。

   ```yaml
   max_download_task: 5
   file_download_concurrency: 4
   download_part_size_mb: 64
   ```

3. 对原文件和每轮下载结果运行 `shasum -a 256 文件路径`（Linux 可用 `sha256sum`）。
   三者哈希必须一致，不能只比较文件长度。日志中的 `in N part(s) with M worker(s)` 表示切片数和计划使用的 worker 数上限；实际连接数取决于确认启动和剩余分片。
   `Opened independent download connection #…` 表示已通过独立连接收到文件数据，包含文件名和 worker 编号；
   `Confirmed independent download startup` 表示该 worker 的首块有效数据已写入，在该确认前不得为同一文件启动下一个新连接；启动间隔仍至少一秒。
   若首个连接一直停在密钥创建或等待首块数据，其余 worker 不应发起下载；若启动失败，确认关闭连接后才尝试下一个。
   `Closed independent download connection #…` 表示该连接已清理。应用运行时，这些 INFO 日志写入统一的 `data/log/tdl.log`。
   切片数本身不能证明 TCP 连接数。实际连接验收还应通过系统 socket 信息或代理连接页面，
   确认同一个下载进程存在不同源端口的下载 TCP；同时下载两个至少四片的文件、每文件四个 worker 时，逐个确认并扩展后最多可看到八条媒体下载连接。
   统计同时活跃的连接数，不要将网络重连产生的累计连接次数当作并发数。
   同一 worker 连续下载后续切片时会沿用自己的连接；控制连接和 CDN 连接可能额外出现。
4. 新建一次多分片测试任务，在有进度后暂停，确认进度停止；恢复后确认分片数不变且哈希一致。
5. 再新建一次任务，下载一部分后停止程序，保留整个 `downloads/.temp` 目录，再启动程序。
   任务恢复后应沿用 `.parts/manifest.json` 中的范围，不从零开始，最终哈希仍一致。
   可以在重启前修改单文件 worker 数或切片大小，验证原任务没有重新分片。
6. 用约 40 MiB、100 MiB 和 500 MiB 的文件验证切片大小：上述配置应分别使用 1、2 和 8 片，worker 数最多为 4。
7. 如需验证 Bot 自有消息，再直接给现有 Bot 发送文件，验证进度、引用刷新和最终哈希。
8. 下载产生数据后，分别通过网页删除、`POST /delete_task?task_id=…`、`POST /batch_delete`（JSON `task_ids`）以及 Bot 的 `/stop` 菜单取消任务；网页/API/MCP 在本步骤显式选择 `delete_file=true`。
   等下载协程结束后，确认对应 `downloads/.temp/` 中的预分配数据文件、staging 成品和 `.parts/` 已删除，连接已关闭；其他下载文件及已完成文件保持原状。
   `Removed cancelled download data: …` 日志表示具体临时路径已清理。暂停和程序退出后仍应保留断点，以便恢复。

完成时间改善与否取决于账号限速、连接延迟、代理带宽和磁盘。离线测试确认文件正确性和调度规则，
实际加速比例需要通过上述真实下载对比测量。正常下载只有一份预分配数据文件，没有合并阶段或第二份完整副本。空间预约不可用时，逻辑大小不代表物理空间已经分配；NAS 上仍需验收磁盘满与配额错误。

## 真实进程中断与续传测试

可以让测试进程在下载达到指定大小后立即退出，跳过 Python 清理，模拟程序异常中断。
应使用单独的测试工作目录；首次运行时复制已有 session，后续运行省略 `--session`。

```sh
python -B tools/benchmark_download.py --work-dir local_test/resume-test \
  --session local_test/telegram-benchmark/sessions/telegram-benchmark.session \
  --message https://t.me/channel/123 --lanes 4 \
  --resume-file local_test/resume-test/download.bin --interrupt-after-mib 64
```

进程返回码 `86` 表示测试工具按计划直接退出。先保留或复制此时的 `.resume.json` 报告，
再使用相同输出路径续传（不传 `--interrupt-after-mib`）：

```sh
python -B tools/benchmark_download.py --work-dir local_test/resume-test \
  --message https://t.me/channel/123 --lanes 2 \
  --resume-file local_test/resume-test/download.bin \
  --expected-file /path/to/previous-verified-download.bin
```

启动参数可改为 2 个 worker，用来验证保存的切片计划和 worker 数不会变化。
`.resume.json` 会记录恢复的字节基数、保存的分片范围、预期和实际请求偏移以及最终 SHA-256。
验收时检查 `resumed_from_bytes` 大于 0、请求从各分片断点开始、`completed` 和 `hash_matches` 均为 `true`。
页面接口的离线集成测试还会验证：恢复后保留一个汇总进度条，已有断点字节不计入初始下载速度。


## 平台与存储验收

在 Linux Alpine amd64/arm64、macOS、Windows 和实际 NAS 文件系统分别验证新建下载、暂停/重启续传与 SHA-256。离线 fallback 测试不代替 Windows 原生验收。
记录原实现与新实现相同输入的总耗时、磁盘读写量、峰值空间和同步耗时。新增下载不应出现 `N.part` 或完整 merge 副本。
在独立测试卷模拟预分配、写入、同步、manifest 更新和最终 rename 失败，确认不会触发 Telegram 重连，清理前没有残留写入。
最终目录必须与 staging 同文件系统；跨设备目标应明确失败并保留可再次发布的数据，不能出现部分最终文件。
强制退出测试只能验证进程中断；断电持久性还取决于文件系统、挂载方式与存储设备。
