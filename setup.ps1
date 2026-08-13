<#
    在新电脑上一键落地 douk。

    用法:
      .\setup.ps1              # 建 venv、装依赖、检查环境
      .\setup.ps1 -Chromium    # 顺便下载 Playwright 自带浏览器（没装 Chrome 时用）

    做完这一步还需要导出一次 cookie，见 使用说明.md。
#>
param([switch]$Chromium)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Ok($msg)   { Say "  [OK]   $msg" "Green" }
function Warn($msg) { Say "  [WARN] $msg" "Yellow" }
function Bad($msg)  { Say "  [BAD]  $msg" "Red" }

Say "=== 1/4 找 Python ===" "Cyan"

function Test-Python($exePath, $exeArgs) {
    try {
        $v = & $exePath @exeArgs --version 2>&1
        if ("$v" -match "Python 3\.(\d+)") {
            return @{ Ver = "$v"; Minor = [int]$Matches[1] }
        }
    } catch { }
    return $null
}

$py = $null; $pyArgs = @()
$tooOld = @()

# 1) PATH 上的常规入口
foreach ($cand in @(@("py", @("-3")), @("python", @()), @("python3", @()))) {
    if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
    $r = Test-Python $cand[0] $cand[1]
    if (-not $r) { continue }
    if ($r.Minor -lt 11) { $tooOld += "$($cand[0]) = $($r.Ver)"; continue }
    $py = $cand[0]; $pyArgs = $cand[1]; Ok "$($cand[0]) -> $($r.Ver)"; break
}

# 2) 装了但没进 PATH —— 这种情况比想象的常见，挨个常见位置翻一遍
if (-not $py) {
    $roots = @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:LOCALAPPDATA\Python",
        "$env:ProgramFiles\Python*",
        "C:\Python3*"
    )
    $found = foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem $r -Recurse -Depth 2 -Filter "python.exe" -ErrorAction SilentlyContinue
        }
    }
    foreach ($f in ($found | Sort-Object FullName -Descending)) {
        $r = Test-Python $f.FullName @()
        if ($r -and $r.Minor -ge 11) {
            $py = $f.FullName; $pyArgs = @()
            Ok "$($r.Ver)  ($($f.FullName))"
            Warn "这个 Python 不在 PATH 上，本脚本直接用绝对路径调用它"
            break
        }
    }
}

# 3) 还没有就装 —— winget 在 Win10 1809+ / Win11 自带
if (-not $py) {
    if ($tooOld) { Warn "找到的版本太旧: $($tooOld -join ', ')（tomllib 需要 3.11+）" }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Say "  没有可用的 Python，正在用 winget 安装 3.12..." "Yellow"
        winget install --id Python.Python.3.12 -e --source winget `
              --accept-package-agreements --accept-source-agreements
        # 刷新本进程的 PATH，省得让用户重开终端
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        foreach ($cand in @(@("py", @("-3")), @("python", @()))) {
            if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
            $r = Test-Python $cand[0] $cand[1]
            if ($r -and $r.Minor -ge 11) {
                $py = $cand[0]; $pyArgs = $cand[1]; Ok "装好了: $($r.Ver)"; break
            }
        }
        if (-not $py) {
            Bad "装完仍找不到 Python。请关掉本窗口、重开一个终端再跑一次本脚本。"
            exit 1
        }
    } else {
        Bad "没找到 Python 3.11+，本机也没有 winget。"
        Say "         去 python.org 下载 3.12+，安装时务必勾选 Add python.exe to PATH，" "Gray"
        Say "         装完重开终端再跑本脚本。" "Gray"
        exit 1
    }
}

Say ""
Say "=== 2/4 建虚拟环境并装依赖 ===" "Cyan"
if (Test-Path ".venv") {
    Warn ".venv 已存在，先删掉重建（venv 里是绝对路径，换机器必须重建）"
    Remove-Item ".venv" -Recurse -Force
}
Say "  正在创建虚拟环境（首次会慢十几秒）..." "Gray"
& $py @pyArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { Bad "创建 venv 失败"; exit 1 }
Ok "虚拟环境已建好"

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
Say "  正在升级 pip..." "Gray"
& $venvPy -m pip install --upgrade pip --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Warn "pip 升级失败，继续往下走" }

# 这里要下 50-100 MB。千万别加 -q：静默几分钟看着就像卡死了，
# 让 pip 自己把进度打出来，用户才知道它还活着。
Say "  正在装依赖（约 50-100 MB，网络慢的话要几分钟，请耐心）..." "Gray"
& $venvPy -m pip install -r requirements.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Bad "装依赖失败，看上面 pip 的报错"; exit 1 }
Ok "依赖装好了"

Say ""
Say "=== 3/4 检查外部程序 ===" "Cyan"

# Chrome —— config.toml 默认 channel = "chrome"，驱动的是本机真 Chrome
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
    Ok "Chrome $((Get-Item $chrome).VersionInfo.ProductVersion)"
} else {
    Warn "没装 Chrome。要么装一个，要么把 config.toml 里 channel 改成 \"\" 并加 -Chromium 重跑本脚本"
    $Chromium = $true
}

if ($Chromium) {
    Say "  正在下载 Playwright 自带浏览器（约 300 MB，会下很久，有进度条）..." "Gray"
    & $venvPy -m playwright install chromium
    if ($LASTEXITCODE -eq 0) { Ok "Playwright chromium 就绪" } else { Warn "下载失败，可稍后手动重试" }
}

# ffmpeg —— 转码和封面转换要用，没有也能下载，只是功能受限
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Ok "ffmpeg 已在 PATH"
} else {
    Warn "没有 ffmpeg：--codec transcode 和 douk convert 用不了，封面会存成 .image"
    Say "         装法: winget install Gyan.FFmpeg  然后重开终端" "Gray"
}

Say ""
Say "=== 4/4 自检 ===" "Cyan"
& $venvPy douk.py doctor
$doctorCode = $LASTEXITCODE

Say ""
Say "=== 完成 ===" "Cyan"
Say "运行方式:" "Gray"
Say "  .\.venv\Scripts\python.exe douk.py --help"
Say ""
if (-not (Test-Path "data\cookies.txt")) {
    Say "下一步：导出 TikTok 登录态（没有它下不了任何东西）" "Yellow"
    Say "  见 使用说明.md 第一节，做完跑 douk.py verify 验证" "Yellow"
} elseif ($doctorCode -ne 0) {
    Say "doctor 有报警，按上面的提示处理。" "Yellow"
} else {
    Say "环境就绪，可以直接用了。" "Green"
}
Say ""
Say "提醒：config.toml 里的 out_dir 是上一台机器的路径，记得改成本机的。" "Yellow"
