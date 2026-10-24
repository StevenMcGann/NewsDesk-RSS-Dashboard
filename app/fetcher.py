"""HTTP fetching with native WinHTTP first and compatible fallback clients.

Automatic mode tries WinHTTP, PowerShell/.NET and urllib, remembering a successful
client per host for the current run. The .NET path supports Windows-authenticated
proxies; urllib remains a portable fallback. Browser sessions are independent.
"""
import base64
import http.client
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

from winhttp_client import DownloadTimeout, ResponseTooLarge, WinHttpClient

log = logging.getLogger(__name__)

TIMEOUT = 15         # seconds per connect or read
TOTAL_TIMEOUT = 60   # seconds for the whole download, however slowly it trickles in
MAX_REDIRECTS = 5
MAX_BYTES = 10 * 1024 * 1024
TOO_SLOW = "The site took too long to send its response"
# All clients identify honestly as the same feed reader while retaining their
# own networking behavior. Keep the proven CISA-compatible User-Agent stable.
USER_AGENT = "NewsDesk/1.0 (Windows; desktop feed reader)"
ACCEPT_FEED = ("application/rss+xml, application/atom+xml, application/feed+json, "
               "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8")
ACCEPT_PAGE = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
CISA_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
TEST_URL = "https://feeds.arstechnica.com/arstechnica/index"


def _winhttp_headers(url, accept):
    headers = {"Accept": accept, "Accept-Encoding": "gzip, deflate", "Connection": "close"}
    parts = urllib.parse.urlsplit(url)
    # The affected-PC comparison verified this profile with native WinHTTP as
    # well as COM. The regular profile was refused by both. Scope the change to
    # the tested feed; article pages and other publishers retain compression.
    if (accept == ACCEPT_FEED and parts.scheme == "https" and
            parts.hostname == "www.cisa.gov" and
            parts.path == "/cybersecurity-advisories/all.xml" and not parts.query):
        headers["Accept"] = CISA_ACCEPT
        del headers["Accept-Encoding"]
    return headers


class FetchError(Exception):
    def __init__(self, message, status=None, network=False, reference=None):
        if reference and not re.fullmatch(r"[\w.-]{1,80}", reference):
            reference = None  # it comes from the server, so only something that looks like an ID
        super().__init__(f"{message} (reference {reference})" if reference else message)
        self.reason = message
        self.status = status
        self.network = network  # couldn't reach the server at all (worth retrying another way)
        self.reference = reference  # Akamai's X-Reference-Error, which the site's operators can look up


def _both_failed(first: FetchError, second: FetchError, names) -> FetchError:
    """One error that reports both download methods, not just the first."""
    if first.status is not None and first.status == second.status:
        message = f"{first.reason}, from both download methods"
    else:
        message = f"{names[0]}: {first.reason}. {names[1]}: {second.reason}"
    return FetchError(message, first.status if first.status == second.status else None,
                      first.network and second.network, second.reference or first.reference)


def _downloads_failed(failures) -> FetchError:
    if len(failures) == 1:
        return failures[0][1]
    if len(failures) == 2:
        return _both_failed(failures[0][1], failures[1][1], [name for name, _ in failures])
    errors = [error for _, error in failures]
    same_status = errors[0].status is not None and all(e.status == errors[0].status for e in errors)
    message = (f"{errors[0].reason}, from all {len(errors)} download methods" if same_status else
               ". ".join(f"{name}: {error.reason}" for name, error in failures))
    reference = next((e.reference for e in reversed(errors) if e.reference), None)
    return FetchError(message, errors[0].status if same_status else None,
                      all(e.network for e in errors), reference)


@dataclass
class Response:
    status: int
    url: str
    body: bytes
    content_type: str
    etag: str
    last_modified: str
    via: str


def _ssl_context():
    ctx = ssl.create_default_context()  # on Windows this trusts the Windows certificate store
    # Python 3.13+ enables strict X.509 checks that reject some corporate TLS-inspection CAs.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError("Only http:// and https:// addresses are supported")
    return url.strip()


LOCAL_REFUSED = "That link points to this computer, so NewsDesk won't download it"


def is_local_address(url: str) -> bool:
    """True when the URL's host is this computer (loopback), link-local or unspecified.

    Private LAN ranges are allowed: intranet articles are legitimate. Hosts that don't
    resolve here are allowed too, since a proxy may resolve them (to its own network).
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return True
    return False


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to http(s), and (for article pages) never to this computer."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, block_local: bool):
        super().__init__()
        self.block_local = block_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise FetchError("The site redirected to an address that isn't http:// or https://")
        if self.block_local and is_local_address(newurl):
            raise FetchError(LOCAL_REFUSED)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, mode=lambda: "auto"):
        self._mode = mode
        self._ctx = _ssl_context()
        self.windows = PowerShellPool()
        self.winhttp = WinHttpClient()
        self._host_methods = {}  # preferences are per host and last for this run
        self._winhttp_slots = threading.BoundedSemaphore(3)

    def get(self, url, etag=None, last_modified=None, accept=ACCEPT_FEED, block_local=False) -> Response:
        """Download url. block_local refuses this computer's own addresses, for links that
        come from feed content (article pages) rather than from the user."""
        url = check_url(url)
        if block_local and is_local_address(url):
            raise FetchError(LOCAL_REFUSED)
        args = (url, etag, last_modified, accept, block_local)
        mode = self._mode()
        if mode == "python":
            return self.get_python(*args)
        if mode == "windows":
            return self.windows.get(*args)
        if mode == "winhttp":
            return self.get_winhttp(*args)
        host = urllib.parse.urlsplit(url).hostname
        methods = []
        if self.winhttp.available:
            methods.append(("WinHTTP", self.get_winhttp))
        if self.windows.available:
            methods.append(("Windows", self.windows.get))
        methods.append(("Python", self.get_python))
        preferred = self._host_methods.get(host)
        methods.sort(key=lambda entry: entry[0] != preferred)
        failures = []
        for name, method in methods:
            try:
                result = method(*args)
            except FetchError as error:
                failures.append((name, error))
                # Do not multiply 404/429 requests, body-limit failures or timeouts.
                if not (error.network or error.status == 403):
                    break
                continue
            self._host_methods[host] = name
            if failures:
                log.info("Used %s fetching for %s after: %s", result.via, host,
                         "; ".join(f"{label}: {e}" for label, e in failures))
            return result
        raise _downloads_failed(failures)

    def get_winhttp(self, url, etag=None, last_modified=None, accept=ACCEPT_FEED, block_local=False) -> Response:
        if not self.winhttp.available:
            raise FetchError("Windows WinHTTP is not available")
        current = check_url(url)
        with self._winhttp_slots:
            deadline = time.monotonic() + TOTAL_TIMEOUT
            for _ in range(MAX_REDIRECTS + 1):
                if block_local and is_local_address(current):
                    raise FetchError(LOCAL_REFUSED)
                headers = _winhttp_headers(current, accept)
                if etag:
                    headers["If-None-Match"] = etag
                if last_modified:
                    headers["If-Modified-Since"] = last_modified
                try:
                    status, body, response_headers = self.winhttp.request(
                        current, USER_AGENT, headers, deadline=deadline, timeout=TIMEOUT, max_bytes=MAX_BYTES)
                except DownloadTimeout:
                    raise FetchError(TOO_SLOW) from None
                except ResponseTooLarge as e:
                    raise FetchError(str(e)) from None
                except OSError as e:
                    raise FetchError(f"Could not connect through WinHTTP: {e}", network=True) from None
                except ValueError as e:
                    raise FetchError(str(e)) from None
                if status in (301, 302, 303, 307, 308) and response_headers.get("Location"):
                    current = check_url(urllib.parse.urljoin(current, response_headers["Location"]))
                    continue
                if status == 304:
                    return Response(304, current, b"", "", etag, last_modified, "winhttp")
                if status >= 400:
                    raise FetchError(_http_message(status), status, network=status == 407,
                                     reference=response_headers.get("X-Reference-Error"))
                body = _decompress(body, response_headers.get("Content-Encoding", ""))
                return Response(status, current, body, response_headers.get("Content-Type", ""),
                                response_headers.get("ETag"), response_headers.get("Last-Modified"), "winhttp")
        raise FetchError("The site redirected too many times")

    def get_python(self, url, etag=None, last_modified=None, accept=ACCEPT_FEED, block_local=False) -> Response:
        headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip, deflate"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=headers)
        # Like build_opener(), minus the ftp:, file: and data: handlers.
        opener = urllib.request.OpenerDirector()
        for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                        urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(context=self._ctx),
                        urllib.request.HTTPDefaultErrorHandler(), _Redirects(block_local),
                        urllib.request.HTTPErrorProcessor()):
            opener.add_handler(handler)
        try:
            deadline = time.monotonic() + TOTAL_TIMEOUT
            with opener.open(request, timeout=TIMEOUT) as resp:
                raw = _read_until(resp, deadline)
                body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                return Response(resp.status, resp.geturl(), body, resp.headers.get("Content-Type", ""),
                                resp.headers.get("ETag"), resp.headers.get("Last-Modified"), "python")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return Response(304, url, b"", "", etag, last_modified, "python")
            if e.code == 407:
                raise FetchError("The proxy asked for a login (HTTP 407)", 407, network=True) from None
            raise FetchError(_http_message(e.code), e.code, reference=e.headers.get("X-Reference-Error")) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", None) or e
            raise FetchError(f"Could not connect: {reason}", network=True) from None

    def diagnose(self, url=TEST_URL) -> dict:
        report = {"url": url, "proxy": proxy_summary(), "results": []}
        for name, method in (("winhttp", self.get_winhttp), ("windows", self.windows.get),
                             ("python", self.get_python)):
            started = time.monotonic()
            entry = {"method": name}
            try:
                r = method(url)
                entry.update(ok=True, status=r.status, bytes=len(r.body))
            except FetchError as e:
                entry.update(ok=False, error=str(e))
            entry["ms"] = int((time.monotonic() - started) * 1000)
            report["results"].append(entry)
        working = [r["method"] for r in report["results"] if r["ok"]]
        report["recommended"] = "auto" if working else None
        return report

    def close(self):
        self.windows.close()


def _read_until(resp, deadline: float) -> bytes:
    """Read the body in pieces, enforcing the size cap and a total deadline.

    The socket timeout only limits each read, so a server sending a byte every few
    seconds could otherwise hold a refresh worker almost forever.
    """
    chunks, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise FetchError(TOO_SLOW)
        chunk = resp.read1(65536)  # returns whatever has arrived, not a full 64 KB
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_BYTES:
            raise FetchError("The response is too large")
        chunks.append(chunk)


def _decompress(raw: bytes, encoding: str) -> bytes:
    encoding = encoding.lower().strip()
    if encoding not in ("gzip", "deflate", "x-gzip"):
        return raw
    candidates = [16 + zlib.MAX_WBITS] if "gzip" in encoding else [zlib.MAX_WBITS, -zlib.MAX_WBITS]
    for wbits in candidates:
        try:
            d = zlib.decompressobj(wbits)
            out = d.decompress(raw, MAX_BYTES + 1)
            if len(out) > MAX_BYTES or d.unconsumed_tail:
                raise FetchError("The response is too large")
            return out
        except zlib.error:
            continue
    raise FetchError("The response could not be decompressed")


def _http_message(code: int) -> str:
    known = {401: "needs a login", 403: "refused access", 404: "was not found",
             410: "is gone", 429: "is rate-limiting requests", 500: "had a server error",
             502: "had a gateway error", 503: "is unavailable"}
    return f"HTTP {code}: the site {known[code]}" if code in known else f"HTTP {code}"


def proxy_summary() -> dict:
    info = {"urllib_proxies": urllib.request.getproxies()}
    if os.name == "nt":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL", "AutoDetect"):
                    try:
                        info[name] = winreg.QueryValueEx(k, name)[0]
                    except OSError:
                        pass
        except OSError:
            pass
    return info


# ---- Windows PowerShell fallback -----------------------------------------------

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
$proxy = [Net.WebRequest]::GetSystemWebProxy()
$proxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials
$max = __MAX__
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  $req = $line | ConvertFrom-Json
  $out = @{}
  $clock = [Diagnostics.Stopwatch]::StartNew()
  try {
    $r = [Net.WebRequest]::Create($req.url)
    $r.AllowAutoRedirect = -not $req.manual_redirects
    $r.Proxy = $proxy
    # With keep-alive on, .NET sends "Connection: Keep-Alive" only on its first request to
    # a site and no Connection header after that, which cisa.gov's Akamai answers with 403.
    # "Connection: close" on every request is consistent, and what urllib sends too.
    $r.KeepAlive = $false
    $r.UserAgent = $req.ua
    $r.Accept = $req.accept
    $r.Timeout = __TIMEOUT__
    $r.ReadWriteTimeout = __TIMEOUT__
    $r.AutomaticDecompression = [Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate
    if ($req.etag) { $r.Headers.Add('If-None-Match', $req.etag) }
    if ($req.lm) { try { $r.IfModifiedSince = [DateTime]::Parse($req.lm, [Globalization.CultureInfo]::InvariantCulture) } catch {} }
    $resp = $null
    try { $resp = $r.GetResponse() } catch {
      $ex = $_.Exception
      while ($ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
      if ($ex -and $ex.Response) { $resp = $ex.Response } else { throw }
    }
    $out.status = [int]$resp.StatusCode
    $out.url = $resp.ResponseUri.AbsoluteUri
    $out.ct = $resp.ContentType
    $out.etag = $resp.Headers['ETag']
    $out.lm = $resp.Headers['Last-Modified']
    $out.location = $resp.Headers['Location']
    $out.ref = $resp.Headers['X-Reference-Error']
    $stream = $resp.GetResponseStream()
    $ms = New-Object IO.MemoryStream
    $buf = New-Object byte[] 65536
    while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
      $ms.Write($buf, 0, $n)
      if ($ms.Length -gt $max) { throw 'The response is too large' }
      if ($clock.Elapsed.TotalSeconds -gt __TOTAL__) { $out.too_slow = $true; throw 'too slow' }
    }
    $resp.Close()
    $out.body = [Convert]::ToBase64String($ms.ToArray())
  } catch {
    $ex = $_.Exception
    while ($ex.InnerException) { $ex = $ex.InnerException }
    $out.error = $ex.Message
  }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress))
  [Console]::Out.Flush()
}
"""


def _powershell_exe():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.exists(path) else None


class _Worker:
    """A long-lived powershell.exe that serves one request at a time over stdin/stdout."""

    def __init__(self, exe):
        script = (_PS_SCRIPT.replace("__MAX__", str(MAX_BYTES)).replace("__TIMEOUT__", str(TIMEOUT * 1000))
                  .replace("__TOTAL__", str(TOTAL_TIMEOUT)))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            self.proc = subprocess.Popen(
                [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise FetchError(f"Could not start Windows PowerShell: {e}", network=True) from None

    def request(self, payload: dict) -> dict:
        try:
            self.proc.stdin.write((json.dumps(payload) + "\n").encode("ascii"))
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except OSError as e:
            raise FetchError(f"Windows PowerShell stopped: {e}", network=True) from None
        if not line:
            raise FetchError("Windows PowerShell stopped unexpectedly (it may be blocked by policy)",
                             network=True)
        return json.loads(line.decode("utf-8", "replace"))

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()
        self.proc.stdout.close()


class PowerShellPool:
    def __init__(self, size=3):
        self._exe = _powershell_exe() if os.name == "nt" else None
        self._idle = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(size)

    @property
    def available(self) -> bool:
        return self._exe is not None

    def get(self, url, etag=None, last_modified=None, accept=ACCEPT_FEED, block_local=False) -> Response:
        if not self._exe:
            raise FetchError("Windows PowerShell is not available")
        payload = {"ua": USER_AGENT, "accept": accept, "etag": etag or "", "lm": last_modified or "",
                   # For article pages, follow redirects here so every hop is checked before
                   # it is requested; .NET would otherwise contact the target first.
                   "manual_redirects": block_local}
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if block_local and is_local_address(current):
                raise FetchError(LOCAL_REFUSED)
            result = self._request(dict(payload, url=current))
            status = int(result.get("status") or 0)
            if block_local and status in (301, 302, 303, 307, 308) and result.get("location"):
                current = urllib.parse.urljoin(current, result["location"])
                if urllib.parse.urlsplit(current).scheme not in ("http", "https"):
                    raise FetchError("The site redirected to an address that isn't http:// or https://")
                continue
            break
        else:
            raise FetchError("The site redirected too many times")
        if status == 304:
            return Response(304, url, b"", "", etag, last_modified, "windows")
        if status >= 400:
            raise FetchError(_http_message(status), status, network=status == 407, reference=result.get("ref"))
        body = base64.b64decode(result.get("body") or "")
        return Response(status, result.get("url") or current, body, result.get("ct") or "",
                        result.get("etag"), result.get("lm"), "windows")

    def _request(self, payload: dict) -> dict:
        with self._slots:
            try:
                worker = self._idle.get_nowait()
            except queue.Empty:
                worker = _Worker(self._exe)
            try:
                result = worker.request(payload)
            except Exception:
                worker.close()
                raise
            self._idle.put(worker)
        if result.get("too_slow"):
            raise FetchError(TOO_SLOW)  # not a network failure: don't retry the other way
        if result.get("error"):
            raise FetchError(f"Could not connect: {result['error']}", network=True)
        return result

    def close(self):
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break
