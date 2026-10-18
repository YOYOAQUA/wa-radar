# WA Radar installer for Windows 10/11 (short path: chats exported from the phone).
# Everything goes into one folder (default %USERPROFILE%\WaRadar).
$ErrorActionPreference = "Stop"

$HomeDir = if ($env:WA_RADAR_HOME) { $env:WA_RADAR_HOME } else { Join-Path $env:USERPROFILE "WaRadar" }
$Skill = Split-Path -Parent $PSScriptRoot
$env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path

Write-Host "[1/4] Python manager (uv)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
}

Write-Host "[2/4] Folder: $HomeDir"
foreach ($d in "packs", "reports", "imports", "voice") {
  New-Item -ItemType Directory -Force -Path (Join-Path $HomeDir $d) | Out-Null
}

Write-Host "[3/4] Python"
$Py = Join-Path $HomeDir ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { uv venv --python 3.11 (Join-Path $HomeDir ".venv") }

$Wrapper = "@echo off`r`nchcp 65001 >nul`r`nset PYTHONUTF8=1`r`nset WA_RADAR_HOME=$HomeDir`r`n`"$Py`" `"$Skill\scripts\wr.py`" %*`r`n"
[System.IO.File]::WriteAllText((Join-Path $HomeDir "wr.cmd"), $Wrapper, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[4/4] Check"
& (Join-Path $HomeDir "wr.cmd") check
