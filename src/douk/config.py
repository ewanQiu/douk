"""配置加载：读 config.toml，路径一律解析成绝对路径。"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.toml"

class ConfigError(RuntimeError):
    """配置文件有问题 —— 带上人能看懂的修法，别甩 traceback。"""


def _explain_toml_error(err: Exception, text: str, path: Path) -> str:
    """把 tomllib 的报错翻译成「哪一行、什么毛病、怎么改」。"""
    msg = str(err)
    lineno = getattr(err, "lineno", None)
    if lineno is None:
        if m := re.search(r"at line (\d+)", msg):
            lineno = int(m.group(1))

    out = [f"{path} 解析失败：{msg}"]
    if lineno:
        lines = text.splitlines()
        if 1 <= lineno <= len(lines):
            bad = lines[lineno - 1]
            out.append(f"  第 {lineno} 行: {bad.strip()}")
            # 最常见的坑：Windows 路径写进双引号，\ 被当成转义符
            if "Unescaped" in msg or "\\" in bad:
                if m := re.match(r'\s*(\w+)\s*=\s*"(.*)"\s*$', bad):
                    key, val = m.group(1), m.group(2)
                    out.append(f"  改成单引号: {key} = '{val}'")
                    out.append(f"  或用正斜杠: {key} = \"{val.replace(chr(92), '/')}\"")
                else:
                    out.append("  Windows 路径请用单引号 'X:\\drama'，"
                               "双引号里的 \\ 会被当成转义符。")
    return "\n".join(out)


CODECS = ("h264", "h265", "transcode")
_CODEC_ALIASES = {"best": "h265", "hevc": "h265", "h.265": "h265",
                  "avc": "h264", "h.264": "h264", "264": "h264", "265": "h265"}


def normalize_codec(value: str) -> str:
    """把各种写法归一到 h264 / h265 / transcode，非法值直接报错。"""
    v = str(value or "").strip().lower()
    v = _CODEC_ALIASES.get(v, v)
    if v not in CODECS:
        raise ValueError(f"video_codec 只能是 {'/'.join(CODECS)}，收到: {value!r}")
    return v


@dataclass
class Config:
    root: Path
    proxy: str = ""
    user_agent: str = ""

    user_data_dir: Path = field(default_factory=lambda: Path("data/browser"))
    headless: bool = True
    channel: str = "chrome"
    scroll_wait_ms: int = 1200
    idle_rounds: int = 4
    max_scrolls: int = 400

    out_dir: Path = field(default_factory=lambda: Path("data/videos"))
    concurrency: int = 3
    delay_min: float = 1.0
    delay_max: float = 3.0
    max_retry: int = 3
    write_thumbnail: bool = True
    write_info_json: bool = True
    write_subs: bool = True
    group_by_mix: bool = True
    video_codec: str = "h264"      # h264 | h265 | transcode
    transcode_crf: int = 26
    filename_template: str = "第{seq}集"

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "douk.db"

    @property
    def cookies_path(self) -> Path:
        return self.root / "data" / "cookies.txt"

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.user_data_dir, self.out_dir):
            p.mkdir(parents=True, exist_ok=True)


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    root = path.resolve().parent
    raw = {}
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(_explain_toml_error(e, text, path)) from None

    net = raw.get("net", {})
    br = raw.get("browser", {})
    dl = raw.get("download", {})

    def abspath(v: str, fallback: str) -> Path:
        p = Path(v or fallback)
        return p if p.is_absolute() else (root / p)

    return Config(
        root=root,
        proxy=net.get("proxy", ""),
        user_agent=net.get("user_agent", ""),
        user_data_dir=abspath(br.get("user_data_dir", ""), "data/browser"),
        headless=bool(br.get("headless", True)),
        channel=br.get("channel", "chrome"),
        scroll_wait_ms=int(br.get("scroll_wait_ms", 1200)),
        idle_rounds=int(br.get("idle_rounds", 4)),
        max_scrolls=int(br.get("max_scrolls", 400)),
        out_dir=abspath(dl.get("out_dir", ""), "data/videos"),
        concurrency=int(dl.get("concurrency", 3)),
        delay_min=float(dl.get("delay_min", 1.0)),
        delay_max=float(dl.get("delay_max", 3.0)),
        max_retry=int(dl.get("max_retry", 3)),
        write_thumbnail=bool(dl.get("write_thumbnail", True)),
        write_info_json=bool(dl.get("write_info_json", True)),
        write_subs=bool(dl.get("write_subs", True)),
        group_by_mix=bool(dl.get("group_by_mix", True)),
        video_codec=normalize_codec(dl.get("video_codec", "h264")),
        transcode_crf=int(dl.get("transcode_crf", 26)),
        filename_template=str(dl.get("filename_template") or "第{seq}集"),
    )
