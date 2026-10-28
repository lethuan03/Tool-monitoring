# Build + đẩy toàn bộ image lên Docker Hub (thuanperry/ecommerce-*), dùng bởi
# docker-compose.app.yml + docker-compose.monitoring.yml trên EC2 (README mục 12).
#
# Chạy từ PowerShell, tại thư mục gốc repo (hoặc truyền -RepoRoot):
#   .\scripts\push-images.ps1
#
# Cần đã `docker login` trước (chạy `docker login`, nhập username/password
# hoặc access token Docker Hub của thuanperry).
#
# Mặc định tag "latest". Muốn tag theo version thì: .\scripts\push-images.ps1 -Tag v1.0.0

param(
    [string]$DockerHubUser = "thuanperry",
    [string]$Tag = "latest",
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot\..")
)

# KHÔNG dùng "Stop": docker build/push ghi tiến trình ra stderr, PowerShell
# với ErrorActionPreference=Stop coi đó là lỗi và dừng oan dù lệnh thành công.
# Kiểm tra bằng $LASTEXITCODE (exit code thật của tiến trình) là đủ.
$ErrorActionPreference = "Continue"

# (name trong Docker Hub, build context)
$images = @(
    @{ Name = "ecommerce-backend";      Context = "backend" },
    @{ Name = "ecommerce-frontend";     Context = "frontend" },
    @{ Name = "ecommerce-nginx";        Context = "nginx" },
    @{ Name = "ecommerce-db";           Context = "mysql" },
    @{ Name = "ecommerce-alloy";        Context = "alloy" },
    @{ Name = "ecommerce-prometheus";   Context = "prometheus" },
    @{ Name = "ecommerce-grafana";      Context = "grafana" },
    @{ Name = "ecommerce-loki";         Context = "loki" },
    @{ Name = "ecommerce-alertmanager"; Context = "alertmanager" },
    @{ Name = "ecommerce-holmes";       Context = "holmes" },
    # Phải đứng SAU ecommerce-holmes: Dockerfile của bridge dựng trên image đó
    @{ Name = "ecommerce-holmes-bridge"; Context = "holmes-bridge" }
)

foreach ($img in $images) {
    $fullName = "$DockerHubUser/$($img.Name):$Tag"
    $contextPath = Join-Path $RepoRoot $img.Context

    Write-Host "==> Build $fullName (context: $contextPath)" -ForegroundColor Cyan
    docker build -t $fullName $contextPath
    if ($LASTEXITCODE -ne 0) { throw "Build thất bại: $fullName" }

    Write-Host "==> Push $fullName" -ForegroundColor Cyan
    docker push $fullName
    if ($LASTEXITCODE -ne 0) { throw "Push thất bại: $fullName" }
}

Write-Host "`nXong. Đã push các image:" -ForegroundColor Green
foreach ($img in $images) {
    Write-Host "  - $DockerHubUser/$($img.Name):$Tag"
}
