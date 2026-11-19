<#
.SYNOPSIS
    Build (and optionally install) the Chess Player Windows app.

.EXAMPLE
    .\build.ps1                     # build exe + installer for the current version
    .\build.ps1 -Install            # ...and install it (silent upgrade in place)
    .\build.ps1 -Bump patch -Install  # 1.0.0 -> 1.0.1, build, install
    .\build.ps1 -NoInstaller        # only build dist\ChessPlayer\ (no Inno Setup needed)
#>
[CmdletBinding()]
param(
    [ValidateSet("major", "minor", "patch")]
    [string]$Bump,
    [switch]$Install,
    [switch]$NoInstaller
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Invoke-Checked([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

# ── Version (single source of truth: __version__ in chess_player.py) ─────────
$src = Join-Path $Root "chess_player.py"
$text = [IO.File]::ReadAllText($src)
$m = [regex]::Match($text, '(?m)^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"')
if (-not $m.Success) { throw "Could not find __version__ = ""X.Y.Z"" in chess_player.py" }
$maj = [int]$m.Groups[1].Value; $min = [int]$m.Groups[2].Value; $pat = [int]$m.Groups[3].Value

if ($Bump) {
    switch ($Bump) {
        "major" { $maj++; $min = 0; $pat = 0 }
        "minor" { $min++; $pat = 0 }
        "patch" { $pat++ }
    }
    $newLine = "__version__ = ""$maj.$min.$pat"""
    $text = $text.Remove($m.Index, $m.Length).Insert($m.Index, $newLine)
    [IO.File]::WriteAllText($src, $text, (New-Object Text.UTF8Encoding($false)))
}
$Version = "$maj.$min.$pat"
Write-Host "== Chess Player $Version ==" -ForegroundColor Cyan

# ── Build virtualenv (kept separate from your normal Python) ─────────────────
$venv = Join-Path $Root ".venv-build"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating build virtualenv in .venv-build ..."
    $basePy = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    Invoke-Checked "venv creation" { & $basePy -m venv $venv }
}
Write-Host "Installing build dependencies ..."
Invoke-Checked "pip install" { & $py -m pip install --quiet --disable-pip-version-check -r requirements.txt pyinstaller }

# ── Icon ─────────────────────────────────────────────────────────────────────
$ico = Join-Path $Root "packaging\chess_player.ico"
if (-not (Test-Path $ico)) {
    Invoke-Checked "icon generation" { & $py packaging\make_icon.py pieces.png $ico }
}

# ── PyInstaller → dist\ChessPlayer\ ──────────────────────────────────────────
Write-Host "Running PyInstaller ..."
Invoke-Checked "PyInstaller" {
    & $py -m PyInstaller --noconfirm --clean --log-level WARN `
        --distpath dist --workpath build packaging\chess_player.spec
}
Write-Host "Built dist\ChessPlayer\ChessPlayer.exe" -ForegroundColor Green

if ($NoInstaller) { return }

# ── Inno Setup → dist\installer\ChessPlayerSetup-<ver>.exe ───────────────────
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    throw "Inno Setup (ISCC.exe) not found. Install it with:  winget install JRSoftware.InnoSetup`n" +
          "or run with -NoInstaller to only build the exe."
}

Write-Host "Running Inno Setup ..."
Invoke-Checked "Inno Setup" { & $iscc /Q "/DAppVersion=$Version" packaging\installer.iss }
$setup = Join-Path $Root "dist\installer\ChessPlayerSetup-$Version.exe"
Write-Host "Built $setup" -ForegroundColor Green

# ── Install ──────────────────────────────────────────────────────────────────
if ($Install) {
    Write-Host "Installing (silent; closes a running Chess Player) ..."
    $p = Start-Process -FilePath $setup -Wait -PassThru `
        -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER"
    if ($p.ExitCode -ne 0) { throw "Installer failed (exit code $($p.ExitCode))" }
    Write-Host "Installed Chess Player $Version" -ForegroundColor Green
}
