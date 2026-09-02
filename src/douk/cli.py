"""douk 命令行入口。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import typer

from . import browser as br
from . import assets, chrome_cookies, collector, config, doctor, downloader
from . import resolver, transcode
from . import relocate as relocate_mod
from .store import Store

app = typer.Typer(add_completion=False, help="TikTok 合集批量归档工具")


def _load(cfg_path: Optional[Path], codec: Optional[str] = None) -> config.Config:
    try:
        cfg = config.load(cfg_path)
    except config.ConfigError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1) from None
    if codec:
        try:
            cfg.video_codec = config.normalize_codec(codec)
        except ValueError as e:
            typer.secho(str(e), fg="red")
            raise typer.Exit(1) from None
    cfg.ensure_dirs()
    return cfg


CODEC_HELP = "视频编码: h264(默认,576x1024,到处能播) / h265(1080p,播放器要支持) / transcode(1080p转H.264)"

# 失败原因决定该怎么办，指错方向比不给提示更坏：
#   提取器过时 -> 升级 yt-dlp（TikTok 改过响应格式，实测 2026.7.4 全挂、
#                 2026.8.19 正常）。这类必须排在最前，它最具体。
#   登录态失效 -> 重导 cookie，重跑一万次也没用
#   网络抖动   -> 直接重跑
STALE_EXTRACTOR = ("unexpected response", "unable to extract",
                   "please report this issue", "extractor")
AUTH_FAIL = ("403", "401", "forbidden", "no video formats", "unauthorized")
TRANSIENT = ("ssl", "connection", "timeout", "timed out", "curl", "reset",
             "temporarily", "eof", "incomplete")


def _hint_failures(store: Store, cfg: config.Config, target_id: Optional[str]) -> None:
    """按失败原因给出下一步，而不是笼统一句「再跑一次」。"""
    rows = store.failed_rows(target_id, cfg.max_retry, stuck_only=False)
    if not rows:
        return
    blob = " ".join((r["error"] or "").lower() for r in rows)
    stuck = sum(1 for r in rows if (r["retry"] or 0) >= cfg.max_retry)

    if any(k in blob for k in STALE_EXTRACTOR):
        _echo("  TikTok 改了响应格式，当前 yt-dlp 解析不了 —— 先升级它：", "yellow")
        # raw string：路径里的 \. \S \p 都不是合法转义，普通字符串会告警
        _echo(r"    .\.venv\Scripts\python.exe -m pip install -U yt-dlp", "cyan")
        _echo("  升完再跑一次本命令。这类错误和 cookie 无关，别去重导。", "bright_black")
    elif any(k in blob for k in AUTH_FAIL):
        _echo("  失败里有 403/无可用格式，多半是 cookie 过期 —— "
              "先跑 douk verify，失效就重新导出。", "yellow")
    elif any(k in blob for k in TRANSIENT):
        _echo("  失败都是网络类错误（SSL/超时/连接中断），瞬时问题，"
              "直接再跑一次本命令即可。", "yellow")
        _echo("  仍然频繁失败的话，把 config.toml 的 concurrency 调小、"
              "delay_min/delay_max 调大。", "bright_black")
    else:
        _echo("  再跑一次本命令会自动重试。", "yellow")

    if stuck:
        t = f" -t {target_id}" if target_id else ""
        _echo(f"  其中 {stuck} 条已达重试上限、不会再被自动捡起，"
              f"跑 douk retry{t} 让它们重新排队。", "yellow")


def _echo(msg: str, color: str = "") -> None:
    typer.secho(msg, fg=color or None)


@app.command(name="doctor")
def doctor_cmd(cfg_path: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """体检：出口 IP / WARP / TikTok 可达性 / 登录态。登录失败先跑这个。"""
    cfg = _load(cfg_path)
    rep = doctor.run(cfg)
    for line in rep.ok:
        _echo(f"  [OK]   {line}", "green")
    for line in rep.warn:
        _echo(f"  [WARN] {line}", "yellow")
    for line in rep.bad:
        _echo(f"  [BAD]  {line}", "red")
    if rep.bad:
        _echo("\n有致命项，先把上面红色的解决掉再登录。", "red")
        raise typer.Exit(1)
    _echo("\n环境没问题。", "green")


@app.command()
def verify(
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    headful: bool = typer.Option(False, "--headful"),
) -> None:
    """验证 cookies.txt 是否可用：注入后打开 TikTok，报出登录的是哪个账号。"""
    cfg = _load(cfg_path)
    if not cfg.cookies_path.exists():
        _echo(f"找不到 {cfg.cookies_path}。请先手动导出 cookie，见 README。", "red")
        raise typer.Exit(1)

    parsed = br.parse_netscape(cfg.cookies_path)
    names = {c["name"] for c in parsed}
    _echo(f"cookies.txt: {len(parsed)} 条 tiktok cookie", "cyan")
    missing = [n for n in ("sessionid", "sessionid_ss") if n not in names]
    if missing == ["sessionid", "sessionid_ss"]:
        _echo("里面没有 sessionid —— 导出时多半没勾上 HttpOnly cookie，或当时未登录。", "red")
        raise typer.Exit(1)

    with br.session(cfg, headless=not headful) as ctx:
        ok, how = br.ensure_session(ctx, cfg.cookies_path)
        _echo(f"注入结果: {how}", "green" if ok else "red")
        page = br.new_page(ctx)
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
        time.sleep(4)
        user = page.evaluate(
            "() => { try { const s = document.getElementById("
            "'__UNIVERSAL_DATA_FOR_REHYDRATION__'); if (!s) return null;"
            " const d = JSON.parse(s.textContent).__DEFAULT_SCOPE__;"
            " const u = d?.['webapp.app-context']?.user;"
            " return u && u.uid ? {uid: u.uid, nick: u.nickName || u.uniqueId} : null;"
            " } catch (e) { return null; } }"
        )
    if user:
        _echo(f"验证通过：TikTok 认出的账号是 {user.get('nick')} (uid {user.get('uid')})", "green")
        _echo("可以直接跑 douk sync 了。", "green")
    else:
        _echo("cookie 注入了，但页面没认出登录账号 —— cookie 可能已过期，重新导出一次。", "red")
        raise typer.Exit(1)


@app.command(name="import-profile")
def import_profile_cmd(
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    profile: str = typer.Option("Default", "--profile", help="Chrome profile 名"),
) -> None:
    """把本机 Chrome 的登录态搬进 douk（Chrome 必须完全退出）。

    只复制加密密钥和 cookie 库两个文件，不碰历史记录/密码/书签。
    适用于 `cookies --from chrome` 报 DPAPI 解密失败（Chrome 127+ 的 ABE 加密）。
    """
    cfg = _load(cfg_path)
    try:
        copied = chrome_cookies.import_profile(cfg.user_data_dir, profile)
    except chrome_cookies.CookieImportError as e:
        _echo(f"导入失败：{e}", "red")
        raise typer.Exit(1) from None
    for f in copied:
        _echo(f"  已复制 {f}", "bright_black")

    _echo("正在用 Chrome 打开验证登录态…", "cyan")
    with br.session(cfg, headless=True) as ctx:
        page = br.new_page(ctx)
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
        time.sleep(3)
        ok = br.is_logged_in(ctx)
        n = br.export_cookies(ctx, cfg.cookies_path)
    if not ok:
        _echo(f"复制完成但没检测到 sessionid（只导出 {n} 条，都是匿名访问新生成的）。", "yellow")
        _echo("原因：Chrome 127+ 的 App-Bound Encryption 密钥不只绑定应用，还绑定原始 "
              "user-data-dir 路径，换目录后解不开——这个办法对你的 Chrome 版本无效。", "yellow")
        _echo("改用 `douk login` 在 douk 自己的浏览器里登一次。", "cyan")
        raise typer.Exit(1)
    _echo(f"登录态导入成功，已导出 {n} 条 cookie -> {cfg.cookies_path}", "green")


@app.command()
def login(
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    timeout: int = typer.Option(900, help="等待手动登录的秒数"),
) -> None:
    """开一个有头浏览器让你手动登录 TikTok，登录态会被持久化下来。"""
    cfg = _load(cfg_path)
    _echo("正在打开浏览器。请在窗口里完成登录，登录成功后本命令会自动收尾。", "cyan")
    _echo("（不要手动关窗口；想中止按 Ctrl+C）", "bright_black")

    with br.session(cfg, headless=False) as ctx:
        page = br.new_page(ctx)
        try:
            page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded")
        except Exception as e:
            _echo(f"打开登录页失败：{e}", "red")
            raise typer.Exit(1)

        waited = 0
        while waited < timeout:
            # 不依赖任何单个 page —— 登录过程会开 OAuth 弹窗、会整页跳转
            if br.is_logged_in(ctx):
                break
            if not ctx.pages:
                _echo("浏览器窗口被关掉了，登录中止。", "red")
                raise typer.Exit(1)
            time.sleep(2)
            waited += 2
            if waited % 20 == 0:
                names = br.auth_cookie_names(ctx)
                _echo(f"  等待中… {waited}s / {timeout}s   已拿到的登录相关 cookie: "
                      f"{', '.join(names) if names else '（无）'}", "bright_black")

        if not br.is_logged_in(ctx):
            _echo(f"等了 {timeout}s 仍未检测到 sessionid，登录未完成。", "red")
            raise typer.Exit(1)

        n = br.export_cookies(ctx, cfg.cookies_path)
        _echo(f"登录成功，已导出 {n} 条 cookie -> {cfg.cookies_path}", "green")


@app.command()
def cookies(
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    from_browser: Optional[str] = typer.Option(
        None, "--from", help="从本机浏览器导入登录态: chrome / edge / firefox / brave"),
    profile: Optional[str] = typer.Option(None, "--profile", help="浏览器 profile 名"),
    from_header: Optional[Path] = typer.Option(
        None, "--from-header",
        help="从存有 DevTools `Cookie:` 请求头的文本文件生成（不需要装扩展）"),
) -> None:
    """导出 cookies.txt。默认读 douk 自己的 profile；也可从浏览器或 Cookie 请求头导入。"""
    cfg = _load(cfg_path)

    if from_header:
        if not from_header.exists():
            _echo(f"找不到文件: {from_header}", "red")
            raise typer.Exit(1)
        try:
            n, found = chrome_cookies.from_cookie_header(
                from_header.read_text(encoding="utf-8-sig", errors="replace"),
                cfg.cookies_path)
        except chrome_cookies.CookieImportError as e:
            _echo(f"转换失败：{e}", "red")
            raise typer.Exit(1) from None
        _echo(f"已写入 {n} 条 cookie -> {cfg.cookies_path}", "green")
        _echo(f"登录相关: {', '.join(found)}", "green")
        _echo("接着跑 `douk verify` 验证。", "cyan")
        raise typer.Exit(0)

    if from_browser:
        _echo(f"从 {from_browser} 导入（该浏览器必须已完全退出）…", "cyan")
        try:
            n = chrome_cookies.import_from_browser(
                from_browser, cfg.cookies_path, profile)
        except chrome_cookies.CookieImportError as e:
            _echo(f"导入失败：{e}", "red")
            raise typer.Exit(1) from None
        ok = chrome_cookies.has_session(cfg.cookies_path)
        _echo(f"已导入 {n} 条 tiktok cookie -> {cfg.cookies_path}", "green")
        _echo(f"登录态: {'有 (sessionid 已拿到)' if ok else '无 —— 该浏览器里没登录过 TikTok'}",
              "green" if ok else "yellow")
        raise typer.Exit(0 if ok else 1)

    with br.session(cfg, headless=False) as ctx:
        page = br.new_page(ctx)
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded")
        time.sleep(3)
        ok = br.is_logged_in(ctx)
        n = br.export_cookies(ctx, cfg.cookies_path)
        names = br.auth_cookie_names(ctx)
        _echo(f"登录态: {'有' if ok else '无'}   登录相关 cookie: "
              f"{', '.join(names) if names else '（无）'}", "green" if ok else "yellow")
        _echo(f"已导出 {n} 条 cookie -> {cfg.cookies_path}", "green" if n else "red")


@app.command()
def resolve(
    url: str,
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    headful: bool = typer.Option(False, "--headful", help="显示浏览器窗口"),
) -> None:
    """看看一条链接会被解析成什么采集目标（不下载）。"""
    cfg = _load(cfg_path)
    # 静态解析出的短剧只有 ID、没有剧名，这种还得开浏览器补名字
    t = resolver.parse_static(url)
    if t and t.name:
        _echo(str(t), "green")
        return
    with br.session(cfg, headless=not headful) as ctx:
        ok, how = br.ensure_session(ctx, cfg.cookies_path)
        _echo(f"登录态: {how}", "green" if ok else "yellow")
        _echo(str(resolver.resolve(url, ctx)), "green")


@app.command(name="collect")
def collect_cmd(
    url: str,
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    headful: bool = typer.Option(False, "--headful", help="显示浏览器窗口，便于过验证码"),
) -> None:
    """只采集清单入库，不下载。"""
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        with br.session(cfg, headless=not headful) as ctx:
            ok, how = br.ensure_session(ctx, cfg.cookies_path)
            _echo(f"登录态: {how}", "green" if ok else "yellow")
            if not ok:
                _echo("没有登录态，采集几乎必然为空。见 README「手动导出 cookie」。", "yellow")
            target = resolver.resolve(url, ctx)
            _echo(f"目标: {target}", "cyan")
            try:
                added = collector.collect(
                    target, ctx, store, cfg,
                    on_progress=lambda n, s: _echo(f"  已采 {n} 条（第 {s} 批）",
                                                   "bright_black"),
                )
            except RuntimeError as e:
                _echo(f"采集失败：{e}", "red")
                raise typer.Exit(1) from None
        # 封面链接带签名且会过期，采完立刻下，别拖
        if target.kind == "drama":
            try:
                res = assets.save(cfg, store, target.target_id)
                if res["cover"]:
                    _echo(f"  封面 -> {res['cover']}", "bright_black")
                _echo(f"  简介 -> {res['meta']}", "bright_black")
            except Exception as e:
                _echo(f"  封面/简介保存失败（不影响视频）: {type(e).__name__}: {e}",
                      "yellow")
        total = len(store.known_ids(target.target_id))
        _echo(f"采集完成：新增 {added} 条，累计 {total} 条。", "green")
    finally:
        store.close()


@app.command()
def download(
    target_id: Optional[str] = typer.Option(None, "--target", "-t"),
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多下载几条（试水用）"),
    codec: Optional[str] = typer.Option(None, "--codec", help=CODEC_HELP),
) -> None:
    """下载库里所有 pending/failed 的视频（可断点续跑）。"""
    cfg = _load(cfg_path, codec)
    store = Store(cfg.db_path)
    try:
        if not cfg.cookies_path.exists():
            _echo("没有 cookies.txt，TikTok 不会返回播放地址。先跑 douk login。", "red")
            raise typer.Exit(1)
        name = "misc"
        if target_id and (row := store.get_target(target_id)):
            name = row["name"] or target_id
        pending = store.pending(target_id, cfg.max_retry)
        n = min(len(pending), limit) if limit else len(pending)
        _echo(f"待下载 {len(pending)} 条，本次下 {n} 条，并发 {cfg.concurrency}…", "cyan")
        ok, bad = downloader.run(
            cfg, store, target_id, name, limit=limit,
            on_event=lambda kind, m: _echo("  " + m, "green" if kind == "ok" else "red"),
        )
        _echo(f"下载结束：成功 {ok}，失败 {bad}。",
              "green" if not bad else "yellow")
        if bad:
            _hint_failures(store, cfg, target_id)
    finally:
        store.close()


@app.command()
def sync(
    url: str,
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    headful: bool = typer.Option(False, "--headful"),
    collect_only: bool = typer.Option(False, "--collect-only"),
    codec: Optional[str] = typer.Option(None, "--codec", help=CODEC_HELP),
) -> None:
    """一条龙：解析 -> 采集 -> 下载。增量安全，重复跑只补新的。"""
    cfg = _load(cfg_path, codec)
    store = Store(cfg.db_path)
    try:
        with br.session(cfg, headless=not headful) as ctx:
            ok, how = br.ensure_session(ctx, cfg.cookies_path)
            _echo(f"登录态: {how}", "green" if ok else "yellow")
            if not ok:
                _echo("没有登录态，采集几乎必然为空。见 README「手动导出 cookie」。", "yellow")
            target = resolver.resolve(url, ctx)
            _echo(f"目标: {target}", "cyan")
            try:
                added = collector.collect(
                    target, ctx, store, cfg,
                    on_progress=lambda n, s: _echo(f"  已采 {n} 条（第 {s} 批）",
                                                   "bright_black"),
                )
            except RuntimeError as e:
                _echo(f"采集失败：{e}", "red")
                raise typer.Exit(1) from None
        # 封面链接带签名且会过期，采完立刻下，别拖
        if target.kind == "drama":
            try:
                res = assets.save(cfg, store, target.target_id)
                if res["cover"]:
                    _echo(f"  封面 -> {res['cover']}", "bright_black")
                _echo(f"  简介 -> {res['meta']}", "bright_black")
            except Exception as e:
                _echo(f"  封面/简介保存失败（不影响视频）: {type(e).__name__}: {e}",
                      "yellow")
        _echo(f"采集完成：新增 {added} 条。", "green")
        if collect_only:
            return
        ok, bad = downloader.run(
            cfg, store, target.target_id, target.name or target.target_id,
            on_event=lambda kind, m: _echo("  " + m, "green" if kind == "ok" else "red"),
        )
        _echo(f"下载结束：成功 {ok}，失败 {bad}。", "green" if not bad else "yellow")
        if bad:
            _hint_failures(store, cfg, target.target_id)
    finally:
        store.close()


@app.command()
def relocate(
    target_id: Optional[str] = typer.Option(None, "--target", "-t"),
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只看会怎么改，不动文件"),
) -> None:
    """把已下载的文件按当前 out_dir / filename_template / 剧名重新归位。

    改了命名规则后不用重下 —— 直接改名搬家，字幕封面元数据一起带走。
    """
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        moves = relocate_mod.plan(cfg, store, target_id)
        if not moves:
            _echo("所有文件都已在正确位置。", "green")
            raise typer.Exit(0)

        _echo(f"待处理 {len(moves)} 个文件：\n", "cyan")
        for m in moves[:5]:
            _echo(f"  {m.old}", "bright_black")
            _echo(f"    -> {m.new}" + (f"   [跳过: {m.skipped}]" if m.skipped else ""),
                  "yellow" if m.skipped else "green")
        if len(moves) > 5:
            _echo(f"  … 另有 {len(moves) - 5} 个", "bright_black")

        if dry_run:
            _echo("\n--dry-run，没有实际改动。", "cyan")
            raise typer.Exit(0)

        ok, skipped = relocate_mod.apply(moves, store)
        cars = sum(m.sidecars for m in moves)
        _echo(f"\n完成：搬运 {ok} 个视频 + {cars} 个附属文件，跳过 {skipped} 个。",
              "green" if not skipped else "yellow")
    finally:
        store.close()


@app.command()
def retry(
    target_id: Optional[str] = typer.Option(None, "--target", "-t", help="不填则处理全部"),
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    stuck_only: bool = typer.Option(
        False, "--stuck-only", help="只重置已达重试上限、download 不会再碰的那些"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只看有哪些，不改动"),
) -> None:
    """把失败的条目退回待下载，清零重试计数。

    `download` 会跳过 retry 已达 max_retry 的条目，跑这个命令让它们重新被捡起。
    """
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        rows = store.failed_rows(target_id, cfg.max_retry, stuck_only)
        if not rows:
            scope = "已达上限的" if stuck_only else ""
            _echo(f"没有{scope}失败条目。", "green")
            raise typer.Exit(0)

        stuck = sum(1 for r in rows if (r["retry"] or 0) >= cfg.max_retry)
        _echo(f"失败 {len(rows)} 条，其中 {stuck} 条已达上限"
              f"（max_retry={cfg.max_retry}，download 会跳过它们）。\n", "cyan")

        # 先按错误归类，重试前总该知道为什么失败
        buckets: dict[str, int] = {}
        for r in rows:
            key = (r["error"] or "未记录错误").strip().splitlines()[0][:80]
            buckets[key] = buckets.get(key, 0) + 1
        _echo("失败原因分布：", "cyan")
        for msg, n in sorted(buckets.items(), key=lambda kv: -kv[1])[:5]:
            _echo(f"  {n:>3} 条  {msg}", "bright_black")

        if dry_run:
            _echo("\n--dry-run，未改动。", "cyan")
            raise typer.Exit(0)

        n = store.reset_retry(target_id, cfg.max_retry, stuck_only)
        _echo(f"\n已重置 {n} 条为待下载。跑 douk download 重新尝试。", "green")
    finally:
        store.close()


@app.command()
def convert(
    target_id: Optional[str] = typer.Option(None, "--target", "-t"),
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    crf: Optional[int] = typer.Option(None, "--crf", help="画质，越小越清晰（默认读配置）"),
) -> None:
    """把已下载的 H.265 视频转成 H.264，解决 Windows 播不了的问题。"""
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        sql = "SELECT * FROM aweme WHERE status='done' AND file_path IS NOT NULL"
        args: list = []
        if target_id:
            sql += " AND target_id=?"
            args.append(target_id)
        rows = store.db.execute(sql + " ORDER BY seq", args).fetchall()
        if not rows:
            _echo("库里没有已下载的文件。", "yellow")
            raise typer.Exit(1)

        _echo(f"检查 {len(rows)} 个文件的编码…", "cyan")
        todo = []
        for r in rows:
            p = Path(r["file_path"])
            if p.exists() and transcode.needs_transcode(p):
                todo.append((r, p))
        if not todo:
            _echo("没有需要转码的文件（都已经是 H.264 了）。", "green")
            raise typer.Exit(0)

        _echo(f"需要转码 {len(todo)} 个。转码较慢，请耐心等…", "cyan")
        ok = bad = 0
        for r, p in todo:
            try:
                size = transcode.to_h264(p, crf if crf is not None else cfg.transcode_crf)
                store.mark_done(r["aweme_id"], str(p), size)
                ok += 1
                _echo(f"  OK   {r['seq']:>3}  {p.name[:50]}  {size/1048576:.1f}MB", "green")
            except transcode.TranscodeError as e:
                bad += 1
                _echo(f"  FAIL {r['seq']:>3}  {e}", "red")
        _echo(f"\n转码完成：成功 {ok}，失败 {bad}。", "green" if not bad else "yellow")
    finally:
        store.close()


@app.command()
def info(
    target_id: Optional[str] = typer.Option(None, "--target", "-t", help="不填则列出全部"),
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    save: bool = typer.Option(False, "--save", help="下载封面并生成 drama.json"),
) -> None:
    """看短剧的名称 / 简介 / 题材 / 封面等信息。"""
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        rows = ([store.get_target(target_id)] if target_id
                else [r for r in store.list_targets() if r["kind"] == "drama"])
        rows = [r for r in rows if r]
        if not rows:
            _echo("库里没有短剧。先跑 douk sync <链接>。", "yellow")
            raise typer.Exit(1)

        for i, row in enumerate(rows):
            if i:
                _echo("")
            for line in assets.describe(row):
                _echo("  " + line)
            if save:
                res = assets.save(cfg, store, row["target_id"])
                _echo(f"    -> 封面: {res['cover'] or '下载失败（链接可能已过期，重跑 collect）'}",
                      "green" if res["cover"] else "yellow")
                _echo(f"    -> 元数据: {res['meta']}", "green")
    finally:
        store.close()


@app.command()
def dramas(
    url: str,
    cfg_path: Optional[Path] = typer.Option(None, "--config", "-c"),
    headful: bool = typer.Option(False, "--headful"),
) -> None:
    """列出一个账号下的全部短剧（dramaID + 集数）。"""
    cfg = _load(cfg_path)
    user_url = url if "/video/" not in url else url.split("/video/")[0]
    with br.session(cfg, headless=not headful) as ctx:
        ok, how = br.ensure_session(ctx, cfg.cookies_path)
        _echo(f"登录态: {how}", "green" if ok else "yellow")
        items = collector.list_dramas(user_url, ctx, cfg)

    if not items:
        _echo("没找到短剧。这个号可能用的是合集(playlist)或普通作品。", "yellow")
        raise typer.Exit(1)

    _echo(f"\n共 {len(items)} 部短剧：\n", "cyan")
    for d in items:
        _echo(f"  {d['drama_id']:<22} {d['num_videos']:>3} 集   {d['name']}")
    _echo("\n用 douk sync <该剧任一集的视频链接> 抓取整部。", "bright_black")


@app.command()
def version() -> None:
    """看当前是什么版本 —— 判断该不该更新、能不能只覆盖部分文件时用得上。"""
    import subprocess
    from . import __version__

    root = config.ROOT
    _echo(f"douk {__version__}")

    # 从 git 仓库读；打包分发的副本没有 .git，改读打包时写入的 VERSION
    rev = ""
    if (root / ".git").exists():
        try:
            # 必须显式指定 utf-8：Windows 下 text=True 走系统 gbk，
            # 提交信息里的中文会直接 UnicodeDecodeError
            p = subprocess.run(["git", "-C", str(root), "log", "-1",
                                "--format=%h %cs %s"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            if p.returncode == 0:
                rev = p.stdout.strip()
        except Exception:
            pass
    elif (vf := root / "VERSION").exists():
        # utf-8-sig：PowerShell 写 utf8 会带 BOM，不剥掉会显示成一个乱字符
        rev = vf.read_text(encoding="utf-8-sig").strip()
    _echo(f"  代码版本: {rev or '未知（既非 git 仓库，也没有 VERSION 文件）'}")

    try:
        import yt_dlp
        _echo(f"  yt-dlp  : {yt_dlp.version.__version__}")
    except Exception:
        _echo("  yt-dlp  : 不可用", "yellow")
    _echo(f"  Python  : {sys.version.split()[0]}")


@app.command()
def status(cfg_path: Optional[Path] = typer.Option(None, "--config", "-c")) -> None:
    """看库里都有什么。"""
    cfg = _load(cfg_path)
    store = Store(cfg.db_path)
    try:
        rows = store.list_targets()
        if not rows:
            _echo("库是空的。先跑 douk sync <url>。", "yellow")
            return
        for r in rows:
            _echo(f"{r['kind']:<10} {r['target_id']:<24} {r['done']}/{r['known']}  {r['name']}")
        _echo(f"\n合计: {store.stats()}", "cyan")
    finally:
        store.close()


if __name__ == "__main__":
    app()
