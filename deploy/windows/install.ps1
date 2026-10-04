# 安装：找/装 Python，建虚拟环境，装依赖。由 1-install.bat 调用。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

function Say($m) { Write-Host $m }
function Ok($m)   { Write-Host "  [OK] $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m"   -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [X]  $m"   -ForegroundColor Red }

Say ""
Say "  工作目录：$root"
Say ""

# ---------------- 1. 找 Python ----------------
Say "[1/3] 检查 Python ..."

$pyExe = $null
$pyArgs = @()

function Test-Python($exe, $args) {
    try {
        $v = & $exe @args -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
            $parts = $v.Trim().Split('.')
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) { return $v.Trim() }
        }
    } catch { }
    return $null
}

# 依次尝试：py 启动器 / python / python3
foreach ($cand in @(
    @{ exe = "py";      args = @("-3.12") },
    @{ exe = "py";      args = @("-3") },
    @{ exe = "python";  args = @() },
    @{ exe = "python3"; args = @() }
)) {
    if (Get-Command $cand.exe -ErrorAction SilentlyContinue) {
        $v = Test-Python $cand.exe $cand.args
        if ($v) {
            $pyExe = $cand.exe; $pyArgs = $cand.args
            Ok "找到 Python $v（$($cand.exe) $($cand.args -join ' ')）"
            break
        }
    }
}

# ---------------- 2. 没有就装 ----------------
if (-not $pyExe) {
    Warn "没找到 Python 3.11+，准备自动安装 3.12"
    Say ""

    $installer = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
    $url = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

    if (-not (Test-Path $installer)) {
        Say "  正在下载 Python（约 25MB）..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing -TimeoutSec 300
            Ok "下载完成"
        } catch {
            Fail "下载失败：$($_.Exception.Message)"
            Say ""
            Say "  请手动安装："
            Say "    1. 打开 https://www.python.org/downloads/"
            Say "    2. 下载 Python 3.12 的 Windows installer (64-bit)"
            Say "    3. 安装时**务必勾选** Add python.exe to PATH"
            Say "    4. 装完关掉这个窗口，重新运行 1-install.bat"
            Say ""
            exit 1
        }
    }

    Say "  正在静默安装（只装给当前用户，不需要管理员密码）..."
    $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1",
        "Include_test=0", "Include_launcher=1", "Include_pip=1"
    )
    if ($p.ExitCode -ne 0) {
        Fail "安装程序返回错误码 $($p.ExitCode)"
        exit 1
    }
    Ok "安装完成"
    Say "  等待系统刷新 PATH ..."
    Start-Sleep -Seconds 8

    # 重新找
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    foreach ($cand in @(@{ exe = "py"; args = @("-3.12") }, @{ exe = "python"; args = @() })) {
        if (Get-Command $cand.exe -ErrorAction SilentlyContinue) {
            $v = Test-Python $cand.exe $cand.args
            if ($v) { $pyExe = $cand.exe; $pyArgs = $cand.args; Ok "Python $v 就绪"; break }
        }
    }
    if (-not $pyExe) {
        Fail "Python 装好了但当前窗口还找不到它。"
        Say "  请**关掉这个窗口**，重新双击运行 1-install.bat"
        exit 1
    }
}

# ---------------- 3. 虚拟环境 ----------------
Say ""
Say "[2/3] 创建独立运行环境 ..."
$vpy = Join-Path $root ".venv\Scripts\python.exe"

if (Test-Path $vpy) {
    Ok "已存在，跳过（要重装就先删掉 .venv 文件夹）"
} else {
    & $pyExe @pyArgs -m venv .venv
    if (-not (Test-Path $vpy)) {
        Fail "创建虚拟环境失败"
        exit 1
    }
    Ok "已创建 .venv"
}

# ---------------- 4. 装依赖 ----------------
Say ""
Say "[3/3] 安装依赖包（几分钟，看网速）..."
Say ""

$mirrors = @(
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.org/simple"
)

& $vpy -m pip install --upgrade pip --quiet --disable-pip-version-check 2>&1 | Out-Null

$installed = $false
foreach ($m in $mirrors) {
    Say "  试源：$m"
    & $vpy -m pip install -r requirements.txt --index-url $m --disable-pip-version-check
    if ($LASTEXITCODE -eq 0) { $installed = $true; break }
    Warn "这个源不行，换下一个"
    Say ""
}
if (-not $installed) {
    Fail "依赖装不上。把上面的报错截图发给对方。"
    exit 1
}

# ---------------- 5. 自检 ----------------
Say ""
Say "  验证关键依赖 ..."
$mods = @("fastapi", "uvicorn", "openai", "pyautogui", "win32gui", "win32clipboard", "PIL", "rapidocr_onnxruntime")
$missing = @()
foreach ($m in $mods) {
    & $vpy -c "import $m" 2>$null
    if ($LASTEXITCODE -eq 0) { Ok $m } else { $missing += $m; Warn "$m 没装上" }
}

Say ""
Say "============================================================"
if ($missing.Count -eq 0) {
    Say "  安装完成！" -ForegroundColor Green
    Say "============================================================"
    Say ""
    Say "  接下来双击运行：2-config.bat"
} else {
    Say "  装完了，但有 $($missing.Count) 个包缺失" -ForegroundColor Yellow
    Say "============================================================"
    Say ""
    Say "  缺的包：$($missing -join ', ')"
    Say "  再运行一次 1-install.bat 通常会补上"
}
Say ""
