$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonPath = $venvPython
} else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "Python est requis. Installez Python 3.11+ et ajoutez-le au PATH."
    }
    $pythonPath = $python.Source
}
Set-Location $root
& $pythonPath -m app.server
