"""URL -> 采集目标。

支持四种输入：
  /@user/playlist/xxx-123   合集（TikTok 内部叫 mix）
  /@user/collection/xxx-123 收藏夹
  /@user                    主页全部作品
  /@user/video/123          单条视频 -> 反查它所属的合集，查不到就退化成主页
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext

from .browser import new_page

# 短剧的新地址形态：/shortdrama/episode/{dramaID}/{第几集}
# 这里的数字**直接就是 dramaID**（接口参数印证过），比旧的 /video/{aweme_id}
# 好办 —— 旧形态得开页面反查这条视频属于哪部剧，新形态正则就能拿到。
# 末尾集号可有可无，detail 变体也一并认。
RE_SHORTDRAMA = re.compile(
    r"tiktok\.com/(?:[\w-]+/)?shortdrama/(?:episode|detail)/(?P<id>\d{8,})"
    r"(?:/(?P<ep>\d+))?"
)

RE_PLAYLIST = re.compile(r"tiktok\.com/@(?P<user>[\w.-]+)/playlist/(?P<slug>[^/?#]*?)-(?P<id>\d{8,})")
RE_COLLECT = re.compile(r"tiktok\.com/@(?P<user>[\w.-]+)/collection/(?P<slug>[^/?#]*?)-(?P<id>\d{8,})")
RE_VIDEO = re.compile(r"tiktok\.com/@(?P<user>[\w.-]+)/video/(?P<id>\d+)")
RE_USER = re.compile(r"tiktok\.com/@(?P<user>[\w.-]+)/?$")


@dataclass
class Target:
    kind: str          # mix | collection | user
    target_id: str
    url: str
    name: str = ""
    author_name: str = ""

    def __str__(self) -> str:
        return f"[{self.kind}] {self.name or self.target_id} <{self.url}>"


def _slug_name(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).strip().title()


# 剧集标题形如「剧名-第1集」「剧名 EP01」，直接拿来当目录名不合适
RE_EP_SUFFIX = re.compile(
    r"\s*[-–—|_]?\s*(?:第\s*\d+\s*[集话話]|EP\s*\.?\s*\d+|Episode\s*\d+)\s*$",
    re.IGNORECASE,
)


def strip_episode_suffix(title: str) -> str:
    """去掉标题尾部的集数标记，最多剥两层（有的标题会叠加）。"""
    name = (title or "").strip()
    for _ in range(2):
        stripped = RE_EP_SUFFIX.sub("", name).strip(" -–—|_")
        if stripped == name:
            break
        name = stripped
    return name or title


def parse_static(url: str) -> Target | None:
    """纯正则能定的，不开浏览器。

    短剧目标的 name 这里留空 —— 剧名只有接口才知道。resolve() 会补上，
    collect 阶段也会用接口返回的 dramaName 覆盖，最终落盘目录名是对的。
    """
    if m := RE_SHORTDRAMA.search(url):
        return Target("drama", m["id"], url, "", "")
    if m := RE_PLAYLIST.search(url):
        return Target("mix", m["id"], url, _slug_name(m["slug"]), m["user"])
    if m := RE_COLLECT.search(url):
        return Target("collection", m["id"], url, _slug_name(m["slug"]), m["user"])
    if m := RE_USER.search(url.split("?")[0]):
        u = m["user"]
        return Target("user", f"user:{u}", f"https://www.tiktok.com/@{u}", f"@{u}", u)
    return None


def drama_name(drama_id: str, url: str, ctx: BrowserContext) -> str:
    """开页面拿剧名。/shortdrama/ 地址里只有 ID，剧名得问接口。"""
    got: dict = {}

    def on_response(resp) -> None:
        if got.get("name"):
            return
        if "/api/drama/detail" in resp.url:
            try:
                di = (resp.json().get("dramaInfo") or {})
            except Exception:
                return
            if nm := (di.get("dramaName") or "").strip():
                got["name"] = nm
        elif "/api/drama/episode/item_list" in resp.url:
            try:
                items = resp.json().get("itemList") or []
            except Exception:
                return
            for it in items:
                di = it.get("dramaInfo") or {}
                if nm := (di.get("dramaName") or "").strip():
                    got["name"] = nm
                    return

    page = new_page(ctx)
    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded")
        for _ in range(25):
            if got.get("name"):
                break
            page.wait_for_timeout(1000)
    except Exception:
        pass
    finally:
        page.remove_listener("response", on_response)
    return got.get("name", "")


def resolve(url: str, ctx: BrowserContext) -> Target:
    """静态解析不出来（单条视频）时，开页面反查它属于哪部剧 / 哪个合集。"""
    if t := parse_static(url):
        # /shortdrama/ 地址正则只能拿到 ID，剧名补一下，否则落盘目录名会是一串数字
        if t.kind == "drama" and not t.name:
            t.name = drama_name(t.target_id, url, ctx) or f"drama {t.target_id}"
        return t

    m = RE_VIDEO.search(url)
    if not m:
        raise ValueError(f"无法识别的 TikTok 链接: {url}")
    user = m["user"]

    page = new_page(ctx)

    # 短剧是主流形态，且只能从接口反查 —— 页面上没有任何 <a href> 指向它
    drama: dict = {}

    def on_response(resp) -> None:
        if "/api/drama/episode/item_list" not in resp.url:
            return
        did = parse_qs(urlparse(resp.url).query).get("dramaID", [""])[0]
        if not did:
            return
        drama.setdefault("id", did)
        if drama.get("name"):
            return
        try:
            items = resp.json().get("itemList") or []
        except Exception:
            return
        for it in items:
            # dramaInfo.dramaName 是权威剧名。标题不可信 —— 有的作者写成
            # 'BabyTails EP01.mp4'，真实剧名却是 'Baby Tails'。
            di = it.get("dramaInfo") or {}
            if name := (di.get("dramaName") or "").strip():
                drama["name"] = name
                return
        # 接口没给才退回从标题剥集数后缀
        for it in items:
            if desc := (it.get("desc") or "").strip():
                drama["name"] = strip_episode_suffix(desc)
                break

    page.on("response", on_response)
    page.goto(url, wait_until="domcontentloaded")
    # 固定等 6 秒不够稳 —— 剧集接口慢一点就抓不到，会被误判成「不属于任何剧」
    # 而退化成抓整个主页。改成轮询到拿着为止，最多等 30 秒。
    for _ in range(30):
        if drama.get("id") and drama.get("name"):
            break
        page.wait_for_timeout(1000)
    page.remove_listener("response", on_response)

    if drama.get("id"):
        return Target("drama", drama["id"], url,
                      drama.get("name") or f"drama {drama['id']}", user)

    # 视频页上如果这条属于某合集，会渲染一个指向 /playlist/ 的锚点
    for href in page.eval_on_selector_all(
        "a[href*='/playlist/']", "els => els.map(e => e.href)"
    ):
        if t := parse_static(href):
            return t

    # 合集信息也可能只在 API 响应里（playlistId 字段）
    pid = page.evaluate(
        "() => { try { const s = document.getElementById("
        "'__UNIVERSAL_DATA_FOR_REHYDRATION__'); if (!s) return null;"
        " const d = JSON.parse(s.textContent);"
        " const it = d.__DEFAULT_SCOPE__?.['webapp.video-detail']?.itemInfo?.itemStruct;"
        " return it?.playlistId || it?.mixInfo?.mixId || null; } catch (e) { return null; } }"
    )
    if pid:
        u = f"https://www.tiktok.com/@{user}/playlist/x-{pid}"
        return Target("mix", str(pid), u, f"playlist {pid}", user)

    # 退化：这条视频不属于任何合集，改抓整个主页
    return Target("user", f"user:{user}", f"https://www.tiktok.com/@{user}",
                  f"@{user}", user)
