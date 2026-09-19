$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$packaged = Join-Path $PSScriptRoot '..\Srez-Windows\Srez.exe'
$localBuild = Join-Path $PSScriptRoot 'dist\Srez.exe'
if (Test-Path -LiteralPath $packaged) {
    & $packaged --data-dir $PSScriptRoot
} elseif (Test-Path -LiteralPath $localBuild) {
    & $localBuild --data-dir $PSScriptRoot
} else {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    $existingPython = Join-Path $PSScriptRoot '..\..\work\flower-competitor-env\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $projectPython) { $pythonForSrez = $projectPython }
    elseif (Test-Path -LiteralPath $existingPython) { $pythonForSrez = $existingPython }
    else { throw 'Install desktop/requirements.txt or build Srez.exe first.' }
    & $pythonForSrez desktop/launcher.py --data-dir $PSScriptRoot
}
