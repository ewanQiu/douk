"""短剧的封面图和简介落地。

封面链接是**签名且带过期时间**的（URL 里有 x-expires），所以采集完要尽快下载，
不能只把链接存进库里等以后再说 —— 过期后就 403 了。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .downloader import _out_base, _safe
from .store import Store

COVER_STEM = "cover"
META_NAME = "drama.json"
EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/avif": ".avif",
}


def _fmt_duration(sec: int | None) -> str:
    if not sec:
        return ""
    h, m = divmod(int(sec) // 60, 60)
    return f"{h}小时{m}分" if h else f"{m}分"


def fetch_cover(url: str, dest_dir: Path, proxy: str = "") -> Path | None:
    """下载封面。返回落地路径，失败返回 None。"""
    if not url:
        return None
    from curl_cffi import requests

    kw: dict = {"impersonate": "chrome", "timeout": 60,
                "headers": {"Referer": "https://www.tiktok.com/"}}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    try:
        r = requests.get(url, **kw)
    except Exception:
        return None
    if r.status_code != 200 or not r.content:
        return None

    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = EXT_BY_TYPE.get(ctype, ".jpg")
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 换扩展名时清掉旧的，免得 cover.jpg 和 cover.webp 并存
    for old in dest_dir.glob(f"{COVER_STEM}.*"):
        if old.suffix != ext:
            old.unlink(missing_ok=True)
    path = dest_dir / f"{COVER_STEM}{ext}"
    path.write_bytes(r.content)
    return path


def save(cfg: Config, store: Store, target_id: str) -> dict:
    """把封面和简介落到剧集目录。返回本次做了什么。"""
    row = store.get_target(target_id)
    if not row:
        raise ValueError(f"库里没有这个目标: {target_id}")

    name = row["name"] or target_id
    dest = _out_base(cfg, name)
    result: dict = {"dir": dest, "cover": None, "meta": None}

    if cover := fetch_cover(row["cover_url"] or "", dest, cfg.proxy):
        store.upsert_target(target_id, row["kind"], cover_path=str(cover))
        result["cover"] = cover

    stats = store.stats(target_id)
    meta = {
        "drama_id": target_id,
        "name": row["name"],
        "description": row["description"],
        "themes": [t.strip() for t in (row["themes"] or "").split(",") if t.strip()],
        "episodes": row["video_count"],
        "total_duration_sec": row["total_duration"],
        "total_duration_text": _fmt_duration(row["total_duration"]),
        "num_watched": row["num_watched"],
        "author": row["author_name"],
        "source_url": row["url"],
        "cover_file": result["cover"].name if result["cover"] else None,
        "downloaded": stats.get("done", 0),
    }
    dest.mkdir(parents=True, exist_ok=True)
    meta_path = dest / META_NAME
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    result["meta"] = meta_path
    return result


def describe(row) -> list[str]:
    """给 CLI 用的可读描述。"""
    lines = [
        f"剧名   : {row['name'] or '—'}",
        f"剧集ID : {row['target_id']}",
        f"作者   : {row['author_name'] or '—'}",
        f"集数   : {row['video_count'] or '—'}",
    ]
    if d := _fmt_duration(row["total_duration"]):
        lines.append(f"总时长 : {d}")
    if row["num_watched"]:
        lines.append(f"观看数 : {row['num_watched']:,}")
    if row["themes"]:
        lines.append(f"题材   : {row['themes']}")
    if row["description"]:
        lines.append(f"简介   : {row['description']}")
    if row["cover_path"]:
        lines.append(f"封面   : {row['cover_path']}")
    elif row["cover_url"]:
        lines.append("封面   : 尚未下载（跑 douk info -t <id> --save）")
    return lines
