<#
.SYNOPSIS
    Builds the portable NewsDesk RSS Dashboard zip that users unzip and double-click.

.DESCRIPTION
    Downloads python.org's Windows embeddable Python (pinned version, checked against
    its published SHA-256 and its Authenticode signature), adds the app, runs a smoke
    test with that runtime, and writes dist\NewsDesk-<version>.zip.

    The embeddable runtime needs no install and no admin rights. launcher.py puts
    app\ and app\vendor\ on sys.path itself, so the runtime's ._pth file is left as shipped.

.PARAMETER Test
    Also run the unit tests with the bundled runtime.

.EXAMPLE
    pwsh -File .\build_portable.ps1 -Test
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.14.8',
    # SHA-256 published on https://www.python.org/downloads/release/python-3148/
    [string]$PythonSha256 = 'a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310',
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = $PSScriptRoot
$appVersion = (Select-String -Path (Join-Path $root 'app\config.py') -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$dist = Join-Path $root 'dist'
$cache = Join-Path $dist 'cache'
$stage = Join-Path $dist 'NewsDesk'
$zipPath = Join-Path $dist "NewsDesk-$appVersion.zip"
New-Item -ItemType Directory -Force $cache | Out-Null

# 1. Python runtime: download once, verify every build.
$pyZip = Join-Path $cache "python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $pyZip)) {
    Write-Host "Downloading Python $PythonVersion (embeddable)..."
    $part = "$pyZip.part"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $part -UseBasicParsing
    Move-Item $part $pyZip -Force
}
$hash = (Get-FileHash $pyZip -Algorithm SHA256).Hash
if ($hash -ne $PythonSha256.ToUpperInvariant()) {
    Remove-Item $pyZip -Force
    throw "SHA-256 mismatch for $pyZip. Expected $PythonSha256, got $hash. The download was deleted."
}

# 2. Stage: runtime + app + launcher.
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Expand-Archive $pyZip (Join-Path $stage 'python')
$pythonExe = Join-Path $stage 'python\python.exe'
$sig = Get-AuthenticodeSignature $pythonExe
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    throw "python.exe signature check failed: $($sig.Status) / $($sig.SignerCertificate.Subject)"
}

Copy-Item (Join-Path $root 'app') (Join-Path $stage 'app') -Recurse
Get-ChildItem (Join-Path $stage 'app') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
foreach ($file in 'Start NewsDesk.bat', 'Install NewsDesk.bat', 'starter-feeds.opml', 'THIRD-PARTY.md', 'LICENSE') {
    Copy-Item (Join-Path $root $file) $stage
}
Copy-Item (Join-Path $root 'USER-GUIDE.md') (Join-Path $stage 'README.md')
Copy-Item (Join-Path $root 'assets\newsdesk.ico') (Join-Path $stage 'NewsDesk.ico')

# 3. Smoke test with the bundled runtime (also proves sqlite3 and ssl are present).
$check = "import sys; sys.path[:0] = [r'$stage\app\vendor', r'$stage\app']; " +
         "import sqlite3, ssl, feedparser, launcher, server; print('Bundled', sys.version.split()[0], 'imports OK')"
& $pythonExe -B -c $check
if ($LASTEXITCODE) { throw 'Smoke test failed' }
if ($Test) {
    & $pythonExe -B -m unittest discover -s (Join-Path $root 'tests') -t (Join-Path $root 'tests')
    if ($LASTEXITCODE) { throw 'Unit tests failed on the bundled runtime' }
}
Get-ChildItem $stage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

# 4. Zip it (the zip holds a single "NewsDesk" folder).
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $stage -DestinationPath $zipPath
$size = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "Built $zipPath ($size MB) with Python $PythonVersion"
