$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:X_BEARER_TOKEN)) {
    Write-Host "Set your X Developer App bearer token first:" -ForegroundColor Yellow
    Write-Host '$env:X_BEARER_TOKEN = "your-token"'
    exit 2
}

$bundledPython = "C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue

if ($pythonCommand) {
    & $pythonCommand.Source "$PSScriptRoot\tracker.py"
} elseif (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython "$PSScriptRoot\tracker.py"
} else {
    Write-Host "Python 3.10 or newer is required." -ForegroundColor Red
    exit 2
}
