# CISA feed refusals: issue #2

The affected URL is `https://www.cisa.gov/cybersecurity-advisories/all.xml`.
Issue: <https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/issues/2>.

## What the evidence establishes

The reporter's work PC receives HTTP 403 from both NewsDesk download methods,
including version 1.0.4, while Edge can open the feed. An Akamai reference ID and
a DigiCert certificate are consistent with a refusal from CISA's CDN. They do
not identify the exact CDN rule or prove that no explicit proxy is involved.
TLS pass-through proxies need not replace the origin certificate.

A separate development-PC check on October 24, 2026 returned:

| Client | Result |
|---|---|
| Current NewsDesk Python/urllib | HTTP 403, Akamai reference |
| Current NewsDesk Windows/.NET | HTTP 200, valid RSS, 30 articles |
| Native Windows WinHTTP COM | HTTP 200, valid RSS, 30 articles |

These results show that a non-browser client can work from one network. They
do not establish that any of these clients will work from the affected work PC.
The 1.0.4 keep-alive change is already present; repeating it is not a new fix.
The diagnostic itself was run with Windows PowerShell 5.1 and returned a valid
RSS document with 30 items in 1.3 seconds at `2026-10-24T21:07:59Z`.

The Windows fallback uses .NET HttpWebRequest and Windows proxy/trust settings.
It does not use Edge's Chromium network engine, browser state or browser
session. A browser User-Agent alone does not make those clients equivalent.

## Work-PC result and version 1.0.5

The reporter ran the WinHTTP diagnostic on the affected work PC at
`2026-10-24T21:23:15.8528909Z`. It returned HTTP 200, 5,696,987 bytes, RSS with
30 items, `valid_feed: true`, a system-resolved direct route and a duration of
1.96 seconds. This establishes a working alternative Windows HTTP stack on
that PC; it does not identify the CDN's rule for the earlier refusals.

Version 1.0.5 makes that stack the first choice in Automatic mode and provides
**Settings > Connection > Windows (WinHTTP)** to select it explicitly. Automatic
mode starts with **WinHTTP -> .NET -> Python**, remembering a successful client
per host until the app restarts. The .NET path remains useful for Windows-
authenticated proxies; Python provides the final fallback.
The production client streams through winhttp.dll rather than buffering the
whole response in the diagnostic's COM ResponseBody. It preserves byte and
decompression caps, a shared redirect/download deadline, HTTP(S)-only redirects,
article local-address checks and TLS validation. It sends no automatic origin
credentials or cookies. The .NET client remains for integrated proxy support.

After installing the updated build on the work PC, add/refresh the original
All CISA Advisories URL and verify a second refresh. Leave Automatic selected,
or choose Windows (WinHTTP) to exercise that client directly. The diagnostic
result is confirmed on the work PC; the integrated application still requires
this final work-PC check before issue #2 can be closed.

### Packaged-build validation

The portable 1.0.5 build was checked with its bundled Python 3.14.8:

- The release test results are recorded in the
  [1.0.5 release notes](releases/1.0.5.md).
- Local HTTP regression cases covered refusal recovery and host preference,
  conditional GET/304, repeated connection headers, local/non-HTTP redirects,
  redirect limits, byte/decompression caps, trickling-response deadlines,
  header injection and rejection of automatic origin authentication.
- A live packaged-runtime subscription test fetches the actual CISA feed through
  the default WinHTTP path and verifies a second refresh. Network-independent
  tests cover .NET/Python fallback selection and per-host learning.
- ZIP CRC checks passed and the packaged downloader/config/UI bytes matched the
  reviewed sources.

Artifact: `NewsDesk-1.0.5.zip` on the
[1.0.5 release](https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard/releases/tag/v1.0.5).
The published release includes the final SHA-256 checksum.

## Work-PC follow-up: 1.0.5 still refused

At `2026-10-24T22:56:54Z`, the reporter confirmed that the updated application
still receives HTTP 403 from **all three clients**, with reference
`18.e518d017.1792882522.bbac835e`. Version 1.0.5 is therefore not a confirmed fix
for the affected PC. The successful earlier COM diagnostic does not establish
that the native streaming client is equivalent in that environment.

The outgoing HTTP/1.1 requests were compared against a local development server.
The original COM diagnostic sends a shorter `Accept` value and no
`Accept-Encoding` header. The application also advertises JSON feeds and
`text/xml`, and sends `Accept-Encoding: gzip, deflate`. Both identify as the same
NewsDesk reader and send `Connection: close`. Separately, the native client uses
WinHTTP automatic proxy discovery, whereas the COM diagnostic supplies the route
resolved by .NET's Windows proxy API. These are differences to test, not proven
causes of the refusal. Both clients still returned valid CISA RSS on the
development PC during this follow-up.

### One-run comparison on the affected PC

Download `tools/Test-CisaComparison.ps1` and run it with the installed copy of
NewsDesk available:

```powershell
powershell.exe -NoProfile -File .\Test-CisaComparison.ps1
```

The default location is `%LOCALAPPDATA%\Programs\NewsDesk`. For a portable or
custom installation, specify the folder containing `app` and `python`:

```powershell
powershell.exe -NoProfile -File .\Test-CisaComparison.ps1 -NewsDeskRoot 'C:\Tools\NewsDesk'
```

Use the organization's normal script policy. The script uses NewsDesk's own
bundled Python and installed downloader, without changing either. It makes six
checks against the same original CISA URL, with no application-level fallback
retries:

| Check | Purpose |
|---|---|
| `com_original_before` | Repeat the previously successful probe now |
| `native_installed` | Exercise the installed application's WinHTTP request |
| `com_app_headers` | Change the COM probe to the application's headers |
| `native_probe_headers` | Change the native request to the original probe's headers |
| `native_probe_headers_resolved` | Also match the COM probe's resolved proxy route |
| `com_original_after` | Check whether the baseline stayed consistent during the run |

The JSON includes the app/runtime versions, downloader source hashes, individual
UTC timestamps, HTTP statuses, sanitized reference IDs, and valid-feed/item
counts. It omits response bodies, proxy addresses, local paths, account names,
and browser data. Native downloads retain their streaming/decompression caps
and deadlines. As in the original diagnostic, COM buffers the fixed public
feed before checking its size. No certificates, credentials, Windows settings,
application settings, or subscriptions are changed.

Interpret the comparison cautiously:

- If the COM baselines are valid and the COM/native results change with headers,
  the header profile is a candidate for a targeted production change.
- If native succeeds only after matching the resolved proxy route, investigate
  proxy-resolution differences; do not force a direct route globally.
- If only COM succeeds, another COM/native difference remains. A streaming
  equivalent or a separately bounded COM transport needs further validation.
- If both COM baselines now fail, the earlier success is not repeatable during
  this run. That does not identify the CDN rule; preserve the new timestamps and
  references for publisher-side investigation.
- If the reported version or source hashes do not match the installed release,
  first resolve the installation mismatch.

Keep issue #2 open until a change is verified in the integrated application on
the affected PC. Development-network success alone is insufficient.

## Standalone diagnostic

Copy `tools/Test-CisaWinHttp.ps1` to that PC and run it in Windows PowerShell:

```powershell
powershell.exe -NoProfile -File .\Test-CisaWinHttp.ps1
```

Use the organization's normal script policy. If policy blocks the script, have
IT run or approve it through the normal process.

The script makes one public-feed request with an honest NewsDesk User-Agent,
matches the system proxy's resolved route, leaves certificate validation on,
disables redirects and automatic Windows authentication, and checks for a
nonempty RSS document. Its output contains no browser cookies or proxy address.
The WinHTTP response is buffered by COM before the diagnostic parsing limit is
checked; this fixed-URL probe is not a replacement production downloader.

Interpret the result:

- **`valid_feed: true`:** The COM probe worked for this request. Use the one-run
  comparison above to determine whether the installed streaming client and its
  headers/route work at the same time. Verify the integrated app separately.
- **HTTP 403:** WinHTTP did not solve that environment's refusal. Further
  generic retries or guessed browser headers are not a demonstrated fix.
- **HTTP 407:** The probe needs proxy authentication and is inconclusive about
  CISA's CDN. It deliberately never sends Windows credentials automatically.
- **TLS/connection error:** Investigate trust, proxy resolution or transport
  reachability before attributing the failure to bot protection.

## If all ordinary clients are refused

The durable publisher-side remedy is for CISA's site operators to permit
legitimate RSS clients on this endpoint. Supply the exact URL, UTC time, complete
Akamai reference ID and, through IT, the relevant egress address. No report has
been sent automatically.

An application-side alternative is an explicit browser-assisted transport using
Edge/WebView2. That uses a real browser engine; HttpWebRequest does not. It adds
runtime/package, profile-isolation and untrusted-navigation work, and still
needs validation on the affected network. Do not assume a headless browser will
pass every CDN rule or borrow the user's normal browser cookies/profile.

An organization-approved feed relay is another option, but changes the source
and trust/deployment model. Do not silently substitute a narrower KEV feed or a
third-party mirror for All CISA Advisories.

## References

- [Akamai detection methods](https://techdocs.akamai.com/cloud-security/docs/detection-methods)
- [Akamai reference IDs](https://techdocs.akamai.com/property-mgr/docs/inject-reference-id)
- [Microsoft WinHttpRequest](https://learn.microsoft.com/en-us/windows/win32/winhttp/winhttprequest)
- [WinHTTP proxy configuration](https://learn.microsoft.com/en-us/windows/win32/winhttp/iwinhttprequest-setproxy)
- [WinHTTP automatic logon policy](https://learn.microsoft.com/en-us/windows/win32/winhttp/iwinhttprequest-setautologonpolicy)
- [WebView2 network requests and response bodies](https://learn.microsoft.com/en-us/microsoft-edge/webview2/how-to/webresourcerequested)
