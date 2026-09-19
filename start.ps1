param([string]$Python = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) {
    $localPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    $workspacePython = Join-Path $PSScriptRoot "..\..\work\flower-competitor-env\Scripts\python.exe"
    if (Test-Path -LiteralPath $localPython) { $Python = $localPython }
    elseif (Test-Path -LiteralPath $workspacePython) { $Python = $workspacePython }
    else { throw "Python environment not found. Follow README.md to install requirements." }
}
& $Python run.py
exit $LASTEXITCODE
