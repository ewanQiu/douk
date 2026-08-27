"""下载器：驱动 yt-dlp 子进程，带 cookie/代理/指纹伪装，失败入库不中断。"""
from __future__ import annotations

import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import transcode
from .config import Config
from .store import Store

_print_lock = threading.Lock()


def ytdlp_bin() -> str:
    exe = Path(sys.executable).parent / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
    return str(exe) if exe.exists() else "yt-dlp"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


_IMPERSONATE: bool | None = None


def has_impersonate() -> bool:
    """curl_cffi 装了不代表能用 —— 版本超出 yt-dlp 的兼容区间时全部目标是
    unavailable，此时传 --impersonate 会直接让下载失败。所以实际探一次。"""
    global _IMPERSONATE
    if _IMPERSONATE is None:
        try:
            p = subprocess.run([ytdlp_bin(), "--list-impersonate-targets"],
                               capture_output=True, text=True, timeout=60)
            _IMPERSONATE = any(
                line.strip() and "unavailable" not in line
                for line in (p.stdout or "").splitlines()[3:]
            )
        except Exception:
            _IMPERSONATE = False
    return _IMPERSONATE


def _safe(name: str, limit: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" .")
    return (name[:limit] or "untitled").strip()


VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}


def _out_base(cfg: Config, target_name: str) -> Path:
    base = cfg.out_dir / _safe(target_name) if cfg.group_by_series else cfg.out_dir
    base.mkdir(parents=True, exist_ok=True)
    return base


def _stem(cfg: Config, row, target_name: str = "") -> str:
    """按 filename_template 渲染文件名主干。

    模板占位符: {seq} {seq2} {seq3} {id} {title} {drama}
    注意 {title} 不可靠 —— 有的作者把标题写成 'BabyTails EP01.mp4'，
    直接用会得到 xxx.mp4.mp4 这种双后缀。
    """
    seq = row["seq"] or 0
    values = {
        "seq": seq,
        "seq2": f"{seq:02d}",
        "seq3": f"{seq:03d}",
        "id": row["aweme_id"],
        "title": _safe(_strip_ext(row["title"]), 50),
        "drama": _safe(target_name, 50),
    }
    try:
        name = cfg.filename_template.format(**values)
    except (KeyError, IndexError, ValueError):
        name = f"第{seq}集"      # 模板写错了也别让下载崩掉
    return _safe(name, 80)


def _strip_ext(title: str) -> str:
    """去掉标题里作者手抖带上的文件扩展名。"""
    return re.sub(r"\.(mp4|mov|mkv|avi|m4v|webm)\s*$", "", title or "",
                  flags=re.IGNORECASE).strip()


def _outtmpl(cfg: Config, row, target_name: str) -> Path:
    return _out_base(cfg, target_name) / f"{_stem(cfg, row, target_name)}.%(ext)s"


def _find_existing(cfg: Config, row, target_name: str) -> Path | None:
    """--no-overwrites 命中已有文件时 yt-dlp 不打印 after_move，这里兜底找一下。"""
    base = _out_base(cfg, target_name)
    stem = _stem(cfg, row, target_name)
    for p in base.iterdir():
        if (p.is_file() and p.stem == stem
                and p.suffix.lower() in VIDEO_EXT and p.stat().st_size > 0):
            return p
    return None


def build_cmd(cfg: Config, row, target_name: str) -> list[str]:
    cmd = [
        ytdlp_bin(), row["web_url"],
        "-o", str(_outtmpl(cfg, row, target_name)),
        "--no-warnings", "--no-progress", "--no-playlist",
        # 别下调这两个：yt-dlp 自己的默认就是 10。之前写死 3 反而比默认更脆，
        # 而且没有退避 —— 三次重试挤在几秒内，一次网络抖动就够全撞上。
        # 典型症状是 curl (35) OpenSSL SSL_connect: Connection closed。
        "--retries", "10", "--fragment-retries", "10",
        # 指数退避：1s、2s、4s… 封顶 30s，给瞬时故障留出恢复窗口
        "--retry-sleep", "exp=1:30",
        "--retry-sleep", "fragment:exp=1:20",
        "--socket-timeout", "30",
        "--no-overwrites", "--continue",
        "--print", "after_move:%(filepath)s",
    ]
    if cfg.video_codec == "h264":
        # TikTok 的 H.264 最高只到 576x1024，但不用转码就能到处播
        cmd += ["-S", "vcodec:h264"]
    else:
        # h265 / transcode 都要先拿到最高画质的源（1080p 只有 H.265）
        cmd += ["-S", "res,vcodec:h265"]
    if has_impersonate():
        cmd += ["--impersonate", "chrome"]
    if cfg.cookies_path.exists():
        cmd += ["--cookies", str(cfg.cookies_path)]
    if cfg.proxy:
        cmd += ["--proxy", cfg.proxy]
    if cfg.write_thumbnail:
        cmd += ["--write-thumbnail"]
        if has_ffmpeg():  # 否则 TikTok 的封面会存成认不出类型的 .image
            cmd += ["--convert-thumbnails", "jpg"]
    if cfg.write_info_json:
        cmd += ["--write-info-json"]
    if cfg.write_subs:
        cmd += ["--write-subs", "--sub-langs", "all"]
        if has_ffmpeg():  # 没 ffmpeg 就保留原始 vtt，别让转换环节整个失败
            cmd += ["--convert-subs", "srt"]
    return cmd


def _download_one(cfg: Config, row, target_name: str) -> tuple[str, bool, str, int]:
    time.sleep(random.uniform(cfg.delay_min, cfg.delay_max))
    try:
        p = subprocess.run(
            build_cmd(cfg, row, target_name),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900,
        )
    except subprocess.TimeoutExpired:
        return row["aweme_id"], False, "timeout: 900s", 0

    if p.returncode != 0:
        err = (p.stderr or p.stdout or "unknown error").strip().splitlines()
        return row["aweme_id"], False, " | ".join(err[-3:]), 0

    found: Path | None = None
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line and Path(line).suffix.lower() in VIDEO_EXT and Path(line).exists():
            found = Path(line)
    if found is None:
        found = _find_existing(cfg, row, target_name)
    if found is None or not found.stat().st_size:
        return row["aweme_id"], False, "yt-dlp 退出码 0 但没找到产出文件", 0

    size = found.stat().st_size
    if cfg.video_codec == "transcode" and transcode.needs_transcode(found):
        try:
            size = transcode.to_h264(found, cfg.transcode_crf)
        except transcode.TranscodeError as e:
            # 转码失败不算下载失败 —— 文件在，只是还是 H.265
            return row["aweme_id"], True, str(found), found.stat().st_size
    return row["aweme_id"], True, str(found), size


def run(
    cfg: Config,
    store: Store,
    target_id: str | None = None,
    target_name: str = "misc",
    on_event: Callable[[str, str], None] | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """返回 (成功数, 失败数)。"""
    rows = store.pending(target_id, cfg.max_retry)
    if limit:
        rows = rows[:limit]
    if not rows:
        return 0, 0

    ok = bad = 0
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
        futures = {
            pool.submit(_download_one, cfg, r, target_name): r for r in rows
        }
        for fut in as_completed(futures):
            aid, success, info, size = fut.result()
            if success:
                store.mark_done(aid, info, size)
                ok += 1
                msg = f"OK   {aid}  {size/1048576:.1f}MB"
            else:
                store.mark_failed(aid, info)
                bad += 1
                msg = f"FAIL {aid}  {info[:120]}"
            if on_event:
                with _print_lock:
                    on_event("ok" if success else "fail", msg)
    return ok, bad
