# douk

**简体中文** | [English](README.md)

批量归档 TikTok 的**短剧（drama）**、合集（playlist / mix）、主页作品、收藏夹。
采集与下载分离，可断点续跑、可增量更新。

**短剧是当下最常见的形态** —— 一个短剧号会有几十部剧、每部几十集，界面上叫
"N Episodes"。它和合集是两套不同的产品，接口也不同，见下方「四种形态」。

## 为什么是这个架构

实测（2026-08）确认了三件事，直接决定了实现路线：

1. **yt-dlp 没有短剧/合集提取器。** 它的 `tiktok:collection` 只匹配
   `/@user/collection/...`（收藏夹），既不匹配合集也不认短剧。「列清单」必须自己做。
2. **匿名请求拿不到播放地址。** 视频页 SSR 里 `playAddr` / `downloadAddr` 都是空串，
   `bitrateInfo` 是空数组，这也是 yt-dlp 报 `No video formats found` 的真正原因。
   登录后同一个字段就有值了（实测 `bitrateInfo` 变成 9 档）。
3. **列表接口需要签名参数。** 自己实现签名维护成本高，交给真浏览器算最省事。

结论：**登录态是硬门槛**，而签名交给真浏览器自己算最省事。所以：

```
Playwright（真浏览器，channel="chrome"）
   ├─ 注入 cookies.txt 取得登录态
   ├─ 采集：在页面上下文里按游标逐页读取列表接口 → SQLite
   └─ 按 dramaID 过滤、集号取自接口的 EpisodeNumber
                    ↓
yt-dlp（带 cookie）并发下载 → data/videos/{剧名}/第1集.mp4
```

好处是 TikTok 改签名算法、改 DOM 结构都不影响采集链路。

不过**采集链路稳不代表下载链路稳**：下载走 yt-dlp，它的 TikTok 提取器依赖
页面响应格式，TikTok 一改就得等上游更新。实测 2026.7.4 全部报
`Unexpected response from webpage request`，升到 2026.8.19 即恢复。
所以 `requirements.txt` 里的 yt-dlp 下限别下调，遇到这个错先升级它。

`curl_cffi` 的版本上界也别放宽：yt-dlp 只支持 0.5.10 与 0.10.x–0.15.x，
装 0.16+ 会被判为 unsupported，`--impersonate` 所有目标变成 unavailable、
伪装静默失效。代码里 `has_impersonate()` 会先探测再决定是否传这个参数，
免得在不支持的环境下每个任务直接失败。

## 安装

```powershell
.\setup.ps1
```

自动建 venv、装依赖、检查 Chrome/ffmpeg 并自检。手动装的话：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 只有在没装 Chrome、要用 Playwright 自带浏览器时才需要这句
.\.venv\Scripts\python.exe -m playwright install chromium
```

需要 **Python 3.11+**（用到 `tomllib`）。

## 迁移到别的机器

```powershell
.\package.ps1        # 生成 douk-package.zip，约 0.05 MB
```

只打包源码和配置。`.venv`（164 MB）里写死了本机绝对路径、`data\browser`（231 MB）
是本机浏览器状态，两者都必须在目标机重建，拷过去没有意义。
目标机解压后跑 `.\setup.ps1` 即可。详见 [使用说明.md](使用说明.md) 第九节。

> `package.ps1` / `setup.ps1` 必须存成 **UTF-8 with BOM**。Windows PowerShell 5.1
> 读 `.ps1` 时按系统 ANSI（中文环境是 cp936）解码，无 BOM 的 UTF-8 中文注释会
> 变成乱码并撞坏语法解析。

## 配置

编辑 `config.toml`。

**前提是你本来就能访问 TikTok。** 大陆网络直连不通，得先自己解决这件事 ——
douk 不提供任何翻墙能力。

能访问之后，`proxy` 填什么取决于**流量怎么走**，而不是你用哪个软件
（同一个 Clash 两种模式都能跑）：

| 流量走法 | `proxy` |
|---|---|
| 透明转发 —— VPN 的 TUN 模式、WARP、路由器分流 | **留空**。socket 已经被接管，douk 根本看不到代理 |
| 本地监听端口 —— 客户端界面上写着 7890 之类的端口 | 填那个地址，如 `http://127.0.0.1:7890` |
| 系统代理 —— 客户端勾了「设为系统代理」 | **留空**，douk 会自己读系统设置 |

留空不等于直连。解析顺序是：这一项 → `HTTPS_PROXY` / `HTTP_PROXY` 环境变量
→ 系统代理设置。浏览器、yt-dlp、`doctor`、封面下载四条发请求的路径共用这个
解析结果，不会互相打架。

```powershell
.\douk doctor
```

会打印实际生效的代理并测 TikTok 连通性。线路质量也有影响：住宅 IP 比机房 IP
稳得多，机房段容易吃验证码。

## 用法

```powershell
# 0) 先体检 —— 登录失败九成是 IP 问题，别急着改代码
.\.venv\Scripts\python.exe douk.py doctor

# 1) 拿登录态 —— 见下方「登录态怎么搞」，手动导出最稳
#    导出好 cookies.txt 放进 data/ 之后，验证一下：
.\.venv\Scripts\python.exe douk.py verify

# 2) 一条龙：解析 -> 采集 -> 下载
.\.venv\Scripts\python.exe douk.py sync "https://www.tiktok.com/@user/playlist/xxx-7123456789"

# 其它
douk.py resolve <url>          # 只看链接会被解析成什么目标
douk.py collect <url>          # 只采清单不下载（加 --headful 可看着过验证码）
douk.py download -t <targetId> # 下载/重试库里 pending+failed 的
douk.py status                 # 看库存
```

`sync` 可重复跑：已入库的视频不会重复下载，只补新增的——增量更新就是再跑一次。

支持的链接形态：

| 输入 | 行为 |
|---|---|
| `/shortdrama/episode/123/1` | 短剧独立页。URL 里那串数字**就是 dramaID**，无需反查 |
| `/@user/video/123` | 反查该视频属于哪部剧 → 抓整部；不属于任何剧则退化为采主页 |
| `/@user/playlist/xxx-123` | 采整个合集 |
| `/@user/collection/xxx-123` | 采整个收藏夹 |
| `/@user` | 采主页全部作品 |

先看一个号有哪些剧：

```powershell
.\.venv\Scripts\python.exe douk.py dramas "https://www.tiktok.com/@某账号"
```

## 四种形态

TikTok 上「一整部剧」有四种承载方式，接口完全不同，douk 都支持：

| 形态 | 接口 | 说明 |
|---|---|---|
| **短剧 drama** | `/api/drama/episode/item_list/` | 当前主流。页面上是 `creator-drama-card`，**包在 `<button>` 里而非 `<a href>`**，所以页面上找不到任何链接，只能从接口反查 `dramaID` |
| 合集 mix | `/api/mix/item_list/` | 老形态，URL 形如 `/playlist/名字-{mixId}` |
| 收藏夹 | `/api/collection/item_list/` | 用户自建收藏 |
| 主页作品 | `/api/post/item_list/` | 该号全部视频 |

短剧采集有三个坑，都已处理：

- **翻页在页面上下文里按游标推进，不驱动 UI。** 采集器让页面自己发起分页请求，
  逐页读取直到 `hasMore` 为假。这样做是因为两条基于 UI 的路都被实测证明不可靠：

  - *滚动*：视频页滚动会翻到下一个视频、整页换掉，越滚越漂离目标剧。
  - *点分页标签*：`1-24 / 25-48 / 49-72` 这些标签是动态渲染的，同一部剧两次
    加载，一次渲染出两个标签、一次一个都没有。靠它翻页会稳定地少最后几集。

  UI 怎么变都不影响这条链路，这是选它的主要理由。

- **必须按 `dramaID` 过滤。** 剧集列表读到底会自动接上下一部剧，不过滤的话两部剧
  的集数会混在一起（实测一次就多混进 24 集）。
- **集号取自接口的 `EpisodeNumber`，不要按发布时间排。** 同一部剧的几十集往往是
  同一时刻批量发布的，按 `createTime` 排会得到随机顺序。

## 登录态怎么搞

**登录态是硬门槛** —— 没有它，TikTok 连列表接口都不打，`playAddr` 也是空的。

**操作步骤见 [使用说明.md](使用说明.md) 第一节**（扩展导出 / DevTools 抄请求头两种办法），
这里只记技术要点：

- **唯一的凭证文件是 `data/cookies.txt`。** 下载器直接 `--cookies` 喂给 yt-dlp，
  采集器启动后注入浏览器上下文。`data/browser/` 只是运行缓存，删了会自动重建。
- **`cookies.txt` 优先于 profile 缓存。** 反过来会有个隐蔽的坑：cookie 过期后
  重新导出一份新的，profile 里的旧 session 会一直赢、新文件被无视，而输出只说
  「使用 profile 自带的登录态」，完全看不出问题。
- **`sessionid` 是 HttpOnly 的**，所以导出时必须包含 HttpOnly cookie，
  用 `document.cookie` 是拿不到的。DevTools 里 **Request Headers** 的 `Cookie:`
  行包含全部 cookie（HttpOnly 也在内），`Set-Cookie:` 响应头则不行。
- 转换和 `verify` 都会校验 `sessionid` 是否存在，缺了直接报错而不是静默降级。

### 为什么要手动导出，而不是自动拿

`login` / `cookies --from` / `import-profile` 三个命令都还在，但在本机实测都不通：

| 方式 | 结果 |
|---|---|
| 在 douk 的浏览器里登录 | 登录接口拒绝，自动化环境会被识别 |
| 从本机浏览器读 cookie 库 | Chrome 127+ 起改用应用绑定加密，读不出明文 |
| 把浏览器 profile 搬过来 | 加密密钥与原始 profile 路径绑定，换目录即失效 |

结论是**手动导出一次最省事也最稳**，所以文档把它作为唯一推荐路径。
换台机器或换个浏览器版本上述方式可能可用，但不值得依赖。

cookie 大概能用几周到几个月，失效了重导一次即可。

## 产物

```
data/
├─ douk.db                    # SQLite：target 表 + aweme 表
├─ cookies.txt                # 导出给 yt-dlp
├─ browser/                   # Playwright 运行缓存（可删）
└─ videos/{剧名}/
     ├─ 第1集.mp4  第1集.srt  第1集.jpg  第1集.info.json
     ├─ cover.jpg             # 剧集封面
     └─ drama.json            # 剧名 / 简介 / 题材 / 集数 / 时长
```

文件名由 `config.toml` 的 `filename_template` 决定，默认 `第{seq}集`。
短剧的集号取自接口的 `EpisodeNumber`；其它形态没有集号，按 `create_time`
正序编号（1 = 最早），因为接口返回的是新→旧，不重排顺序是反的。

## 编码与播放

TikTok 对同一个视频提供两套编码，**是个真实取舍**：

| 编码 | 最高分辨率 | 播放兼容性 |
|---|---|---|
| H.264 | 576x1024 | 到处都能播 |
| H.265 (bytevc1) | **1080x1920** | Windows 自带播放器需付费 HEVC 扩展 |

`config.toml` 的 `video_codec` 三选一，命令行 `--codec` 可临时覆盖：

- `h264`（**默认**）—— `-S vcodec:h264`，实测选中 576x1024，不转码，到处能播。
- `h265` —— `-S res,vcodec:h265`，实测选中 1080x1920。**最省事的办法是换播放器**：
  VLC / PotPlayer / MPV 都原生支持 HEVC，不用转码也不损画质。
- `transcode` —— 下 1080p H.265 再用 ffmpeg 转 H.264，两头兼顾，费 CPU 和磁盘。

`best` / `hevc` / `264` 这类写法会自动归一，非法值直接报错而不是静默降级。

已经下好的文件不用重下，直接转：

```powershell
.\.venv\Scripts\python.exe douk.py convert -t <targetId>
```

`convert` 是幂等的，只挑 H.265 的转，中断了再跑一次会接着做。

**转码必然让文件变大** —— H.264 编码效率不如 H.265，这是编码格式本身决定的。
实测 30 集 1080p（源 663kbps）：

| CRF | 相对源文件 | 耗时/集 |
|---|---|---|
| 23 | 3.1x | 26s |
| **26（默认）** | **2.3x** | **23s** |
| 28 | 1.9x | 21s |

`preset slow` 只比 `medium` 小 5% 却慢 30%，不划算，所以固定用 `medium`。
实测整部剧 131 MB → 323 MB。

## 排错

**先跑 `douk doctor`。** 它会查出口 IP 归属、是否被标记为代理、Cloudflare WARP 是否开着、
TikTok 可达性、以及本地登录态。

| 现象 | 原因 / 处理 |
|---|---|
| 登录页报 `get region err` / `account-api error: [7]` | 先分清：**如果你平时的浏览器能正常登录**，那就是自动化环境被识别了，别在 douk 里登，按 [使用说明.md](使用说明.md) 手动导出 cookie。**如果平时的浏览器也登不上**，才是 IP 问题（Cloudflare WARP、云主机段都会中招），需换住宅 IP |
| `Could not copy Chrome cookie database` | Chrome 没退干净 —— 托盘图标、后台进程全关掉再试 |
| `Maximum number of attempts reached` | 已被限频，换 IP 后还要等 30-60 分钟，并删掉 `data/browser/` 重来 |
| `Unexpected response from webpage request` | TikTok 改了响应格式，当前 yt-dlp 解析不了 —— `pip install -U yt-dlp` 升级即可。**和 cookie 无关**，别去重导。实测 2026.7.4 全挂、2026.8.19 正常 |
| `No video formats found` | 没登录或 cookie 过期 → 重新导出 `cookies.txt`，跑 `douk verify` |
| 采集报「一条视频都没采到」 | 同上；或被风控 → `douk collect <url> --headful` 手动过验证码 |
| 命中验证码 | 换代理 IP；把 `headless` 设为 `false` |
| 下载慢/被限速 | 降低 `concurrency`，调大 `delay_min/delay_max` |
| 视频顺序不对 | 短剧的集号取自接口 `EpisodeNumber`，不会错；其它形态跑一次 `douk collect` 会自动 renumber |
| 视频打不开 / 提示缺编解码器 | 下的是 H.265。装 VLC/PotPlayer/MPV，或跑 `douk convert` 转 H.264 |

## 许可与免责

源代码以 MIT 许可发布，见 [LICENSE](LICENSE)。

**MIT 只覆盖本仓库的代码，不对任何下载内容授予任何权利。** 使用前请读
[DISCLAIMER.md](DISCLAIMER.md)：用途限定、版权归属、平台条款责任、凭证安全。

## 合规

仅供个人离线归档与研究。视频版权归原作者，别二次分发或商用。高频抓取会触发平台风控，
`concurrency` 保持个位数、留足随机延迟。
