# ============================================================================
# Hàm dùng chung cho 3 kịch bản test trong thư mục này.
# KHÔNG chạy file này trực tiếp: các file 1-, 2-, 3- tự nạp nó.
# ============================================================================

$ErrorActionPreference = 'Stop'
# Tắt thanh tiến trình "Reading web response..." của Invoke-WebRequest
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$LoadtestDir = Join-Path $ProjectRoot 'loadtest'
$PromUrl     = 'http://127.0.0.1:9090'
$AmUrl       = 'http://127.0.0.1:9093'
# Ghim phiên bản image giống README mục 7.12
$K6Image     = 'grafana/k6:0.54.0'
$StressImage = 'alexeiled/stress-ng:0.12.05'
$script:StartedAt = Get-Date

# ---------- In có mốc thời gian ----------
function Reset-Clock { $script:StartedAt = Get-Date }
function Get-Elapsed { [int]((Get-Date) - $script:StartedAt).TotalSeconds }
function Write-Step {
  param([string]$Message, [ConsoleColor]$Color = 'Gray')
  Write-Host ('[{0,4}s] {1}' -f (Get-Elapsed), $Message) -ForegroundColor $Color
}

# ---------- Đọc 1 biến không bí mật trong .env (ví dụ HTTP_PORT) ----------
function Get-EnvValue {
  param([string]$Name, [string]$Default)
  $file = Join-Path $ProjectRoot '.env'
  if (Test-Path $file) {
    $line = Get-Content $file | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if ($line) {
      $v = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
      if ($v) { return $v }
    }
  }
  return $Default
}

# ---------- Docker ----------
function Invoke-Compose {
  & docker compose -f $ComposeFile @args
  if ($LASTEXITCODE -ne 0) { throw "Lệnh 'docker compose $args' bị lỗi (mã $LASTEXITCODE)" }
}

# Xoá container nếu còn (không báo lỗi khi không có)
function Remove-ContainerQuiet {
  param([string[]]$Names)
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { & docker rm -f @Names 2>&1 | Out-Null } finally { $ErrorActionPreference = $old }
}

function Test-ContainerRunning {
  param([string]$Name)
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $s = & docker inspect -f '{{.State.Running}}' $Name 2>&1 } finally { $ErrorActionPreference = $old }
  return ("$s".Trim() -eq 'true')
}

# Tên mạng Docker của hệ thống (thường là tool-monitoring_appnet), để k6 gọi thẳng nginx:80
function Get-AppNetwork {
  $nets = & docker inspect ecommerce-nginx --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
  $name = (("$nets").Trim() -split '\s+')[0]
  if (-not $name) { throw 'Không tìm được mạng Docker của container ecommerce-nginx' }
  return $name
}

# Kiểm tra hệ thống đã bật đủ trước khi test
function Assert-StackUp {
  param([string[]]$Services)
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Không thấy lệnh docker. Cài và bật Docker Desktop trước.' }
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $running = @(& docker compose -f $ComposeFile ps --status running --services 2>$null) } finally { $ErrorActionPreference = $old }
  if ($LASTEXITCODE -ne 0) { throw 'Docker chưa chạy (bật Docker Desktop), hoặc hệ thống chưa bật: docker compose up -d' }
  $missing = @($Services | Where-Object { $running -notcontains $_ })
  if ($missing.Count -gt 0) { throw ('Các service chưa chạy: {0}. Bật bằng: docker compose up -d' -f ($missing -join ', ')) }
  try { Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 -Uri "$PromUrl/-/ready" | Out-Null }
  catch { throw "Không gọi được Prometheus ở $PromUrl" }
}

# ---------- Prometheus ----------
function Invoke-PromQuery {
  param([string]$Query)
  try {
    $r = Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 -Uri ("$PromUrl/api/v1/query?query=" + [uri]::EscapeDataString($Query))
    return $r.data.result
  } catch { return $null }
}

function Get-PromNumber {
  param([string]$Query)
  $res = @(Invoke-PromQuery $Query)
  if ($res.Count -eq 0 -or $null -eq $res[0]) { return $null }
  return [double]$res[0].value[1]
}

function Format-Num {
  param($Value, [string]$Format = '{0:N0}')
  if ($null -eq $Value -or [double]::IsNaN($Value)) { return '?' }
  return ($Format -f $Value)
}

# Trạng thái 1 luật cảnh báo: FIRING / PENDING / bình thường
function Get-AlertState {
  param([string]$AlertName)
  $states = @(Invoke-PromQuery "ALERTS{alertname=`"$AlertName`"}" | ForEach-Object { $_.metric.alertstate })
  if ($states -contains 'firing') { return 'FIRING' }
  if ($states -contains 'pending') { return 'PENDING' }
  return 'bình thường'
}

# Alertmanager đã nhận cảnh báo này chưa (kể cả đang bị silence)
function Test-AlertmanagerHas {
  param([string]$AlertName)
  try {
    $f = [uri]::EscapeDataString("alertname=`"$AlertName`"")
    $a = @(Invoke-RestMethod -UseBasicParsing -TimeoutSec 5 -Uri "$AmUrl/api/v2/alerts?active=true&silenced=true&inhibited=true&filter=$f")
    return (@($a | Where-Object { $_ }).Count -gt 0)
  } catch { return $false }
}

# ---------- Silence (tạm tắt Telegram/mail khi dùng -Silent) ----------
function New-TestSilence {
  param([string]$AlertRegex, [int]$Minutes = 30)
  $fmt = "yyyy-MM-dd'T'HH:mm:ss'Z'"; $inv = [Globalization.CultureInfo]::InvariantCulture
  $now = [DateTime]::UtcNow
  $body = @{
    matchers  = @(@{ name = 'alertname'; value = $AlertRegex; isRegex = $true; isEqual = $true })
    startsAt  = $now.ToString($fmt, $inv)
    endsAt    = $now.AddMinutes($Minutes).ToString($fmt, $inv)
    createdBy = 'loadtest-scenario'
    comment   = 'Tạm tắt thông báo trong lúc chạy kịch bản test (-Silent)'
  } | ConvertTo-Json -Depth 5
  $r = Invoke-RestMethod -UseBasicParsing -Method Post -Uri "$AmUrl/api/v2/silences" `
         -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
  Write-Step "Đã tạo silence (alertname=~`"$AlertRegex`", $Minutes phút): Telegram/mail sẽ KHÔNG gửi" 'DarkYellow'
  return $r.silenceID
}

# Gỡ silence khi các luật đã về bình thường. Còn luật đang bật thì để silence
# tự hết hạn, tránh gỡ xong Telegram lại réo vì sự cố test chưa kịp tan.
function Close-TestSilence {
  param([string]$Id, [string]$AlertRegex)
  if (-not $Id) { return }
  $active = @(Invoke-PromQuery "ALERTS{alertname=~`"$AlertRegex`"}" | Where-Object { $_ })
  if ($active.Count -gt 0) {
    Write-Step "Còn luật đang bật nên GIỮ silence tới khi tự hết hạn (xem/gỡ tay ở $AmUrl/#/silences)" 'DarkYellow'
    return
  }
  try {
    Invoke-RestMethod -UseBasicParsing -Method Delete -Uri "$AmUrl/api/v2/silence/$Id" | Out-Null
    Write-Step 'Đã gỡ silence' 'DarkYellow'
  } catch { Write-Step "Không gỡ được silence, nó sẽ tự hết hạn (xem ở $AmUrl/#/silences)" 'Yellow' }
}

# ---------- Tóm tắt mức cao nhất trong lúc test (đọc lại từ Prometheus) ----------
function Show-PeakSummary {
  param([datetime]$Since)
  $w = [Math]::Max(60, [int]((Get-Date) - $Since).TotalSeconds)
  $be = 'job="backend",route!~"/health|/metrics"'
  $rows = [ordered]@{
    'CPU máy chủ cao nhất (%)'          = "max_over_time((100 * (1 - avg(rate(node_cpu_seconds_total{mode=`"idle`"}[1m]))))[${w}s:15s])"
    'RAM máy chủ cao nhất (%)'          = "max_over_time((100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))[${w}s:15s])"
    'CPU backend cao nhất (% 1 nhân)'   = "max_over_time((100 * rate(process_cpu_seconds_total{job=`"backend`"}[1m]))[${w}s:15s])"
    'Request/giây vào nginx cao nhất'   = "max_over_time(rate(nginx_http_requests_total[1m])[${w}s:15s])"
    'p95 backend tự đo cao nhất (giây)' = "max_over_time(histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{$be}[1m])))[${w}s:15s])"
    'Tỉ lệ lỗi 5xx cao nhất (%)'        = "max_over_time((100 * (sum(rate(http_requests_total{$be,status_code=~`"5..`"}[1m])) or vector(0)) / sum(rate(http_requests_total{$be}[1m])))[${w}s:15s])"
    'Kết nối MySQL cao nhất'            = "max_over_time(mysql_global_status_threads_connected[${w}s])"
  }
  Write-Host ''
  Write-Host ("=== Mức cao nhất trong {0:N1} phút vừa rồi (Prometheus) ===" -f ($w / 60)) -ForegroundColor Cyan
  foreach ($k in $rows.Keys) { Write-Host ('  {0,-36} {1}' -f $k, (Format-Num (Get-PromNumber $rows[$k]) '{0:N2}')) }
  $fired = @(Invoke-PromQuery "count by (alertname) (max_over_time(ALERTS{alertstate=`"firing`"}[${w}s]))" |
             Where-Object { $_ } | ForEach-Object { $_.metric.alertname })
  $txt = if ($fired.Count) { $fired -join ', ' } else { '(không có)' }
  Write-Host ('  {0,-36} {1}' -f 'Luật đã FIRING', $txt)
  Write-Host '  Xem lại biểu đồ: Grafana Shop Overview, chọn khung giờ vừa chạy.' -ForegroundColor DarkGray
}
