# 分片下载测试

离线测试不需要 Telegram 账号、API 凭据或测试 Bot。真实下载验收可以使用现有登录账号，
只有需要验证 Bot 收到文件后的下载路径时才需要 Bot，现有 Bot 即可。

## 离线回归

在已安装项目依赖的环境中，从仓库根目录运行：

```sh
python -B -m unittest discover -s tests -p 'test_parallel*.py' -v
python -B -m unittest discover -s tests -v
```

测试通过模拟 `get_file` 返回不同内容的 1 MiB 数据块，验证：

- 乱序返回后合并文件的 SHA-256 与原始数据一致，尾部不足 1 MiB 也正确。
- 多个文件独立启动自己的固定分片，分片结束不重新分配工作，不再预留全局下载名额。
- 暂停恢复不创建新分片；取消可打断挂起的请求或等待并发名额的任务。
- 断流、短块、旧串行断点、非完整块、程序重启和合并中断均能保留断点并正确续传。
- 现有下载入口能刷新过期文件引用，并使用消息所属账号刷新引用。
- UI 数据只有一个文件进度记录，已下载字节汇总全部分片。
- 小文件按整除规则减少分片，配置保存和非法配置校验正确。

## 真实下载验收

可以使用独立的本地测速入口，直接运行应用使用的分片下载核心。它需要项目 Python 依赖，
通过登录账号读取 Telegram 文件，不需要启动 Bot 或 WebUI；耗时不包含 WebUI/Bot 回调开销。

先在 Git 忽略的 `local_test/config.yaml` 中填写 `api_id`、`api_hash` 和代理。
并发设置与应用配置相同。首次登录在本地终端运行，按提示输入手机号、验证码及必要的两步验证密码：

```sh
python -B tools/benchmark_download.py --login
```

已有用户 session 时，可以直接指定配置和 session 路径。程序通过 SQLite 备份复制 session，
将测速登录数据单独保存在 `local_test/telegram-benchmark/sessions/`，之后运行时省略 `--session`：

```sh
python -B tools/benchmark_download.py --config /path/to/config.yaml \
  --session /path/to/media_downloader.session \
  --message https://t.me/channel/123 --lanes 1 4
```

每次测速使用新目录，自动校验单路和多路结果的 SHA-256，保存各轮传输时间、含合并的总耗时及平均速度：

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
   download_min_part_size_mb: 16
   ```

3. 对原文件和每轮下载结果运行 `shasum -a 256 文件路径`（Linux 可用 `sha256sum`）。
   三者哈希必须一致，不能只比较文件长度。日志中的 `fixed range(s)` 表示本次使用的固定分片数。
4. 新建一次多分片测试任务，在有进度后暂停，确认进度停止；恢复后确认分片数不变且哈希一致。
5. 再新建一次任务，下载一部分后停止程序，保留整个 `temp` 目录，再启动程序。
   任务恢复后应沿用 `.parts/manifest.json` 中的范围，不从零开始，最终哈希仍一致。
   可以在重启前修改单文件并发数或最小分片大小，验证原任务没有重新分片。
6. 用约 10 MiB 和 40 MiB 的文件验证最小分片规则：上述配置应分别使用 1 片和 2 片。
7. 如需验证 Bot 自有消息，再直接给现有 Bot 发送文件，验证进度、引用刷新和最终哈希。

完成时间改善与否取决于账号限速、连接延迟、代理带宽和磁盘。离线测试确认文件正确性和调度规则，
实际加速比例需要通过上述真实下载对比测量。多分片合并会短暂占用最多约两倍文件大小的临时空间。

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

启动参数可改为 2 片，用来验证原有 4 片计划不会被重新拆分。
`.resume.json` 会记录恢复的字节基数、保存的分片范围、预期和实际请求偏移以及最终 SHA-256。
验收时检查 `resumed_from_bytes` 大于 0、请求从各分片断点开始、`completed` 和 `hash_matches` 均为 `true`。
页面接口的离线集成测试还会验证：恢复后保留一个汇总进度条，已有断点字节不计入初始下载速度。
