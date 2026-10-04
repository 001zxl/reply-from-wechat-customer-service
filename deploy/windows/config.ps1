# 图形化配置：让不懂技术的人也能填 API Key，不用碰记事本
# 由 2-config.bat 调用

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ---------- 读已有配置 ----------
$envPath = Join-Path $root ".env"
$existing = @{}
if (Test-Path $envPath) {
    Get-Content $envPath -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$') {
            $existing[$matches[1]] = $matches[2].Trim()
        }
    }
}
function Get-Old($k) { if ($existing.ContainsKey($k)) { $existing[$k] } else { "" } }

# ---------- 窗口 ----------
$form = New-Object System.Windows.Forms.Form
$form.Text = "微信客服助手 - 配置"
$form.Size = New-Object System.Drawing.Size(560, 520)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)

function Add-Label($text, $y, $bold = $false) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text
    $l.Location = New-Object System.Drawing.Point(24, $y)
    $l.Size = New-Object System.Drawing.Size(500, 22)
    if ($bold) { $l.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9, [System.Drawing.FontStyle]::Bold) }
    $form.Controls.Add($l)
    return $l
}

$y = 18
Add-Label "第一步：填大模型的 API Key（必填）" $y $true | Out-Null
$y += 26
Add-Label "去 platform.deepseek.com 注册，在「API Keys」里创建一个，粘到下面：" $y | Out-Null
$y += 26
$tbKey = New-Object System.Windows.Forms.TextBox
$tbKey.Location = New-Object System.Drawing.Point(24, $y)
$tbKey.Size = New-Object System.Drawing.Size(500, 26)
$tbKey.Text = Get-Old "DEEPSEEK_API_KEY"
$tbKey.UseSystemPasswordChar = $true
$form.Controls.Add($tbKey)
$y += 30
$cbShow = New-Object System.Windows.Forms.CheckBox
$cbShow.Text = "显示密钥"
$cbShow.Location = New-Object System.Drawing.Point(24, $y)
$cbShow.Size = New-Object System.Drawing.Size(100, 22)
$cbShow.Add_CheckedChanged({ $tbKey.UseSystemPasswordChar = -not $cbShow.Checked })
$form.Controls.Add($cbShow)
$y += 34

Add-Label "第二步：物流查询（可选，暂时没有就留空）" $y $true | Out-Null
$y += 26
Add-Label "快递100 客户编号：" $y | Out-Null
$tbCust = New-Object System.Windows.Forms.TextBox
$tbCust.Location = New-Object System.Drawing.Point(180, $y - 3)
$tbCust.Size = New-Object System.Drawing.Size(344, 26)
$tbCust.Text = Get-Old "KD100_CUSTOMER"
$form.Controls.Add($tbCust)
$y += 32
Add-Label "快递100 授权 Key：" $y | Out-Null
$tbKd = New-Object System.Windows.Forms.TextBox
$tbKd.Location = New-Object System.Drawing.Point(180, $y - 3)
$tbKd.Size = New-Object System.Drawing.Size(344, 26)
$tbKd.Text = Get-Old "KD100_KEY"
$form.Controls.Add($tbKd)
$y += 34

Add-Label "第三步：运行模式" $y $true | Out-Null
$y += 26
$rbReview = New-Object System.Windows.Forms.RadioButton
$rbReview.Text = "审核模式 —— AI 出草稿，你在网页上点发送才发出去（推荐，先跑这个）"
$rbReview.Location = New-Object System.Drawing.Point(24, $y)
$rbReview.Size = New-Object System.Drawing.Size(500, 22)
$rbReview.Checked = $true
$form.Controls.Add($rbReview)
$y += 24
$rbAuto = New-Object System.Windows.Forms.RadioButton
$rbAuto.Text = "全自动 —— AI 直接发送（确认效果好之后再选）"
$rbAuto.Location = New-Object System.Drawing.Point(24, $y)
$rbAuto.Size = New-Object System.Drawing.Size(500, 22)
$form.Controls.Add($rbAuto)
$y += 36

# ---------- 状态文字 ----------
$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Location = New-Object System.Drawing.Point(24, $y)
$lblStatus.Size = New-Object System.Drawing.Size(500, 44)
$lblStatus.ForeColor = [System.Drawing.Color]::DimGray
$lblStatus.Text = "填好后点「测试连接」确认能通，再点「保存」。"
$form.Controls.Add($lblStatus)
$y += 50

# ---------- 按钮 ----------
$btnTest = New-Object System.Windows.Forms.Button
$btnTest.Text = "测试连接"
$btnTest.Location = New-Object System.Drawing.Point(24, $y)
$btnTest.Size = New-Object System.Drawing.Size(110, 32)
$form.Controls.Add($btnTest)

$btnSave = New-Object System.Windows.Forms.Button
$btnSave.Text = "保存"
$btnSave.Location = New-Object System.Drawing.Point(300, $y)
$btnSave.Size = New-Object System.Drawing.Size(110, 32)
$btnSave.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.Controls.Add($btnSave)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = "取消"
$btnCancel.Location = New-Object System.Drawing.Point(416, $y)
$btnCancel.Size = New-Object System.Drawing.Size(108, 32)
$btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($btnCancel)

$form.AcceptButton = $btnTest
$form.CancelButton = $btnCancel

# ---------- 测试连接 ----------
$btnTest.Add_Click({
    $key = $tbKey.Text.Trim()
    if (-not $key) { $lblStatus.Text = "❌ 还没填 API Key"; return }
    $lblStatus.Text = "正在连接 DeepSeek，稍等…"
    $form.Refresh()
    try {
        $body = @{
            model = "deepseek-flash"
            messages = @(@{ role = "user"; content = "回复两个字：正常" })
            max_tokens = 20
        } | ConvertTo-Json -Depth 5 -Compress
        $r = Invoke-RestMethod -Uri "https://api.deepseek.com/v1/chat/completions" `
            -Method Post -TimeoutSec 30 `
            -Headers @{ Authorization = "Bearer $key"; "Content-Type" = "application/json" } `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
        $reply = $r.choices[0].message.content
        $lblStatus.ForeColor = [System.Drawing.Color]::Green
        $lblStatus.Text = "✅ 连接成功！模型回复：$reply`nKey 没问题，可以点「保存」了。"
    } catch {
        $lblStatus.ForeColor = [System.Drawing.Color]::Firebrick
        $msg = $_.Exception.Message
        if ($msg -match "401") { $msg = "Key 不对（401 未授权）。检查是不是复制少了字符。" }
        elseif ($msg -match "402") { $msg = "余额不足（402）。去 DeepSeek 后台充值。" }
        elseif ($msg -match "timeout|超时") { $msg = "连接超时。检查网络，或稍后再试。" }
        $lblStatus.Text = "❌ 失败：$msg"
    }
})

# ---------- 保存 ----------
$result = $form.ShowDialog()

if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Host "  已取消，配置没改动。"
    exit 1
}

$key = $tbKey.Text.Trim()
if (-not $key) {
    Write-Host "  [失败] API Key 是空的"
    exit 1
}

# ---------- 写 .env ----------
$lines = @(
    "# 由配置向导生成 $(Get-Date -Format 'yyyy-MM-dd HH:mm')",
    "",
    "DEEPSEEK_API_KEY=$key",
    "LLM_BACKEND=deepseek",
    "",
    "LOGISTICS_PROVIDER=$(if ($tbCust.Text.Trim() -and $tbKd.Text.Trim()) { 'kuaidi100' } else { 'mock' })",
    "KD100_CUSTOMER=$($tbCust.Text.Trim())",
    "KD100_KEY=$($tbKd.Text.Trim())",
    "KD100_COM=shentong",
    "",
    "WECHAT_CHANNEL=windows_wechat",
    "WECHAT_WIN_WATCH=",
    "WECHAT_CHANNEL_TYPE=personal",
    "",
    "# ---- 安全设置（默认最保守，别急着改）----",
    "DRY_RUN=$(if ($rbAuto.Checked) { '0' } else { '1' })",
    "WECHAT_GUARD=whitelist",
    "WECHAT_SEND_ALLOWLIST=",
    "",
    "# ---- 风控 ----",
    "QUIET_HOURS=22:00-08:00",
    "AUTO_REPLY_MAX_PER_DAY=150",
    "AUTO_REPLY_MAX_PER_MINUTE=6",
    "MIN_REPLY_INTERVAL_SECONDS=3",
    "REPLY_DELAY_MIN=1.5",
    "REPLY_DELAY_MAX=5.0",
    "SIMILAR_REPLY_WINDOW=5",
    "SIMILAR_REPLY_THRESHOLD=0.88",
    "CIRCUIT_BREAKER_FAILURES=3",
    "CIRCUIT_BREAKER_COOLDOWN_MINUTES=30",
    "TYPING_SIMULATION=1",
    "",
    "PORT=8787",
    "DB_PATH=data/assistant.db"
)

if (Test-Path $envPath) {
    Copy-Item $envPath "$envPath.bak" -Force
    Write-Host "  已备份原配置到 .env.bak"
}

# 用 UTF-8 无 BOM 写，python-dotenv 才读得对
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($envPath, $lines, $utf8)

Write-Host ""
Write-Host "  已写入 .env"
Write-Host "    模型密钥     : $(if($key){'已设置'}else{'空'})"
Write-Host "    物流查询     : $(if ($tbCust.Text.Trim() -and $tbKd.Text.Trim()) { '快递100' } else { '模拟数据（演示用）' })"
Write-Host "    运行模式     : $(if ($rbAuto.Checked) { '全自动' } else { '审核模式' })"
Write-Host "    微信通道     : windows_wechat"
Write-Host "    演练模式     : $(if ($rbAuto.Checked) { '关闭（会真的发）' } else { '开启（不会发）' })"
