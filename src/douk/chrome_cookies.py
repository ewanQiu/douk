"""从本机浏览器 profile 直接导入 TikTok 登录态。

前提：目标浏览器必须**完全退出**。Chrome 运行时会锁住 cookie 数据库，
读取会报 "Could not copy Chrome cookie database"（yt-dlp #7271）。
"""
from __future__ import annotations

from pathlib import Path

TIKTOK_DOMAINS = ("tiktok.com", ".tiktok.com", "www.tiktok.com")


class CookieImportError(RuntimeError):
    pass


def import_from_browser(browser: str, dest: Path, profile: str | None = None) -> int:
    """读浏览器 cookie 库，筛出 tiktok 的，写成 Netscape 格式。返回条数。"""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as e:  # pragma: no cover
        raise CookieImportError(f"yt-dlp 不可用: {e}") from None

    try:
        jar = extract_cookies_from_browser(browser, profile)
    except Exception as e:
        msg = str(e)
        if "Could not copy" in msg or "locked" in msg.lower():
            raise CookieImportError(
                f"{browser} 的 cookie 数据库被锁住了 —— 请**完全退出 {browser}**"
                "（任务栏托盘里也要退，后台进程全关）再重试。"
            ) from None
        raise CookieImportError(f"读取 {browser} cookie 失败: {type(e).__name__}: {e}") from None

    rows = []
    for c in jar:
        if not any(d in (c.domain or "") for d in ("tiktok.com",)):
            continue
        if not c.value:
            continue
        domain = c.domain or ""
        rows.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.path or "/",
            "TRUE" if c.secure else "FALSE",
            str(int(c.expires) if c.expires else 2147483647),
            c.name,
            c.value,
        ]))

    if not rows:
        raise CookieImportError(
            f"{browser} 里没找到任何 tiktok.com 的 cookie。"
            "确认你确实在这个浏览器里登录过 TikTok，且选对了 profile。"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "\n".join(["# Netscape HTTP Cookie File", f"# imported from {browser}", "", *rows])
        + "\n",
        encoding="utf-8",
    )
    return len(rows)


def from_cookie_header(header: str, dest: Path) -> tuple[int, list[str]]:
    """把 DevTools 里抄来的 Cookie 请求头转成 Netscape 格式。

    Network 面板的请求头包含全部 cookie（HttpOnly 的也在），所以这条路
    不需要任何扩展。输入形如 `a=1; b=2; c=3`，可带 `Cookie:` 前缀。
    返回 (条数, 登录相关 cookie 名列表)。
    """
    # Windows 记事本/PowerShell 存的 UTF-8 带 BOM，不剥掉会污染第一个 cookie 名
    header = header.lstrip("﻿").strip()
    if header.lower().startswith("cookie:"):
        header = header[len("cookie:"):].strip()
    if not header:
        raise CookieImportError("输入是空的。")

    auth = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard")
    rows, names, found = [], set(), []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value or name in names:
            continue
        names.add(name)
        if name in auth:
            found.append(name)
        rows.append("\t".join([
            ".tiktok.com", "TRUE", "/", "TRUE", "2147483647", name, value,
        ]))

    if not rows:
        raise CookieImportError(
            "没解析出任何 cookie。确认粘贴的是 `Cookie:` 请求头的值，"
            "形如 `name=value; name2=value2`。"
        )
    if not found:
        raise CookieImportError(
            f"解析出 {len(rows)} 条，但没有 sessionid —— 你抄的多半是响应头 "
            "`Set-Cookie` 或未登录状态的请求。请在已登录的 tiktok.com 页面上，"
            "从 Network 面板某个请求的 **Request Headers** 里找 `Cookie:` 那一行。"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "\n".join(["# Netscape HTTP Cookie File", "# from Cookie header", "", *rows]) + "\n",
        encoding="utf-8",
    )
    return len(rows), found


def chrome_user_data() -> Path | None:
    import os
    for env in ("LOCALAPPDATA", "APPDATA"):
        if base := os.environ.get(env):
            p = Path(base) / "Google" / "Chrome" / "User Data"
            if p.is_dir():
                return p
    return None


def chrome_is_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                             capture_output=True, text=True, timeout=20).stdout
        return "chrome.exe" in out.lower()
    except Exception:
        return False


# Chrome 127+ 用 App-Bound Encryption 保护 cookie 密钥，DPAPI 解不开（yt-dlp #10927）。
# 但 ABE 密钥是绑定到「应用」的，Playwright 启动的就是同一个 chrome.exe，
# 所以把密钥文件和 cookie 库搬进 douk 的 profile，让 Chrome 自己解就行。
# 只搬这两个必需文件，历史记录/密码/书签一概不碰。
PROFILE_FILES = [
    ("Local State", "Local State"),                              # 含加密密钥
    ("{profile}/Network/Cookies", "{profile}/Network/Cookies"),  # cookie 库
]


def import_profile(dest_user_data: Path, profile: str = "Default",
                   src_user_data: Path | None = None) -> list[str]:
    """把本机 Chrome 的密钥+cookie 复制进 douk 的 profile。返回复制的文件列表。"""
    import shutil

    src = src_user_data or chrome_user_data()
    if not src or not src.is_dir():
        raise CookieImportError("找不到本机 Chrome 的 User Data 目录。")
    if chrome_is_running():
        raise CookieImportError(
            "Chrome 还在运行 —— cookie 库被锁，复制出来会是残缺的。"
            "请完全退出 Chrome（托盘图标、后台进程全关）再重试。"
        )

    copied: list[str] = []
    for src_tpl, dst_tpl in PROFILE_FILES:
        s = src / src_tpl.format(profile=profile)
        d = dest_user_data / dst_tpl.format(profile=profile)
        if not s.exists():
            raise CookieImportError(f"源文件不存在: {s}")
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        copied.append(str(d.relative_to(dest_user_data)))
    return copied


def has_session(dest: Path) -> bool:
    """cookies.txt 里有没有真正的登录凭证。"""
    if not dest.exists():
        return False
    for line in dest.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in ("sessionid", "sessionid_ss") and parts[6]:
            return True
    return False
