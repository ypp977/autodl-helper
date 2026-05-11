$ErrorActionPreference = 'Stop'

$IsWindowsPlatform = if (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) {
    $IsWindows
} else {
    $env:OS -eq 'Windows_NT'
}

if (-not $IsWindowsPlatform) {
    throw 'This script must be run on Windows PowerShell.'
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$DefaultPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$PythonBin = if ($env:PYTHON) { $env:PYTHON } else { $DefaultPython }
$DistDir = Join-Path $ProjectRoot 'dist\nuitka-windows'
$OutputName = 'autodl-helper.exe'

if (-not (Test-Path $PythonBin)) {
    throw "Python not found: $PythonBin. Create a venv first, or run with `$env:PYTHON='C:\path\to\python.exe'."
}

Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

& $PythonBin -m nuitka `
    --standalone `
    --onefile `
    --assume-yes-for-downloads `
    --output-filename=$OutputName `
    --output-dir=$DistDir `
    --include-package=autodl_helper `
    --include-data-files="config.example.yaml=config.example.yaml" `
    --include-data-files=".env.template=.env.template" `
    --remove-output `
    main.py

Write-Host "Built: $(Join-Path $DistDir $OutputName)"
Write-Host ''
Write-Host 'Notes:'
Write-Host '- This builds a console executable only; it does not create an MSI, GUI app, or code signature.'
Write-Host '- Playwright browser binaries are not bundled. Install Chromium in the runtime environment if login/browser flows need it.'
