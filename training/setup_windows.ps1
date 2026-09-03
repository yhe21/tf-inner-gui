$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Set-Location $ProjectRoot

if (-not (Test-Path $Python)) {
    python -m venv .venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install "ultralytics==8.4.138"
& $Python -m pip install --force-reinstall "torch==2.12.0" "torchvision==0.27.0" --index-url https://download.pytorch.org/whl/cu130
& $Python -m pip install portalocker tqdm
& $Python (Join-Path $PSScriptRoot "check_environment.py")
