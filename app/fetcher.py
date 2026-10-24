"""HTTP fetching that also works behind corporate proxies.

Python's urllib reads the proxy from the Windows registry, but it can't use a PAC
script or log in to an NTLM/Kerberos proxy. When urllib can't reach the network,
the request is retried through Windows PowerShell's .NET HTTP stack, which uses the
same system proxy and Windows login that Edge does.
"""
import base64
import http.client
import ipaddress
import json
import logging
import os
import queue
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

log = logging.getLogger(__name__)

TIMEOUT = 15
MAX_BYTES = 10 * 1024 * 1024
# Identify honestly as a feed reader. A browser-looking User-Agent paired with Python's
# or .NET's TLS handshake looks like a disguised bot to CDNs such as Akamai, which then
# answer 403 (cisa.gov does this).
USER_AGENT = "NewsDesk/1.0 (Windows; desktop feed reader)"
ACCEPT_FEED = ("application/rss+xml, application/atom+xml, application/feed+json, "
               "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8")
ACCEPT_PAGE = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
TEST_URL = "https://feeds.arstechnica.com/arstechnica/index"


class FetchError(Exception):
    def __init__(self, message, status=None, network=False):
        super().__init__(message)
        self.status = status
        self.network = network  # couldn't reach the server at all (worth retrying another way)


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
        self._prefer_windows = False  # urllib can't reach the network at all (proxy)
        self._windows_hosts = set()   # sites that refuse urllib but accept Windows' HTTP stack
        self.windows = PowerShellPool()

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
        host = urllib.parse.urlsplit(url).hostname
        methods = [self.get_python, self.windows.get]
        if self._prefer_windows or host in self._windows_hosts:
            methods.reverse()
        try:
            return methods[0](*args)
        except FetchError as first:
            # Some CDNs (Akamai on cisa.gov, for one) refuse Python's TLS handshake with a 403
            # but accept the Windows stack Edge uses, so a 403 is worth one retry too.
            if not (first.network or first.status == 403) or not self.windows.available:
                raise
            try:
                result = methods[1](*args)
            except FetchError:
                raise first from None
            if first.network:
                self._prefer_windows = result.via == "windows"
            elif result.via == "windows":
                self._windows_hosts.add(host)
            else:
                self._windows_hosts.discard(host)
            log.info("Used %s fetching for %s after: %s", result.via, host, first)
            return result

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
            with opener.open(request, timeout=TIMEOUT) as resp:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise FetchError("The response is too large")
                body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
                return Response(resp.status, resp.geturl(), body, resp.headers.get("Content-Type", ""),
                                resp.headers.get("ETag"), resp.headers.get("Last-Modified"), "python")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return Response(304, url, b"", "", etag, last_modified, "python")
            if e.code == 407:
                raise FetchError("The proxy asked for a login (HTTP 407)", 407, network=True) from None
            raise FetchError(_http_message(e.code), e.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", None) or e
            raise FetchError(f"Could not connect: {reason}", network=True) from None

    def diagnose(self, url=TEST_URL) -> dict:
        report = {"url": url, "proxy": proxy_summary(), "results": []}
        for name, method in (("python", self.get_python), ("windows", self.windows.get)):
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
        report["recommended"] = "auto" if "python" in working else (working[0] if working else None)
        return report

    def close(self):
        self.windows.close()


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
  try {
    $r = [Net.WebRequest]::Create($req.url)
    $r.Proxy = $proxy
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
    $stream = $resp.GetResponseStream()
    $ms = New-Object IO.MemoryStream
    $buf = New-Object byte[] 65536
    while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
      $ms.Write($buf, 0, $n)
      if ($ms.Length -gt $max) { throw 'The response is too large' }
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
        script = _PS_SCRIPT.replace("__MAX__", str(MAX_BYTES)).replace("__TIMEOUT__", str(TIMEOUT * 1000))
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
        if block_local and is_local_address(url):
            raise FetchError(LOCAL_REFUSED)
        payload = {"url": url, "ua": USER_AGENT, "accept": accept, "etag": etag or "", "lm": last_modified or ""}
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
        if result.get("error"):
            raise FetchError(f"Could not connect: {result['error']}", network=True)
        status = int(result.get("status") or 0)
        if status == 304:
            return Response(304, url, b"", "", etag, last_modified, "windows")
        if status >= 400:
            raise FetchError(_http_message(status), status, network=status == 407)
        final_url = result.get("url") or url
        # .NET follows redirects itself (http/https only), so check where it ended up.
        if block_local and final_url != url and is_local_address(final_url):
            raise FetchError(LOCAL_REFUSED)
        body = base64.b64decode(result.get("body") or "")
        return Response(status, final_url, body, result.get("ct") or "",
                        result.get("etag"), result.get("lm"), "windows")

    def close(self):
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                break
