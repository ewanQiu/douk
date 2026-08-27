<#
    把 douk 打包成可以拷到别的电脑的 zip。

    只打包源码和配置（约 0.2 MB）。.venv 和 data/browser 一律排除：
      .venv         里面写死了本机绝对路径，拷过去用不了，必须在目标机重建
      data/browser  231 MB 的浏览器 profile，是本机状态，没有拷贝价值

    用法:
      .\package.ps1                    # 生成 douk-package.zip（不含登录态）
      .\package.ps1 -IncludeCookies    # 带上 cookies.txt（仅限拷给自己！）
      .\package.ps1 -IncludeDb         # 带上 douk.db（保留下载记录，可续传）
#>
param(
    [string]$Out = "douk-package.zip",
    [switch]$IncludeCookies,
    [switch]$IncludeDb
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$stage = Join-Path $env:TEMP "douk-pkg-$(Get-Random)"

Write-Host "打包 douk..." -ForegroundColor Cyan
New-Item -ItemType Directory -Path $stage -Force | Out-Null

# --- 源码与文档 ---
# 清单以 git 跟踪的文件为准：仓库的 .gitignore 已经定义了「什么该分发」，
# 让打包跟着它走，以后新增文件不会再漏。
# 硬编码清单吃过亏 —— 加了 LICENSE / DISCLAIMER / README.zh-CN / config.example
# 之后清单没跟着更新，别人拿到的包缺了一半文档。
$files = @()
try {
    Push-Location $root
    $files = git -c core.quotepath=false ls-files 2>$null
    Pop-Location
} catch { }

if (-not $files) {
    Write-Host "  git 不可用，回退到静态清单" -ForegroundColor Yellow
    $files = @("douk.py", "douk.cmd", "requirements.txt", "config.example.toml",
               "README.md", "README.zh-CN.md", "使用说明.md",
               "LICENSE", "DISCLAIMER.md",
               "setup.ps1", "setup.bat", "package.ps1", "package.bat")
    $files += (Get-ChildItem (Join-Path $root "src") -Recurse -Filter *.py |
               ForEach-Object { $_.FullName.Substring($root.Length + 1).Replace('\','/') })
}

# config.toml 绝不打包：里面是本机路径（可能含内网地址），
# 而且会覆盖掉接收方自己的配置。给模板就够了。
$files = $files | Where-Object { $_ -and $_ -ne "config.toml" }

foreach ($rel in $files) {
    $src = Join-Path $root $rel
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $stage $rel
    $dir = Split-Path $dst -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item $src $dst
}
Write-Host "  打包 $($files.Count) 个文件" -ForegroundColor Gray

# 写入版本标记：分发出去的副本没有 .git，靠这个文件回答「这台是什么版本」。
# 没它的话，接收方只能靠猜，而「只覆盖某几个文件」这种操作恰恰依赖准确的版本。
try {
    Push-Location $root
    $rev = git log -1 --format="%h %cs %s" 2>$null
    Pop-Location
    if ($rev) {
        Set-Content -Path (Join-Path $stage "VERSION") -Value $rev -Encoding utf8
        Write-Host "  版本标记: $rev" -ForegroundColor Gray
    }
} catch { }

# 清掉 __pycache__，否则会带上本机的 .pyc
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# --- 可选：登录态与数据库 ---
$dataDir = Join-Path $stage "data"
if ($IncludeCookies -or $IncludeDb) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}
if ($IncludeCookies) {
    $c = Join-Path $root "data\cookies.txt"
    if (Test-Path $c) {
        Copy-Item $c (Join-Path $dataDir "cookies.txt")
        Write-Host "  已包含 cookies.txt —— 这等同于你的 TikTok 登录凭证，别外传！" -ForegroundColor Yellow
    } else {
        Write-Host "  没找到 cookies.txt，跳过" -ForegroundColor Yellow
    }
}
if ($IncludeDb) {
    $d = Join-Path $root "data\douk.db"
    if (Test-Path $d) {
        Copy-Item $d (Join-Path $dataDir "douk.db")
        Write-Host "  已包含 douk.db（下载记录会一并带过去）" -ForegroundColor Gray
    }
}

# --- 打包 ---
$outPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $root $Out }
if (Test-Path $outPath) { Remove-Item $outPath -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $outPath -Force
Remove-Item $stage -Recurse -Force

$mb = [math]::Round((Get-Item $outPath).Length / 1MB, 2)
Write-Host ""
Write-Host "完成: $outPath  ($mb MB)" -ForegroundColor Green
Write-Host ""
Write-Host "在目标电脑上：" -ForegroundColor Cyan
Write-Host "  1. 解压到任意目录"
Write-Host "  2. 复制 config.example.toml 为 config.toml，改里面的 out_dir"
Write-Host "  3. 双击 setup.bat（别直接跑 setup.ps1，Windows 默认禁止运行 .ps1）"
Write-Host ""
Write-Host "升级已有安装（对方装过旧版）：" -ForegroundColor Cyan
Write-Host "  解压到新目录，把旧目录的 data\ 整个搬过来，config.toml 也搬过来"
Write-Host "  再跑 setup.bat。数据库会自动补列，登录态和下载记录都保留。"
if (-not $IncludeCookies) {
    Write-Host "  3. 按 使用说明.md 重新导出一次 cookie"
}
