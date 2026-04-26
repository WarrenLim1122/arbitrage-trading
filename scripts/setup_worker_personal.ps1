# Run this in PowerShell as Administrator on worker-personal (139.180.136.233)

Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force

# 1. Firewall — allow ZMQ ports from VPS #1 only
New-NetFirewallRule -DisplayName "ZMQ Layer2" -Direction Inbound -Protocol TCP `
    -LocalPort 5555-5556 -RemoteAddress 152.42.213.98 -Action Allow

# 2. Install Git and Python 3.11
winget install Git.Git --accept-source-agreements --accept-package-agreements
winget install Python.Python.3.11 --accept-source-agreements --accept-package-agreements

# 3. Refresh PATH so git and python are available immediately
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")

# 4. Install uv
pip install uv

# 5. Clone repo and install dependencies
git clone https://github.com/WarrenLim1122/ArbitrageTradingStrategy.git C:\arbitrage
Set-Location C:\arbitrage
uv sync --extra layer3

# 6. Write .env file (fill in your Fusion Markets MT5 credentials below)
@"
WORKER_NAME=personal
MT5_LOGIN=REPLACE_WITH_FUSIONMARKETS_ACCOUNT_NUMBER
MT5_PASSWORD=REPLACE_WITH_FUSIONMARKETS_PASSWORD
MT5_SERVER=REPLACE_WITH_FUSIONMARKETS_SERVER
ZMQ_PULL_ADDR=tcp://0.0.0.0:5555
ZMQ_REP_ADDR=tcp://0.0.0.0:5556
MT5_MAGIC=20250002
"@ | Set-Content -Path C:\arbitrage\.env -Encoding UTF8

Write-Host ""
Write-Host "=== worker-personal setup complete ===" -ForegroundColor Green
Write-Host "Next: install MT5 from Fusion Markets, log in, enable automated trading." -ForegroundColor Yellow
Write-Host "Then edit C:\arbitrage\.env and fill in your MT5 credentials." -ForegroundColor Yellow
Write-Host "Then run: cd C:\arbitrage && uv run python layer3/worker_personal.py" -ForegroundColor Yellow
