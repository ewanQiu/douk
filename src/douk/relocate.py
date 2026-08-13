"""把已下载的文件按当前命名规则重新归位。

改了 out_dir / filename_template / 剧名之后，老文件还留在旧路径旧名字上。
重下一遍太浪费，直接原地改名搬家即可，顺带把附属文件（字幕/封面/元数据）
一起带走 —— 它们的文件名都以视频名为前缀。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .downloader import VIDEO_EXT, _out_base, _stem
from .store import Store


@dataclass
class Move:
    seq: int
    aweme_id: str
    old: Path
    new: Path
    sidecars: int = 0
    skipped: str = ""


def plan(cfg: Config, store: Store, target_id: str | None = None) -> list[Move]:
    sql = ("SELECT a.*, t.name AS target_name FROM aweme a "
           "JOIN target t ON t.target_id = a.target_id "
           "WHERE a.status='done' AND a.file_path IS NOT NULL")
    args: list = []
    if target_id:
        sql += " AND a.target_id=?"
        args.append(target_id)
    rows = store.db.execute(sql + " ORDER BY a.seq", args).fetchall()

    moves: list[Move] = []
    for r in rows:
        old = Path(r["file_path"])
        if not old.exists():
            continue
        base = _out_base(cfg, r["target_name"] or r["target_id"])
        new = base / f"{_stem(cfg, r, r['target_name'] or '')}{old.suffix}"
        m = Move(r["seq"] or 0, r["aweme_id"], old, new)
        if old.resolve() == new.resolve():
            continue                      # 已经在正确位置
        if new.exists():
            m.skipped = "目标已存在"
        moves.append(m)
    return moves


def apply(moves: list[Move], store: Store) -> tuple[int, int]:
    """执行搬家。返回 (成功, 跳过)。"""
    ok = skipped = 0
    for m in moves:
        if m.skipped:
            skipped += 1
            continue
        m.new.parent.mkdir(parents=True, exist_ok=True)

        # 附属文件（.srt/.jpg/.info.json）的名字都以视频文件名为前缀
        old_stem, new_stem = m.old.stem, m.new.stem
        for p in sorted(m.old.parent.iterdir()):
            if not p.is_file() or not p.name.startswith(old_stem):
                continue
            if p == m.old:
                continue
            dest = m.new.parent / (new_stem + p.name[len(old_stem):])
            if not dest.exists():
                # 必须用 shutil.move —— Path.rename 底层是 os.rename，
                # 跨盘符会抛 WinError 17「系统无法将文件移到不同的磁盘驱动器」
                shutil.move(str(p), str(dest))
                m.sidecars += 1

        shutil.move(str(m.old), str(m.new))
        store.mark_done(m.aweme_id, str(m.new), m.new.stat().st_size)
        ok += 1

    # 收拾空掉的旧目录
    for d in {m.old.parent for m in moves if not m.skipped}:
        try:
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return ok, skipped
