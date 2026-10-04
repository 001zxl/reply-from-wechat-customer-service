# 环境诊断：收集运行所需信息，生成可安全外发的报告。由 4-diagnose.bat 调用。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$report = Join-Path $root "diagnose-report.txt"
$probe  = "C:\Windows\Temp\wechat_cs_probe"
$vpy    = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) { $vpy = "python" }

$out = New-Object System.Collections.Generic.List[string]
function W($s) { $out.Add([string]$s); Write-Host $s }

W "微信客服助手 - 环境诊断报告"
W "生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
W ("=" * 64)

# ---------------- 系统 ----------------
W ""
W "[系统信息]"
W ("-" * 64)
try {
    $os = Get-CimInstance Win32_OperatingSystem
    W "系统      : $($os.Caption) $($os.Version) (Build $($os.BuildNumber))"
    W "架构      : $($os.OSArchitecture)"
    W "内存      : $([math]::Round($os.TotalVisibleMemorySize/1MB,1)) GB"
} catch { W "取系统信息失败：$($_.Exception.Message)" }
try {
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    W "处理器    : $($cpu.Name)"
} catch { }
W "PowerShell: $($PSVersionTable.PSVersion)"

# ---------------- DPI 缩放（点击位置偏移的常见原因）----------------
W ""
W "[显示缩放]"
W ("-" * 64)
try {
    Add-Type -AssemblyName System.Windows.Forms
    foreach ($s in [System.Windows.Forms.Screen]::AllScreens) {
        W "屏幕 $($s.DeviceName): 分辨率 $($s.Bounds.Width)x$($s.Bounds.Height) 主屏=$($s.Primary)"
    }
    # 从注册表读每屏 DPI 缩放
    $k = "HKCU:\Control Panel\Desktop\PerMonitorSettings"
    if (Test-Path $k) {
        Get-ChildItem $k | ForEach-Object {
            $v = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DpiValue
            W "  缩放项 $($_.PSChildName): DpiValue=$v  (0=100%, 1=125%, 2=150%)"
        }
    }
} catch { W "取显示信息失败：$($_.Exception.Message)" }

# ---------------- Python ----------------
W ""
W "[Python]"
W ("-" * 64)
& $vpy --version 2>&1 | ForEach-Object { W $_ }
& $vpy -c "import sys;print('路径:',sys.executable)" 2>&1 | ForEach-Object { W $_ }
& $vpy -c "import sys;print('版本:',sys.version)" 2>&1 | ForEach-Object { W $_ }

# ---------------- 依赖 ----------------
W ""
W "[依赖包]"
W ("-" * 64)
foreach ($m in @("fastapi","uvicorn","openai","pyautogui","win32gui","win32clipboard","PIL","rapidocr_onnxruntime","dotenv")) {
    & $vpy -c "import $m" 2>$null
    if ($LASTEXITCODE -eq 0) { W "  OK    $m" } else { W "  MISS  $m" }
}

# ---------------- 配置（不泄露密钥）----------------
W ""
W "[配置文件]"
W ("-" * 64)
$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
    W ".env 存在"
    Get-Content $envPath -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$') {
            $k = $matches[1]; $v = $matches[2].Trim()
            if ($k -match 'KEY|SECRET|TOKEN|PASSWORD') {
                W "  $k = $(if ($v) { '已设置(长度 ' + $v.Length + ')' } else { '空' })"
            } elseif ($k -match '^(WECHAT_CHANNEL|LOGISTICS_PROVIDER|DRY_RUN|WECHAT_GUARD|QUIET_HOURS|PORT)$') {
                W "  $k = $v"
            }
        }
    }
} else {
    W ".env 不存在（还没跑过 2-config.bat）"
}

# ---------------- 微信窗口 ----------------
W ""
W "[微信窗口探测]"
W ("-" * 64)
& $vpy "bridge\inspect_wechat_windows.py" --window 2>&1 | ForEach-Object { W $_ }

# ---------------- OCR ----------------
W ""
W "[OCR 测试]"
W ("-" * 64)
& $vpy "bridge\inspect_wechat_windows.py" --ocr 2>&1 | Select-Object -First 30 | ForEach-Object { W $_ }

# ---------------- 会话列表 ----------------
W ""
W "[会话列表扫描]"
W ("-" * 64)
& $vpy "bridge\inspect_wechat_windows.py" --list 2>&1 | ForEach-Object { W $_ }

# ---------------- 版面标定 ----------------
W ""
W "[版面标定]"
W ("-" * 64)
& $vpy "bridge\inspect_wechat_windows.py" --layout 2>&1 | ForEach-Object { W $_ }

W ""
W ("=" * 64)
W "报告结束"

# 用 UTF-8 写，跨平台打开都不乱码
[System.IO.File]::WriteAllLines($report, $out, (New-Object System.Text.UTF8Encoding $true))

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  诊断完成" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  报告：$report"
Write-Host "  截图：$probe"
Write-Host ""
Write-Host "  把这两个东西发给对方："
Write-Host "    1. $report"
Write-Host "    2. $probe 文件夹里的所有图片"
Write-Host ""
Write-Host "  报告里**没有** API Key，可以放心发。" -ForegroundColor Yellow
Write-Host ""
