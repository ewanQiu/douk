"""Playwright 会话：持久化登录态 + 导出 cookies.txt 给 yt-dlp 用。

为什么必须用真浏览器：TikTok 的列表接口带 msToken / X-Gnarly 签名，
匿名 HTTP 请求拿不到 playAddr，也列不出合集。让页面自己去算签名，
我们只负责拦响应 —— 签名算法怎么改都不影响。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import Config

# 只保留真正能「去掉」自动化标记的参数。--no-sandbox 之类反而是特征，不要加。
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# 刻意做得极简：伪造 navigator.plugins / languages 这类反而更容易被指纹识别
# （伪造出来的对象类型对不上真实的 PluginArray），只保底抹掉 webdriver 标记。
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""


_UA_CACHE: dict[str, str] = {}


def _headful_ua(p, kwargs: dict) -> str | None:
    """读出本机 Chrome 的真实 UA 并去掉 Headless 前缀。

    用一个非持久化的临时实例来读，避免和正式的 persistent context 抢同一个
    user_data_dir。结果按 channel 缓存，一个进程只付一次启动开销。
    """
    channel = kwargs.get("channel") or "_bundled"
    if channel in _UA_CACHE:
        return _UA_CACHE[channel]

    launch: dict = {"headless": True, "args": LAUNCH_ARGS,
                    "ignore_default_args": ["--enable-automation"]}
    if kwargs.get("channel"):
        launch["channel"] = kwargs["channel"]
    try:
        b = p.chromium.launch(**launch)
        try:
            ua = b.new_page().evaluate("() => navigator.userAgent")
        finally:
            b.close()
    except Exception:
        return None

    if not ua or "Headless" not in ua:
        return None
    _UA_CACHE[channel] = ua.replace("HeadlessChrome", "Chrome")
    return _UA_CACHE[channel]


@contextlib.contextmanager
def session(cfg: Config, headless: bool | None = None) -> Iterator[BrowserContext]:
    cfg.ensure_dirs()
    headless = cfg.headless if headless is None else headless
    proxy = {"server": cfg.proxy} if cfg.proxy else None

    kwargs: dict = dict(
        user_data_dir=str(cfg.user_data_dir),
        headless=headless,
        args=LAUNCH_ARGS,
        # Playwright 默认带 --enable-automation，这是最直白的自动化标记
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    # channel="chrome" 会驱动本机安装的真 Chrome，而不是 Playwright 自带的
    # Chrome for Testing 构建 —— 指纹差异小很多，TikTok 登录能不能过多半看这个。
    if cfg.channel:
        kwargs["channel"] = cfg.channel
    if proxy:
        kwargs["proxy"] = proxy
    if cfg.user_agent:
        kwargs["user_agent"] = cfg.user_agent

    with sync_playwright() as p:
        if headless and not cfg.user_agent:
            # headless 下 UA 会自报 HeadlessChrome/xxx，这是最后一个明显破绽。
            # 先起一个探测实例读出真实 UA，去掉 Headless 前缀再正式启动。
            if ua := _headful_ua(p, kwargs):
                kwargs["user_agent"] = ua

        ctx = p.chromium.launch_persistent_context(**kwargs)
        ctx.add_init_script(STEALTH_JS)
        ctx.set_default_timeout(60_000)
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()


def new_page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


AUTH_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")


def auth_cookie_names(ctx: BrowserContext) -> list[str]:
    """诊断用：看看目前到底拿到了哪些登录相关 cookie。"""
    try:
        return sorted({c["name"] for c in ctx.cookies()
                       if c["name"] in AUTH_COOKIES and c.get("value")})
    except Exception:
        return []


def is_logged_in(ctx: BrowserContext) -> bool:
    try:
        return any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value")
                   for c in ctx.cookies())
    except Exception:
        return False


def parse_netscape(path: Path) -> list[dict]:
    """解析 Netscape 格式 cookies.txt -> Playwright 的 cookie dict。

    兼容各家扩展的写法：#HttpOnly_ 前缀、空行、CRLF、多余空白。
    """
    out: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            line, http_only = line[len("#HttpOnly_"):], True
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()          # 少数扩展用空格分隔
            if len(parts) < 7:
                continue
        domain, _sub, cpath, secure, expires, name, value = parts[:7]
        if "tiktok" not in domain:
            continue
        try:
            exp = int(float(expires))
        except ValueError:
            exp = 0
        out.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": cpath or "/",
            "expires": exp if exp > 0 else -1,   # -1 = 会话 cookie
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
            "sameSite": "None" if secure.upper() == "TRUE" else "Lax",
        })
    return out


def load_cookies(ctx: BrowserContext, path: Path) -> int:
    """把 cookies.txt 注入浏览器上下文。返回注入条数。"""
    if not path.exists():
        return 0
    cookies = parse_netscape(path)
    if cookies:
        ctx.add_cookies(cookies)
    return len(cookies)


def ensure_session(ctx: BrowserContext, cookies_path: Path) -> tuple[bool, str]:
    """确保上下文里有登录态。

    **cookies.txt 优先，profile 只是缓存。** 顺序反过来会有个隐蔽的坑：
    cookie 过期后你重新导出一份新的，profile 里的旧 session 却会一直赢，
    新文件被无视，而输出只说「使用 profile 自带的登录态」，根本看不出问题。
    add_cookies 按 name+domain+path 覆盖，所以注入新的会盖掉旧的。

    返回 (是否登录, 说明)。
    """
    n = load_cookies(ctx, cookies_path)
    if n and is_logged_in(ctx):
        return True, f"已从 cookies.txt 注入 {n} 条 cookie"
    if is_logged_in(ctx):
        # cookies.txt 缺失或没有 sessionid，退回 profile 里的旧登录态
        why = "cookies.txt 里没有 sessionid" if n else "没有 cookies.txt"
        return True, f"{why}，改用 profile 里的旧登录态（建议重新导出 cookie）"
    if not n:
        return False, "没有 cookies.txt，且 profile 里也没有登录态"
    return False, f"注入了 {n} 条 cookie，但其中没有 sessionid"


def export_cookies(ctx: BrowserContext, dest: Path) -> int:
    """导出 Netscape 格式 cookies.txt，yt-dlp --cookies 直接吃。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", "# generated by douk", ""]
    n = 0
    for c in ctx.cookies():
        domain = c.get("domain", "")
        if "tiktok" not in domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:  # 会话 cookie，给个远期过期时间否则 yt-dlp 会丢掉
            expires = 2147483647
        lines.append(
            "\t".join([domain, include_sub, c.get("path", "/"), secure,
                       str(expires), c["name"], c["value"]])
        )
        n += 1
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n
