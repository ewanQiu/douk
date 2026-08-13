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
Copy-Item (Join-Path $root "src") (Join-Path $stage "src") -Recurse
foreach ($f in @("douk.py", "douk.cmd", "requirements.txt", "config.toml",
                 "README.md", "使用说明.md",
                 "setup.ps1", "setup.bat", "package.ps1", "package.bat")) {
    $p = Join-Path $root $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $stage $f) }
}

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
Write-Host "  2. 双击 setup.bat（别直接跑 setup.ps1，Windows 默认禁止运行 .ps1）"
if (-not $IncludeCookies) {
    Write-Host "  3. 按 使用说明.md 重新导出一次 cookie"
}
