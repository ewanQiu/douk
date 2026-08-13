"""采集器：打开目标页，滚动，拦截列表接口，把视频清单落库。

核心思路是不解析 DOM 也不自己签名 —— 页面每次翻页都会打一次列表接口，
我们只在 response 事件里把 JSON 捞出来。TikTok 改签名算法、改 DOM 都不影响。

TikTok 上「一整部剧」有三种承载方式，接口各不相同：
  合集 mix        /api/mix/item_list/
  收藏夹          /api/collection/item_list/
  短剧 drama      /api/drama/episode/item_list/   <- 需按 dramaID 过滤
主页作品则是      /api/post/item_list/
"""
from __future__ import annotations

import re
from typing import Callable
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Response

from .browser import new_page
from .config import Config
from .resolver import Target
from .store import Store

DRAMA_EPISODE_API = "/api/drama/episode/item_list"
DRAMA_LIST_API = "/api/drama/user/drama_list"

API_MARKERS = (
    "/api/mix/item_list",         # 合集
    "/api/post/item_list",        # 主页作品
    "/api/collection/item_list",  # 收藏夹
    "/api/playlist/item_list",
)

# 剧集面板是内部滚动容器，滚窗口没用，得直接把容器 scrollTop 推到底
SCROLL_PANELS_JS = """
() => {
  const els = [...document.querySelectorAll('div')].filter(e => {
    const s = getComputedStyle(e);
    return (s.overflowY === 'auto' || s.overflowY === 'scroll')
           && e.scrollHeight > e.clientHeight + 50;
  });
  els.forEach(e => { e.scrollTop = e.scrollHeight; });
  return els.length;
}
"""

# 翻页的正解：把捕获到的 item_list 请求 URL 改掉 cursor 直接重放。
# 实测签名参数（msToken / X-Gnarly）**不绑定 cursor**，所以改了照样 200。
# 这条路完全绕开 UI —— 分页标签时有时无（同一部剧两次加载，一次渲染出
# ['1-24','25-48']，一次一个都没有），靠点标签永远不稳。
FETCH_PAGE_JS = """
async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include'});
    const j = await r.json();
    const d0 = ((j.itemList || [])[0] || {}).dramaInfo || {};
    return {
      status: r.status,
      statusCode: j.statusCode,
      hasMore: !!j.hasMore,
      cursor: j.cursor,
      total: j.totalEpisodeCount,
      drama: {
        name: d0.dramaName || '',
        description: d0.description || '',
        cover: ((d0.cover || {}).urlList || [])[0] || '',
        themes: (d0.themes || []).map(t => t.tagVal).filter(Boolean),
        totalDuration: d0.totalDuration,
        numWatched: d0.numWatched,
        numVideos: d0.numVideos,
        authorUID: d0.authorUID || '',
      },
      items: (j.itemList || []).map(it => {
        const di = it.dramaInfo || {};
        const dvd = di.DramaVideoData || {};
        const v = it.video || {};
        const a = it.author || {};
        return {
          id: it.id,
          desc: it.desc || '',
          createTime: it.createTime,
          duration: v.duration,
          cover: v.cover || v.originCover,
          uniqueId: a.uniqueId || '',
          ep: dvd.EpisodeNumber,
          dramaName: di.dramaName || '',
        };
      }),
    };
  } catch (e) { return {err: String(e)}; }
}
"""

_SSR_ITEMS_JS = """
() => {
  try {
    const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
    if (!s) return [];
    const d = JSON.parse(s.textContent).__DEFAULT_SCOPE__ || {};
    const out = [];
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v && Array.isArray(v.itemList)) out.push(...v.itemList);
    }
    return out;
  } catch (e) { return []; }
}
"""


def drama_meta(it: dict) -> tuple[str, int | None]:
    """从条目里取权威的剧名和集号。

    接口的 dramaInfo 直接给了 dramaName 和 EpisodeNumber，比从标题里正则抠
    可靠得多 —— 有的作者会把标题写成 'BabyTails EP01.mp4'，剧名其实是
    'Baby Tails'，靠标题永远猜不对。
    """
    di = it.get("dramaInfo") or {}
    ep = (di.get("DramaVideoData") or {}).get("EpisodeNumber")
    try:
        ep = int(ep) if ep else None
    except (TypeError, ValueError):
        ep = None
    return (di.get("dramaName") or "").strip(), ep


def _normalize(it: dict, seq: int | None = None) -> dict | None:
    aid = it.get("id") or it.get("aweme_id")
    if not aid:
        return None
    author = it.get("author") or {}
    uniq = author.get("uniqueId") or author.get("unique_id") or ""
    video = it.get("video") or {}
    return {
        "aweme_id": str(aid),
        "seq": seq,
        "title": it.get("desc") or "",
        "create_time": it.get("createTime") or it.get("create_time"),
        "duration": video.get("duration") or (it.get("music") or {}).get("duration"),
        "author_name": uniq,
        "cover_url": video.get("cover") or video.get("originCover"),
        "web_url": f"https://www.tiktok.com/@{uniq}/video/{aid}" if uniq else
                   f"https://www.tiktok.com/embed/{aid}",
    }


def list_dramas(user_url: str, ctx: BrowserContext, cfg: Config) -> list[dict]:
    """列出一个账号下的全部短剧。"""
    found: dict[str, dict] = {}

    def on_response(resp: Response) -> None:
        if DRAMA_LIST_API not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        for d in data.get("dramaList") or []:
            if did := d.get("dramaID"):
                found[str(did)] = {
                    "drama_id": str(did),
                    "name": d.get("dramaName") or "",
                    "num_videos": d.get("numVideos") or 0,
                    "description": (d.get("description") or "")[:200],
                }

    page = new_page(ctx)
    page.on("response", on_response)
    page.goto(user_url, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)

    # 主页上的短剧卡片是滚出来的（剧集列表不是，那条走游标）
    idle = 0
    for _ in range(cfg.max_scrolls):
        n0 = len(found)
        page.evaluate(SCROLL_PANELS_JS)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(cfg.scroll_wait_ms)
        if len(found) == n0:
            idle += 1
            if idle >= cfg.idle_rounds:
                break
        else:
            idle = 0

    page.remove_listener("response", on_response)
    return sorted(found.values(), key=lambda d: d["name"])


def _collect_drama(
    target: Target,
    ctx: BrowserContext,
    store: Store,
    cfg: Config,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """短剧：捕获一次剧集接口的请求 URL，之后按 cursor 重放翻页。

    不点标签也不滚动 —— 那两条路都不稳：分页标签是动态渲染的，同一部剧
    两次加载可能一次有、一次完全没有；滚动更糟，视频页滚动会翻到下一个
    视频、整页换掉。而签名参数不绑定 cursor，重放请求是确定性的。
    """
    seen: dict[str, dict] = {}
    state: dict = {"total": 0, "name": "", "base_url": "", "meta": {}}

    def take(items, cursor: int) -> int:
        """收下一批条目，返回新增数。"""
        added = 0
        for i, raw in enumerate(items):
            aid = raw.get("id")
            if not aid or aid in seen:
                continue
            if nm := (raw.get("dramaName") or "").strip():
                state["name"] = state["name"] or nm
            uniq = raw.get("uniqueId") or ""
            seen[aid] = {
                "aweme_id": str(aid),
                "seq": raw.get("ep") or (cursor + i + 1),
                "title": raw.get("desc") or "",
                "create_time": raw.get("createTime"),
                "duration": raw.get("duration"),
                "author_name": uniq,
                "cover_url": raw.get("cover"),
                "web_url": (f"https://www.tiktok.com/@{uniq}/video/{aid}" if uniq
                            else f"https://www.tiktok.com/embed/{aid}"),
            }
            added += 1
        return added

    def on_response(resp: Response) -> None:
        """只为拿到一个可重放的请求 URL（含签名参数）。"""
        if DRAMA_EPISODE_API not in resp.url or state["base_url"]:
            return
        if parse_qs(urlparse(resp.url).query).get("dramaID", [""])[0] != target.target_id:
            return
        state["base_url"] = resp.url

    page = new_page(ctx)
    page.on("response", on_response)
    page.goto(target.url, wait_until="domcontentloaded")
    for _ in range(30):
        if state["base_url"]:
            break
        page.wait_for_timeout(1000)
    page.remove_listener("response", on_response)

    if _is_blocked(page):
        raise RuntimeError("命中验证码/风控。换个代理 IP，或用 --headful 手动过一次。")
    if not state["base_url"]:
        raise RuntimeError(
            "没等到剧集列表接口。多半是 cookie 过期——跑 `douk verify` 查一下。")

    cursor, page_no = 0, 0
    while page_no < 200:
        page_no += 1
        url = re.sub(r"([?&]cursor=)\d+", rf"\g<1>{cursor}", state["base_url"])
        res = page.evaluate(FETCH_PAGE_JS, url)
        if not res or res.get("err") or res.get("status") != 200:
            raise RuntimeError(f"翻页请求失败: {(res or {}).get('err') or res}")
        if res.get("statusCode") not in (0, None):
            raise RuntimeError(f"接口返回 statusCode={res.get('statusCode')}，"
                               "可能是登录态失效，跑 `douk verify` 查一下。")

        if t := res.get("total"):
            state["total"] = int(t)
        # 剧集元数据每页都带，取第一份有内容的即可
        if not state["meta"] and (d := res.get("drama")) and d.get("name"):
            state["meta"] = d
        items = res.get("items") or []
        take(items, cursor)
        if on_progress:
            on_progress(len(seen), page_no)

        if not res.get("hasMore") or not items:
            break
        try:
            nxt = int(res.get("cursor"))
        except (TypeError, ValueError):
            nxt = cursor + len(items)
        cursor = nxt if nxt > cursor else cursor + len(items)

    if not seen:
        raise RuntimeError("一集都没采到。跑 `douk verify` 查一下登录态。")

    total = state["total"]
    if total and len(seen) < total:
        raise RuntimeError(
            f"只采到 {len(seen)}/{total} 集，没采全（翻了 {page_no} 页）。"
            "可能是接口返回的 total 含未公开集数；用 --headful 看一眼剧集面板核对。"
        )

    # 接口给的 dramaName 才是权威剧名，覆盖掉从标题猜出来的那个
    if state["name"]:
        target.name = state["name"]
    meta = state["meta"] or {}
    store.upsert_target(
        target.target_id, "drama", name=target.name, url=target.url,
        author_name=target.author_name,
        video_count=total or len(seen),
        description=meta.get("description") or None,
        cover_url=meta.get("cover") or None,
        themes=", ".join(meta.get("themes") or []) or None,
        total_duration=_int_or_none(meta.get("totalDuration")),
        num_watched=_int_or_none(meta.get("numWatched")),
        author_uid=meta.get("authorUID") or None,
    )
    added = store.add_awemes(target.target_id, seen.values())
    return added


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def collect(
    target: Target,
    ctx: BrowserContext,
    store: Store,
    cfg: Config,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """滚到底为止。返回本次新增条数。"""
    if target.kind == "drama":
        return _collect_drama(target, ctx, store, cfg, on_progress)

    store.upsert_target(target.target_id, target.kind, name=target.name,
                        url=target.url, author_name=target.author_name)

    seen: dict[str, dict] = {}
    hit_api = {"n": 0}

    def on_response(resp: Response) -> None:
        if not any(mark in resp.url for mark in API_MARKERS):
            return
        try:
            data = resp.json()
        except Exception:
            return
        hit_api["n"] += 1
        for raw in (data.get("itemList") or data.get("items") or []):
            if item := _normalize(raw):
                seen.setdefault(item["aweme_id"], item)

    page = new_page(ctx)
    page.on("response", on_response)

    page.goto(target.url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    for raw in page.evaluate(_SSR_ITEMS_JS) or []:
        if item := _normalize(raw):
            seen.setdefault(item["aweme_id"], item)

    idle = 0
    for i in range(cfg.max_scrolls):
        before = len(seen)
        page.evaluate(SCROLL_PANELS_JS)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(cfg.scroll_wait_ms)

        if _is_blocked(page):
            raise RuntimeError("命中验证码/风控。换个代理 IP，或用 --headful 手动过一次。")

        if len(seen) == before:
            idle += 1
            if idle >= cfg.idle_rounds:
                break
        else:
            idle = 0
            if on_progress:
                on_progress(len(seen), i + 1)

    page.remove_listener("response", on_response)

    if not seen:
        raise RuntimeError(
            f"一条视频都没采到（拦到 {hit_api['n']} 次列表接口）。"
            "多半是 cookie 过期——跑 `douk verify` 查一下。"
        )

    added = store.add_awemes(target.target_id, seen.values())
    store.upsert_target(target.target_id, target.kind, video_count=len(seen))
    store.renumber(target.target_id)   # 非短剧没有准确集号，按发布时间正序编
    return added


def _is_blocked(page) -> bool:
    try:
        return bool(page.query_selector(
            "div[id*='captcha'], div[class*='captcha'], .captcha_verify_container"
        ))
    except Exception:
        return False
