"""环境体检：登录失败十有八九是 IP 问题，先把 IP 查清楚再折腾代码。"""
from __future__ import annotations

from dataclasses import dataclass, field

from curl_cffi import requests

from .config import Config

IPAPI = ("http://ip-api.com/json/?fields=status,country,countryCode,isp,org,"
         "query,proxy,hosting")
CF_TRACE = "https://www.cloudflare.com/cdn-cgi/trace"

# TikTok 登录接口对这些 ISP/组织特别敏感，命中基本等于 get region err
FLAGGED = ("cloudflare", "warp", "google", "amazon", "aws", "azure",
           "digitalocean", "linode", "vultr", "oracle", "hetzner", "ovh")


@dataclass
class Report:
    ok: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)


def _get(url: str, proxy: str, timeout: int = 15):
    kw = {"impersonate": "chrome", "timeout": timeout}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    return requests.get(url, **kw)


def _check_deps(cfg: Config, r: Report) -> None:
    """外部依赖检查 —— 换机器落地时这部分最容易漏。"""
    import shutil
    import subprocess

    # yt-dlp：下载全靠它
    try:
        from .downloader import ytdlp_bin
        p = subprocess.run([ytdlp_bin(), "--version"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            r.ok.append(f"yt-dlp {p.stdout.strip()}")
        else:
            r.bad.append("yt-dlp 跑不起来")
    except Exception as e:
        r.bad.append(f"yt-dlp 不可用: {type(e).__name__}")

    # curl_cffi 与伪装可用性：版本超出 yt-dlp 支持区间时，--impersonate 的
    # 所有目标会变成 unavailable，伪装静默失效 —— 两台机器行为不一致时，
    # 这里往往就是差异点。
    try:
        import curl_cffi
        ver = curl_cffi.__version__
        parts = tuple(int(x) for x in ver.split(".")[:2])
        supported = ver == "0.5.10" or (0, 10) <= parts < (0, 16)
        try:
            from .downloader import has_impersonate
            active = has_impersonate()
        except Exception:
            active = False
        if supported and active:
            r.ok.append(f"curl_cffi {ver}，--impersonate 可用")
        elif not supported:
            r.warn.append(
                f"curl_cffi {ver} 超出 yt-dlp 支持区间(0.5.10 / 0.10.x-0.15.x)，"
                f"--impersonate 全部目标不可用。修：pip install \"curl_cffi>=0.10,<0.16\"")
        else:
            r.warn.append(f"curl_cffi {ver} 版本合规，但 --impersonate 探测不到可用目标")
    except ImportError:
        r.warn.append("没装 curl_cffi，--impersonate 不可用（有 cookie 时通常也能下）")

    # 浏览器：channel 指定时驱动本机真浏览器，否则用 Playwright 自带的
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            kw = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
            if cfg.channel:
                kw["channel"] = cfg.channel
            b = p.chromium.launch(**kw)
            ver = b.version
            b.close()
        r.ok.append(f"浏览器可启动: {cfg.channel or 'playwright 自带'} {ver}")
    except Exception as e:
        msg = str(e).split("\n")[0][:120]
        if cfg.channel:
            r.bad.append(
                f"启动 {cfg.channel} 失败: {msg}\n"
                f"         装 Chrome，或把 config.toml 的 channel 改成 \"\" "
                f"并跑 playwright install chromium"
            )
        else:
            r.bad.append(f"启动浏览器失败: {msg}\n"
                         f"         跑一次 playwright install chromium")

    # ffmpeg：只影响转码和封面，不装也能下载
    if shutil.which("ffmpeg"):
        r.ok.append("ffmpeg 可用（转码 / 封面转换正常）")
    else:
        r.warn.append("没有 ffmpeg：--codec transcode 和 douk convert 用不了，"
                      "封面会存成 .image。装法: winget install Gyan.FFmpeg")

    # 输出目录可写
    try:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.out_dir / ".douk_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.ok.append(f"输出目录可写: {cfg.out_dir}")
    except Exception as e:
        r.bad.append(f"输出目录不可写: {cfg.out_dir} ({type(e).__name__}) "
                     f"—— 改 config.toml 的 out_dir")


def run(cfg: Config) -> Report:
    r = Report()
    proxy = cfg.effective_proxy()
    if cfg.proxy:
        r.ok.append(f"代理配置: {cfg.proxy}")
    elif proxy:
        r.ok.append(f"代理配置: 未在 config.toml 指定，沿用系统/环境变量代理 {proxy}")
    else:
        r.ok.append("代理配置: 直连（config.toml 未填，系统与环境变量也没有代理）")

    _check_deps(cfg, r)

    # --- 出口 IP 归属 ---
    info = {}
    try:
        resp = _get(IPAPI, proxy)
        if resp.status_code == 200:
            info = resp.json()
    except Exception as e:
        r.bad.append(f"查询出口 IP 失败: {type(e).__name__}: {e}")

    # 已经有可用 cookie 的话，IP 的代理属性就无所谓了 —— 它只挡「在浏览器里
    # 登录」这一步。实测同一个 WARP IP，手动导出 cookie 后采集下载一切正常，
    # 所以这些只作提醒，不能算致命项，否则新机器落地时会误报失败。
    from .chrome_cookies import has_session
    logged_in = has_session(cfg.cookies_path)
    note = "（你已有可用 cookie，不影响使用，仅影响 douk login）" if logged_in else ""
    ip_level = r.warn if logged_in else r.bad

    if info.get("status") == "success":
        ip = info.get("query", "?")
        cc = info.get("countryCode", "?")
        isp = info.get("isp") or ""
        org = info.get("org") or ""
        r.ok.append(f"出口 IP: {ip}  {info.get('country','?')}({cc})  ISP: {isp or '?'}")

        if cc == "CN":
            r.bad.append("IP 在中国大陆 —— TikTok 完全不可达，必须配海外代理。")
        blob = f"{isp} {org}".lower()
        if hit := [k for k in FLAGGED if k in blob]:
            ip_level.append(
                f"IP 属于 {', '.join(hit)} 段（机房/匿名代理），"
                f"在浏览器里登录会报 get region err{note}"
            )
        elif info.get("proxy"):
            ip_level.append(f"该 IP 被标记为 proxy/VPN，浏览器登录可能失败{note}")
        if info.get("hosting"):
            r.warn.append("该 IP 属于机房段（hosting），风控概率略高。")

    # --- Cloudflare WARP 检测 ---
    try:
        t = dict(
            line.split("=", 1) for line in
            _get(CF_TRACE, proxy).text.strip().splitlines() if "=" in line
        )
        if t.get("warp") == "on":
            ip_level.append(f"Cloudflare WARP 开着 —— 浏览器登录多半过不去{note}")
        else:
            r.ok.append(f"WARP: off   Cloudflare 判定地区: {t.get('loc','?')}")
    except Exception as e:
        r.warn.append(f"Cloudflare trace 查询失败: {type(e).__name__}")

    # --- TikTok 可达性 ---
    try:
        resp = _get("https://www.tiktok.com/", proxy, timeout=20)
        if resp.status_code == 200:
            r.ok.append("tiktok.com 可达 (200)")
        else:
            r.warn.append(f"tiktok.com 返回 {resp.status_code}")
    except Exception as e:
        r.bad.append(f"tiktok.com 不可达: {type(e).__name__}: {e}")

    # --- 本地状态 ---
    if logged_in:
        n = sum(1 for ln in cfg.cookies_path.read_text(encoding="utf-8").splitlines()
                if ln and not ln.startswith("#"))
        r.ok.append(f"cookies.txt: {n} 条，含 sessionid")
    elif cfg.cookies_path.exists():
        r.bad.append("cookies.txt 里没有 sessionid —— 导出时漏了 HttpOnly cookie，"
                     "重新导一次（见 使用说明.md）")
    else:
        r.bad.append("还没有 cookies.txt —— 没有登录态下不了任何东西，见 使用说明.md")

    if any((cfg.user_data_dir / p).exists() for p in ("Default", "Local State")):
        r.ok.append(f"浏览器 profile 已存在: {cfg.user_data_dir}")
    else:
        r.warn.append("浏览器 profile 还没建立 —— 没登录过。")

    return r
