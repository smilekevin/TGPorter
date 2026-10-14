#!/usr/bin/env python3
"""Compare real Telegram downloads locally without starting a bot or web server."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyrogram
from pyrogram.file_id import FileId
from ruamel.yaml import YAML

from module.parallel_download import CHUNK_SIZE, download_file, parts_path, part_count


def message_target(link):
    parsed = urlparse(link)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("t.me", "www.t.me", "telegram.me"):
        raise ValueError("请使用 Telegram 文件消息链接，例如 https://t.me/channel/123")
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts or not parts[-1].isdigit():
        raise ValueError("链接必须指向一条消息")
    if parts[0] == "c" and len(parts) in (3, 4) and parts[1].isdigit():
        chat = int("-100" + parts[1])
    elif len(parts) in (2, 3) and not parts[0].startswith(("+", "joinchat")):
        chat = parts[0]
    else:
        raise ValueError("无法识别消息链接；收藏夹可使用 --chat me --message-id 消息编号")
    return chat, int(parts[-1])


def load_config(path):
    with path.open(encoding="utf-8") as file:
        config = YAML(typ="safe").load(file)
    if not isinstance(config, dict):
        raise ValueError("配置文件必须为 YAML 对象")
    try:
        api_id = int(config.get("api_id", ""))
    except (TypeError, ValueError):
        raise ValueError("请在本地配置文件填写有效的 api_id") from None
    api_hash = config.get("api_hash")
    if api_id < 1 or not isinstance(api_hash, str) or len(api_hash) != 32:
        raise ValueError("请在本地配置文件填写有效的 api_id 和 api_hash")
    settings = {"file_download_concurrency": config.get("file_download_concurrency", 4),
                "download_part_size_mb": config.get("download_part_size_mb", config.get("download_min_part_size_mb", 64))}
    for key, value in settings.items():
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} 必须为正整数")
    config.pop("download_concurrency", None)
    config.update(settings)
    config["api_id"] = api_id
    return config


def prepare_session(work_dir, source=None):
    directory = work_dir / "sessions"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / "telegram-benchmark.session"
    if source is not None:
        if not source.is_file():
            raise ValueError("指定的 session 文件不存在")
        if destination.exists():
            raise ValueError("测速 session 已存在；复用时省略 --session，或指定新的 --work-dir")
        # SQLite backup also copies committed WAL data without touching the source.
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
        os.chmod(destination, 0o600)
    return destination


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


async def benchmark(client, chat, message_id, counts, config, run_dir, expected=None):
    run_dir.mkdir(parents=True, exist_ok=False)
    part_size = config.get("download_part_size_mb", config.get("download_min_part_size_mb", 64)) * CHUNK_SIZE
    rows = []
    original_identity = None
    original_size = None
    expected_hash = sha256_file(expected) if expected else None
    report = {"completed": False, "timezone": "Asia/Shanghai", "pyrogram_version": pyrogram.__version__,
              "proxy_used": bool(config.get("proxy")),
              "part_size_mib": part_size // CHUNK_SIZE,
              "expected_sha256": expected_hash, "runs": rows}

    for index, count in enumerate(counts, 1):
        # Refresh references before every round, keeping the same media identity.
        message = await client.get_messages(chat, message_id)
        media = next((getattr(message, kind, None) for kind in (
            "video", "document", "audio", "photo", "voice", "video_note", "animation"
        ) if getattr(message, kind, None) is not None), None)
        if media is None or not getattr(media, "file_size", 0):
            raise ValueError("消息不可访问，或不含已知大小的可下载文件")
        size = media.file_size
        file_id = FileId.decode(media.file_id)
        identity = getattr(media, "file_unique_id", None) or f"{file_id.dc_id}:{file_id.media_id}"
        if original_identity is None:
            original_identity, original_size = identity, size
        if identity != original_identity or size != original_size:
            raise ValueError("两轮之间文件发生变化，测速结果不可比较")
        pieces = part_count(size, part_size)
        effective = min(count, pieces)
        if count > 1 and effective < 2:
            raise ValueError("文件太小，无法测试多分片；请选择更大的文件或调整切片大小配置")
        output = run_dir / f"{index:02d}-{count}-lanes.bin"
        started = time.perf_counter()
        received_at = None
        last_print = 0.0

        async def progress(current, total):
            nonlocal received_at, last_print
            now = time.perf_counter()
            if current > 0 and current >= total - 1 and received_at is None:
                received_at = now
            if now - last_print >= 1 or current == total:
                elapsed = max(now - started, 0.001)
                print(f"\r第 {index} 轮：{effective} worker，{pieces} 片，{current / total:.1%}，"
                      f"平均 {current / CHUNK_SIZE / elapsed:.2f} MiB/s", end="", flush=True)
                last_print = now

        async def ready():
            return

        try:
            await download_file(client, file_id, size, str(output), identity,
                                count, progress, ready, part_size)
        except BaseException:
            report["incomplete_round"] = index
            (run_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            raise
        finished = time.perf_counter()
        transfer_seconds = (received_at or finished) - started
        elapsed = finished - started
        digest = sha256_file(output)
        rows.append({"requested_workers": count, "actual_workers": effective, "actual_parts": pieces, "bytes": size,
                     "transfer_seconds": round(transfer_seconds, 3),
                     "total_seconds": round(elapsed, 3),
                     "transfer_mib_per_second": round(size / CHUNK_SIZE / transfer_seconds, 3),
                     "total_mib_per_second": round(size / CHUNK_SIZE / elapsed, 3),
                     "sha256": digest, "file": str(output)})
        hashes = {row["sha256"] for row in rows}
        report["hashes_match"] = len(hashes) == 1 and (expected_hash is None or digest == expected_hash)
        (run_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n耗时 {elapsed:.2f}s（含合并），SHA-256：{digest}")
        if not report["hashes_match"]:
            raise ValueError("文件哈希不一致，保留结果供排查，不能据此判定测速成功")
    report["completed"] = True
    (run_dir / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"结果：{run_dir / 'results.json'}")
    if len(rows) == 2:
        print(f"端到端速度比：{rows[0]['total_seconds'] / rows[1]['total_seconds']:.2f} 倍")
    return report


async def resume_download(client, chat, message_id, config, output, count,
                          expected=None, interrupt_after_mib=None):
    """Reuse the same checkpoint across separate processes and record offsets."""
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    message = await client.get_messages(chat, message_id)
    media = next((getattr(message, kind, None) for kind in (
        "video", "document", "audio", "photo", "voice", "video_note", "animation"
    ) if getattr(message, kind, None) is not None), None)
    if media is None or not getattr(media, "file_size", 0):
        raise ValueError("消息不可访问，或不含已知大小的可下载文件")
    size = media.file_size
    if interrupt_after_mib is not None and interrupt_after_mib * CHUNK_SIZE >= size:
        raise ValueError("中断点必须小于文件大小")
    file_id = FileId.decode(media.file_id)
    identity = getattr(media, "file_unique_id", None) or f"{file_id.dc_id}:{file_id.media_id}"
    manifest = parts_path(str(output)) / "manifest.json"
    plan = json.loads(manifest.read_text()) if manifest.exists() else None
    expected_offsets = []
    if plan:
        for index, (start, end) in enumerate(plan["ranges"]):
            path = manifest.parent / f"{index}.part"
            length = path.stat().st_size if path.exists() else 0
            if length < end - start:
                expected_offsets.append(start // CHUNK_SIZE + length // CHUNK_SIZE)
    report = {"completed": False, "file": str(output), "size": size,
              "requested_workers": count, "saved_ranges": plan["ranges"] if plan else None,
              "expected_resume_offsets_mib": expected_offsets,
              "request_offsets_mib": [], "resumed_from_bytes": None,
              "expected_sha256": sha256_file(expected) if expected else None}
    report_path = Path(str(output) + ".resume.json")
    started = time.perf_counter()
    last_print = 0.0

    def record_request(file_id, file_size, limit, offset):
        report["request_offsets_mib"].append(offset)

    async def progress(current, total):
        nonlocal last_print
        now = time.perf_counter()
        report["downloaded_bytes"] = current
        if report["resumed_from_bytes"] is None:
            report["resumed_from_bytes"] = current
            print(f"从 {current / CHUNK_SIZE:.1f} MiB 继续，当前配置 {count} worker", flush=True)
        if now - last_print >= 1 or current == total:
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\r文件进度：{current / total:.1%}，已下载 {current / CHUNK_SIZE:.1f} MiB",
                  end="", flush=True)
            last_print = now
        if interrupt_after_mib is not None and current >= interrupt_after_mib * CHUNK_SIZE:
            report["deliberately_interrupted"] = True
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print("\n测试进程立即退出，跳过 Python 清理，保留分片断点。", flush=True)
            os._exit(86)

    async def ready():
        return

    try:
        await download_file(client, file_id, size, str(output), identity, count,
                            progress, ready, config.get("download_part_size_mb", config.get("download_min_part_size_mb", 64)) * CHUNK_SIZE,
                            request_started=record_request)
    except BaseException:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        raise
    report["total_seconds"] = round(time.perf_counter() - started, 3)
    report["sha256"] = sha256_file(output)
    report["hash_matches"] = report["expected_sha256"] is None or report["sha256"] == report["expected_sha256"]
    report["completed"] = report["hash_matches"] and output.stat().st_size == size
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["completed"]:
        raise ValueError("续传结果的文件大小或 SHA-256 不正确")
    print(f"\n续传完成，SHA-256：{report['sha256']}\n报告：{report_path}")
    return report


async def run(args, config):
    work_dir = args.work_dir.resolve()
    session = prepare_session(work_dir, args.session)
    if not args.login and not session.exists():
        raise ValueError("尚未登录。先运行 --login，或用 --session 指定已有用户 session 文件")
    transport_limit = max(args.lanes or [config["file_download_concurrency"]])
    if args.resume_file:
        manifest = parts_path(str(args.resume_file.resolve())) / "manifest.json"
        if manifest.exists():
            plan = json.loads(manifest.read_text())
            transport_limit = max(transport_limit, plan.get("workers", len(plan["ranges"])))
    client = pyrogram.Client("telegram-benchmark", api_id=config["api_id"],
                             api_hash=config["api_hash"], workdir=str(session.parent),
                             proxy=config.get("proxy") or None, no_updates=True,
                             max_concurrent_transmissions=transport_limit)
    # Match the application's transport timeout, including slow/throttled links.
    from pyrogram.connection.transport.tcp import TCP
    TCP.TIMEOUT = 900
    try:
        async with client:
            os.chmod(session, 0o600)
            if args.login:
                print(f"本地登录完成：{session}")
                return
            chat, message_id = message_target(args.message) if args.message else (args.chat, args.message_id)
            if isinstance(chat, str) and chat.lstrip("-").isdigit():
                chat = int(chat)
            counts = args.lanes or [1, config["file_download_concurrency"]]
            if any(count < 1 for count in counts):
                raise ValueError("--lanes 必须为正整数")
            if args.resume_file:
                await resume_download(client, chat, message_id, config, args.resume_file,
                                      counts[-1], args.expected_file, args.interrupt_after_mib)
                return
            stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
            run_dir = work_dir / "runs" / f"{stamp}-{uuid4().hex[:6]}"
            await benchmark(client, chat, message_id, counts, config, run_dir, args.expected_file)
    finally:
        client.executor.shutdown(wait=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "local_test" / "config.yaml")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "local_test" / "telegram-benchmark")
    parser.add_argument("--session", type=Path, help="首次使用时复制已有用户 session，不修改原部署")
    parser.add_argument("--login", action="store_true", help="在终端交互登录并保存本地 session")
    parser.add_argument("--message", help="Telegram 文件消息链接")
    parser.add_argument("--chat", help="或使用聊天 ID/用户名；收藏夹填 me")
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--lanes", nargs="+", type=int, help="每轮 worker 数，默认 1 和配置中的单文件并发数")
    parser.add_argument("--expected-file", type=Path, help="可选原始文件，用于额外校验 SHA-256")
    parser.add_argument("--resume-file", type=Path, help="使用固定输出路径测试中断和重新启动续传")
    parser.add_argument("--interrupt-after-mib", type=int, help="仅测试用：下载到指定 MiB 后立即退出进程")
    args = parser.parse_args()
    if not args.login and not (args.message or (args.chat and args.message_id)):
        parser.error("测速需要 --message，或 --chat 与 --message-id")
    if args.interrupt_after_mib is not None and (not args.resume_file or args.interrupt_after_mib < 1):
        parser.error("--interrupt-after-mib 需要 --resume-file，且必须为正整数")
    try:
        asyncio.run(run(args, load_config(args.config)))
    except KeyboardInterrupt:
        print("\n已停止测速，保留本地下载数据。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"\n测速未完成：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
