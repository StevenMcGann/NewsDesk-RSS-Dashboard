<#
.SYNOPSIS
    Compare the successful CISA COM probe with NewsDesk's installed WinHTTP client.
.DESCRIPTION
    Makes six checks against the original public CISA feed. Compares the original
    COM probe, the installed client, request headers, and proxy resolution in one
    run. Does not modify NewsDesk, its data, or Windows settings. Leaves TLS
    verification on and automatic authentication off. No browser profile is used.

    The COM checks buffer the response before the 10 MiB parsing limit, just like
    the original fixed-URL diagnostic; this is not a production downloader.
    Console JSON omits proxy addresses, response bodies, and local paths.
.PARAMETER NewsDeskRoot
    Folder containing app and python. Defaults to the per-user installed copy,
    then the current folder or the folder containing this script.
.EXAMPLE
    powershell.exe -NoProfile -File .\Test-CisaComparison.ps1
.EXAMPLE
    powershell.exe -NoProfile -File .\Test-CisaComparison.ps1 -NewsDeskRoot 'C:\Tools\NewsDesk'
#>
[CmdletBinding()]
param([string]$NewsDeskRoot)

$ErrorActionPreference = 'Stop'
if (-not $NewsDeskRoot) {
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'Programs\NewsDesk' }
    $candidates += (Get-Location).Path
    $candidates += $PSScriptRoot
    foreach ($candidate in $candidates) {
        if ((Test-Path -LiteralPath (Join-Path $candidate 'python\python.exe')) -and
            (Test-Path -LiteralPath (Join-Path $candidate 'app\winhttp_client.py'))) {
            $NewsDeskRoot = $candidate
            break
        }
    }
}
if (-not $NewsDeskRoot -or
    -not (Test-Path -LiteralPath (Join-Path $NewsDeskRoot 'python\python.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $NewsDeskRoot 'app\winhttp_client.py'))) {
    throw 'NewsDesk was not found. Use -NewsDeskRoot with the folder containing app and python.'
}
$python = Join-Path $NewsDeskRoot 'python\python.exe'
$code = @'
import base64
import datetime
import hashlib
import json
import os
import pathlib
import re
import struct
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / "app"), str(root / "app" / "vendor")]
import config
import feedparser
import fetcher

URL = "https://www.cisa.gov/cybersecurity-advisories/all.xml"
PROBE_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
PROBE_UA = "NewsDesk/1.0 (Windows; desktop feed reader)"
POWERSHELL = str(pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) /
                 "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")

# Only this child-process protocol includes the resolved proxy and base64 body.
# Neither is copied to the report. Authentication and redirects stay disabled.
COM_CODE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
$inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$feedUri = [Uri]'https://www.cisa.gov/cybersecurity-advisories/all.xml'
$client = $null
$out = @{}
try {
    $proxy = [Net.WebRequest]::GetSystemWebProxy()
    $proxyUri = $proxy.GetProxy($feedUri)
    $viaProxy = $null -ne $proxyUri -and $proxyUri -ne $feedUri
    $out.proxy_route = if ($viaProxy) { 'system proxy' } else { 'system resolved direct' }
    if ($inputData.resolve_only) {
        $out.proxy = if ($viaProxy) { $proxyUri.Authority } else { $null }
    } else {
        $client = New-Object -ComObject WinHttp.WinHttpRequest.5.1
        $client.SetTimeouts(10000, 10000, 10000, 15000)
        $client.Open('GET', $feedUri.AbsoluteUri, $true)
        if ($viaProxy) { $client.SetProxy(2, $proxyUri.Authority) } else { $client.SetProxy(1) }
        $client.SetAutoLogonPolicy(2)
        $client.Option(6) = $false
        $client.SetRequestHeader('User-Agent', [string]$inputData.ua)
        $client.SetRequestHeader('Accept', [string]$inputData.accept)
        if ($inputData.encoding) { $client.SetRequestHeader('Accept-Encoding', [string]$inputData.encoding) }
        $client.SetRequestHeader('Connection', 'close')
        $client.Send()
        if (-not $client.WaitForResponse(30)) { $client.Abort(); throw 'Timeout' }
        $out.status = [int]$client.Status
        foreach ($pair in @(@('content_type', 'Content-Type'), @('encoding', 'Content-Encoding'), @('reference', 'X-Reference-Error'))) {
            try { $out[$pair[0]] = $client.GetResponseHeader($pair[1]) } catch {}
        }
        if ($out.status -eq 200) {
            [byte[]]$body = $client.ResponseBody
            $out.raw_bytes = $body.Length
            if ($body.Length -gt 10MB) { $out.error = 'Response exceeds 10 MiB' }
            else { $out.body = [Convert]::ToBase64String($body) }
        }
    }
} catch { $out.error = $_.Exception.GetType().Name }
finally {
    if ($client) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($client) }
}
$out | ConvertTo-Json -Compress
"""


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def com_request(**payload):
    encoded = base64.b64encode(COM_CODE.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        input=json.dumps(payload), capture_output=True, text=True, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("COM diagnostic process failed")
    return json.loads(result.stdout.lstrip("\ufeff"))


def valid_body(body, entry):
    if len(body) > fetcher.MAX_BYTES:
        raise ValueError("Response exceeds limit")
    parsed = feedparser.parse(body)
    entry.update(bytes=len(body), items=len(parsed.entries),
                 valid_feed=bool(parsed.version and parsed.entries))
    if not entry["valid_feed"]:
        entry["error"] = "HTTP 200 did not contain a nonempty RSS/Atom feed"


def run_check(label, method, profile, route, call):
    entry = dict(check=label, checked_at_utc=utc(), method=method,
                 header_profile=profile, proxy_mode=route, status=None,
                 reference=None, valid_feed=False)
    started = time.monotonic()
    try:
        call(entry)
        if entry["status"] is not None and entry["status"] != 200:
            entry["error"] = "HTTP %s" % entry["status"]
    except fetcher.FetchError as error:
        entry.update(status=error.status, reference=error.reference,
                     error="HTTP %s" % error.status if error.status else "Native WinHTTP request failed")
    except Exception as error:
        # Do not expose exception strings: OS/proxy errors can contain local paths
        # or internal addresses. The error type still distinguishes failed checks.
        entry["error"] = type(error).__name__
    reference = entry.get("reference")
    if reference and not re.fullmatch(r"[\w.-]{1,80}", reference):
        entry["reference"] = None
    entry["seconds"] = round(time.monotonic() - started, 2)
    return entry


def check_com(entry, app_headers=False):
    result = com_request(ua=fetcher.USER_AGENT if app_headers else PROBE_UA,
                         accept=fetcher.ACCEPT_FEED if app_headers else PROBE_ACCEPT,
                         encoding="gzip, deflate" if app_headers else None)
    for key in ("status", "content_type", "reference", "proxy_route", "raw_bytes", "error"):
        if key in result:
            entry[key] = result[key]
    if "body" in result:
        body = fetcher._decompress(base64.b64decode(result["body"], validate=True), result.get("encoding", ""))
        valid_body(body, entry)


class ResolvedRoute:
    """Apply only the same per-request route that the COM probe resolved."""
    def __init__(self, dll, proxy):
        self.dll, self.proxy = dll, proxy

    def __getattr__(self, name):
        return getattr(self.dll, name)

    def WinHttpOpen(self, agent, access, proxy, bypass, flags):
        return self.dll.WinHttpOpen(agent, 3 if self.proxy else 1, self.proxy, None, flags)


def check_native(entry, probe_headers=False, resolved=False):
    client = fetcher.Fetcher(mode=lambda: "winhttp")
    try:
        if resolved:
            route = com_request(resolve_only=True)
            if "proxy_route" not in route:
                raise RuntimeError("System proxy resolution failed")
            entry["proxy_route"] = route["proxy_route"]
            client.winhttp._dll = ResolvedRoute(client.winhttp._dll, route.get("proxy"))
        if probe_headers:
            # Inject headers at the existing client's request boundary. All
            # streaming, timeout, redirect and TLS behavior stays in installed code.
            original = client.winhttp.request
            def request(url, user_agent, headers, **options):
                return original(url, PROBE_UA, {"Accept": PROBE_ACCEPT, "Connection": "close"}, **options)
            client.winhttp.request = request
        response = client.get_winhttp(URL)
        entry.update(status=response.status, content_type=response.content_type)
        valid_body(response.body, entry)
    finally:
        client.close()


report = dict(diagnostic="NewsDesk CISA comparison 1", checked_at_utc=utc(), url=URL,
              newsdesk_version=config.VERSION, python_version=sys.version.split()[0],
              process_bits=struct.calcsize("P") * 8,
              windows_version="%d.%d.%d" % tuple(sys.getwindowsversion()[:3]),
              source_sha256={name: hashlib.sha256((root / "app" / name).read_bytes()).hexdigest()
                             for name in ("config.py", "fetcher.py", "winhttp_client.py")},
              results=[])
checks = [
    ("com_original_before", "WinHTTP COM", "original probe", "system resolved", lambda e: check_com(e)),
    ("native_installed", "native WinHTTP", "installed app", "automatic", lambda e: check_native(e)),
    ("com_app_headers", "WinHTTP COM", "installed app", "system resolved", lambda e: check_com(e, app_headers=True)),
    ("native_probe_headers", "native WinHTTP", "original probe", "automatic", lambda e: check_native(e, probe_headers=True)),
    ("native_probe_headers_resolved", "native WinHTTP", "original probe", "system resolved", lambda e: check_native(e, probe_headers=True, resolved=True)),
    ("com_original_after", "WinHTTP COM", "original probe", "system resolved", lambda e: check_com(e)),
]
for label, method, profile, route, call in checks:
    report["results"].append(run_check(label, method, profile, route, call))
print(json.dumps(report, indent=2))
'@

# No policy bypass, software installation, data/config writes, or app restart.
$code | & $python -I -B - $NewsDeskRoot
if ($LASTEXITCODE) { throw 'The comparison did not complete; no NewsDesk settings were changed.' }
