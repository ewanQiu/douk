"""H.265 -> H.264 转码。

TikTok 的 1080p 只有 H.265(bytevc1)，而 Windows 自带播放器要装付费的 HEVC 扩展
才能解。转成 H.264 后到处都能播，代价是一次 CPU 开销和轻微画质损失。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class TranscodeError(RuntimeError):
    pass


def ffprobe_codec(path: Path) -> str | None:
    """读出视频流的编码名，读不出返回 None。"""
    if not shutil.which("ffprobe"):
        return None
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return (p.stdout or "").strip().splitlines()[0].strip() or None
    except Exception:
        return None


def needs_transcode(path: Path) -> bool:
    return ffprobe_codec(path) in ("hevc", "h265", "bytevc1")


def to_h264(path: Path, crf: int = 26, preset: str = "medium") -> int:
    """H.264 编码效率不如 H.265，转完必然变大 —— 实测 CRF 23/26/28 分别是源文件的
    3.1x / 2.3x / 1.9x。CRF 26 + medium 是画质、体积、耗时的平衡点；preset slow
    只小 5% 却慢 30%，不值得。
    """
    """原地转码（先写临时文件，成功后再替换）。返回新文件大小。"""
    if not shutil.which("ffmpeg"):
        raise TranscodeError("找不到 ffmpeg，无法转码。")

    tmp = path.with_suffix(".h264.tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(path),
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",          # 保证老播放器也认
        "-c:a", "copy",                 # 音轨本来就是 aac，不用动
        "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise TranscodeError("转码超时（1 小时）") from None

    if p.returncode != 0 or not tmp.exists() or not tmp.stat().st_size:
        tmp.unlink(missing_ok=True)
        err = (p.stderr or "").strip().splitlines()
        raise TranscodeError(" | ".join(err[-2:]) or "ffmpeg 失败")

    size = tmp.stat().st_size
    # 替换原文件：先删原文件再改名，避免同名冲突
    path.unlink(missing_ok=True)
    tmp.rename(path)
    return size
