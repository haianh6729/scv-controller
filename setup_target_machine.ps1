# setup_target_machine.ps1
# Chạy script này trên MỖI máy đích (với quyền Administrator)
# Mục đích: Bật OpenSSH Server để Flask host có thể kết nối

Write-Host "=== SCV Controller - Setup Target Machine ===" -ForegroundColor Cyan
Write-Host ""

# 1. Kiểm tra quyền Admin
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Vui long chay PowerShell voi quyen Administrator!" -ForegroundColor Red
    Exit 1
}

# 2. Cài đặt OpenSSH Server nếu chưa có
Write-Host "[1/4] Kiem tra OpenSSH Server..." -ForegroundColor Yellow
$sshFeature = Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH.Server*'
if ($sshFeature.State -ne 'Installed') {
    Write-Host "     Dang cai dat OpenSSH Server..." -ForegroundColor Yellow
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Write-Host "     Da cai dat OpenSSH Server." -ForegroundColor Green
} else {
    Write-Host "     OpenSSH Server da duoc cai dat." -ForegroundColor Green
}

# 3. Khởi động và đặt startup tự động
Write-Host "[2/4] Khoi dong SSH service..." -ForegroundColor Yellow
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
Write-Host "     SSH service dang chay (auto-start)." -ForegroundColor Green

# 4. Cho phép qua Windows Firewall
Write-Host "[3/4] Cau hinh Firewall (port 22)..." -ForegroundColor Yellow
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
        -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True `
        -Direction Inbound `
        -Protocol TCP `
        -Action Allow `
        -LocalPort 22
}
Write-Host "     Firewall OK." -ForegroundColor Green

# 5. Đặt shell mặc định là PowerShell 7
Write-Host "[4/4] Dat PowerShell 7 lam default shell cho SSH..." -ForegroundColor Yellow
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) {
    $regPath = 'HKLM:\SOFTWARE\OpenSSH'
    if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
    Set-ItemProperty -Path $regPath -Name 'DefaultShell' -Value $pwsh.Source
    Write-Host "     Default shell: $($pwsh.Source)" -ForegroundColor Green
} else {
    Write-Host "     [WARN] PowerShell 7 (pwsh) khong tim thay. Cai dat tu: https://aka.ms/PSWindows" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup hoan tat! ===" -ForegroundColor Cyan
Write-Host "IP cua may nay:" -ForegroundColor White
(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' }).IPAddress
