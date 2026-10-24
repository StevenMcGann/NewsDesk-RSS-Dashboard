<#
.SYNOPSIS
    Test an alternative Windows HTTP client for NewsDesk issue #2.
.DESCRIPTION
    Makes one request to CISA's public RSS feed, using WinHTTP rather than the
    app's urllib or .NET HttpWebRequest client. Uses the system proxy's resolved
    route for this URL. Does not alter Windows settings, disable certificate
    checks, send automatic Windows credentials, or access browser cookies.

    Run on the affected PC in Windows PowerShell:
        powershell.exe -NoProfile -File .\Test-CisaWinHttp.ps1

    Prints JSON to the console. A successful test is a valid RSS document,
    not just an HTTP 200 response. This is a diagnostic, not an installed fix.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$feedUri = [Uri]'https://www.cisa.gov/cybersecurity-advisories/all.xml'
$client = $null
$reader = $null
$stream = $null
$clock = [Diagnostics.Stopwatch]::StartNew()
$report = [ordered]@{
    checked_at_utc = [DateTime]::UtcNow.ToString('o')
    url = $feedUri.AbsoluteUri
    method = 'Windows WinHTTP COM'
    user_agent = 'NewsDesk/1.0 (Windows; desktop feed reader)'
    proxy_route = $null
    status = $null
    content_type = $null
    reference = $null
    bytes = 0
    root_element = $null
    items = 0
    valid_feed = $false
    error = $null
    seconds = 0
}

try {
    # Match the route resolved by the app's Windows proxy API; do not silently
    # take a direct route when the company uses a proxy/PAC configuration.
    $proxy = [Net.WebRequest]::GetSystemWebProxy()
    $proxyUri = $proxy.GetProxy($feedUri)
    $viaProxy = $null -ne $proxyUri -and $proxyUri -ne $feedUri
    $report.proxy_route = if ($viaProxy) { 'system proxy' } else { 'system resolved direct' }

    $client = New-Object -ComObject WinHttp.WinHttpRequest.5.1
    $client.SetTimeouts(10000, 10000, 10000, 15000)
    $client.Open('GET', $feedUri.AbsoluteUri, $true)
    if ($viaProxy) {
        $client.SetProxy(2, $proxyUri.Authority)
    } else {
        $client.SetProxy(1)
    }
    $client.SetAutoLogonPolicy(2) # Never send NTLM/Negotiate automatically.
    $client.Option(6) = $false   # Do not follow an unexpected redirect.
    $client.SetRequestHeader('User-Agent', $report.user_agent)
    $client.SetRequestHeader('Accept', 'application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8')
    $client.SetRequestHeader('Connection', 'close')
    $client.Send()
    if (-not $client.WaitForResponse(30)) {
        $client.Abort()
        throw 'No complete response within 30 seconds.'
    }

    $report.status = [int]$client.Status
    try { $report.content_type = $client.GetResponseHeader('Content-Type') } catch {}
    try { $report.reference = $client.GetResponseHeader('X-Reference-Error') } catch {}
    if ($report.reference -and $report.reference -notmatch '^[\w.-]{1,80}$') {
        $report.reference = $null
    }
    if ($report.status -ne 200) {
        throw ('HTTP {0}. A 407 means proxy authentication is required; this probe deliberately does not send Windows credentials.' -f $report.status)
    }

    [byte[]]$body = $client.ResponseBody
    $report.bytes = $body.Length
    if ($body.Length -gt 10MB) { throw 'Response exceeds the 10 MiB diagnostic parsing limit.' }
    $stream = [IO.MemoryStream]::new($body, $false)
    $xmlSettings = [Xml.XmlReaderSettings]::new()
    $xmlSettings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $xmlSettings.XmlResolver = $null
    $xmlSettings.MaxCharactersInDocument = 10MB
    $reader = [Xml.XmlReader]::Create($stream, $xmlSettings)
    while ($reader.Read()) {
        if ($reader.NodeType -ne [Xml.XmlNodeType]::Element) { continue }
        if ($reader.Depth -eq 0) { $report.root_element = $reader.LocalName }
        if ($reader.LocalName -in @('item', 'entry')) { $report.items++ }
    }
    $report.valid_feed = $report.root_element -in @('rss', 'feed', 'RDF') -and $report.items -gt 0
    if (-not $report.valid_feed) { throw 'HTTP 200 did not contain a nonempty RSS/Atom feed.' }
} catch {
    # Report the failure without dumping response content, proxy addresses,
    # cookies, account names or local filesystem locations.
    $report.error = $_.Exception.Message
} finally {
    if ($reader) { $reader.Dispose() }
    if ($stream) { $stream.Dispose() }
    if ($client) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($client) }
    $report.seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 2)
}

$report | ConvertTo-Json -Depth 3
if (-not $report.valid_feed) { exit 1 }
