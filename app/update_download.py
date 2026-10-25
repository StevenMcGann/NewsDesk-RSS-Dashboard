"""Bounded release downloads. Feed fetching keeps its independent 10 MiB cap."""
import base64
import http.client
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from fetcher import FetchError, USER_AGENT, _powershell_exe, _ssl_context
from winhttp_client import DownloadTimeout, ResponseTooLarge

MAX_ARCHIVE = 128 * 1024 * 1024
DOWNLOAD_SECONDS = 600
CDN_HOSTS = {"github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateCancelled(Exception):
    pass


def trusted_download_url(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or p.hostname not in CDN_HOSTS or p.port not in (None, 443)
            or p.username is not None or p.password is not None or p.fragment):
        raise ValueError("The update redirected outside GitHub's HTTPS download hosts")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# One hop per process. Only the proxy receives Windows credentials. There is no
# automatic redirect/decompression, and bytes are written directly to the file.
_PS_DOWNLOAD = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
$d = [Console]::In.ReadLine() | ConvertFrom-Json
$resp = $null; $stream = $null; $file = $null
try {
  $proxy = [Net.WebRequest]::GetSystemWebProxy()
  $proxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials
  $r = [Net.WebRequest]::Create($d.url)
  $r.Proxy = $proxy; $r.AllowAutoRedirect = $false; $r.KeepAlive = $false
  $r.UserAgent = $d.ua; $r.Accept = 'application/octet-stream'
  $r.Headers.Add('Accept-Encoding', 'identity')
  $r.Timeout = 15000; $r.ReadWriteTimeout = 15000
  try { $resp = $r.GetResponse() } catch {
    $ex = $_.Exception
    while ($ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
    if ($ex -and $ex.Response) { $resp = $ex.Response } else { throw }
  }
  $status = [int]$resp.StatusCode
  $headers = @{'Location'=$resp.Headers['Location']; 'Content-Encoding'=$resp.Headers['Content-Encoding']}
  if ($resp.ContentLength -gt $d.max) { throw 'Update download exceeds its size limit' }
  if ($status -eq 200) {
    if ($resp.Headers['Content-Encoding'] -and $resp.Headers['Content-Encoding'] -ne 'identity') { throw 'Unexpected download encoding' }
    $stream = $resp.GetResponseStream(); $file = [IO.File]::Create($d.path)
    $buf = New-Object byte[] 65536; $total = 0
    while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
      $total += $n
      if ($total -gt $d.max) { throw 'Update download exceeds its size limit' }
      $file.Write($buf, 0, $n)
      [Console]::Out.WriteLine((@{progress=$total} | ConvertTo-Json -Compress))
      [Console]::Out.Flush()
    }
    $file.Flush(); $file.Dispose(); $file = $null
  }
  [Console]::Out.WriteLine((@{status=$status;headers=$headers} | ConvertTo-Json -Compress))
} catch {
  $fatal = $_.Exception.Message -match 'size limit|Unexpected download encoding'
  [Console]::Out.WriteLine((@{error='Windows could not download the release';fatal=$fatal} | ConvertTo-Json -Compress))
} finally {
  if ($file) {$file.Dispose()}; if ($stream) {$stream.Dispose()}; if ($resp) {$resp.Close()}
}
"""


class ReleaseDownloader:
    def __init__(self, fetcher, validate=trusted_download_url):
        self.fetcher, self.validate = fetcher, validate
        self.deadline = time.monotonic() + DOWNLOAD_SECONDS

    @staticmethod
    def _cancel(event):
        if event.is_set():
            raise UpdateCancelled("Update cancelled")

    def download(self, url, destination, size, cancel, progress=lambda n: None, limit=MAX_ARCHIVE):
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= limit:
            raise ValueError("The update has an invalid or excessive download size")
        mode = self.fetcher._mode()
        methods = []
        if self.fetcher.winhttp.available:
            methods.append(("winhttp", self._native))
        if self.fetcher.windows.available:
            methods.append(("windows", self._windows))
        methods.append(("python", self._python))
        if mode != "auto":
            methods = [(name, method) for name, method in methods if name == mode]
        else:
            preferred = self.fetcher._host_methods.get(urllib.parse.urlsplit(url).hostname, "").lower()
            methods.sort(key=lambda m: m[0] != preferred)
        if not methods:
            raise FetchError("The selected download method is unavailable")
        deadline = self.deadline  # Shared by the archive and its checksum, including retries.
        failure = None
        for name, method in methods:
            self._cancel(cancel)
            current = url
            try:
                for _ in range(6):
                    self.validate(current)
                    self._cancel(cancel)
                    if time.monotonic() >= deadline:
                        raise DownloadTimeout("The update download took too long")
                    status, headers = method(current, destination, size, deadline, cancel, progress)
                    if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                        current = urllib.parse.urljoin(current, headers["Location"])
                        continue
                    if status != 200:
                        raise FetchError(f"Update download returned HTTP {status}", status, network=status == 407)
                    if destination.stat().st_size != size:
                        raise ValueError("The update download is incomplete or its size changed")
                    self.fetcher._host_methods[urllib.parse.urlsplit(url).hostname] = {
                        "winhttp": "WinHTTP", "windows": "Windows", "python": "Python"}[name]
                    return
                raise ValueError("The update redirected too many times")
            except (UpdateCancelled, ValueError, DownloadTimeout, ResponseTooLarge):
                raise
            except (FetchError, OSError, urllib.error.URLError, http.client.HTTPException) as error:
                failure = error
                if isinstance(error, FetchError) and not (error.network or error.status == 403):
                    raise
                progress(0)
        raise FetchError("No selected download method could download the release") from failure

    def _native(self, url, path, size, deadline, cancel, progress):
        with path.open("wb") as stream:
            status, _, headers = self.fetcher.winhttp.request(
                url, USER_AGENT, {"Accept": "application/octet-stream", "Accept-Encoding": "identity", "Connection": "close"},
                deadline=deadline, timeout=15, max_bytes=size, body_sink=stream,
                cancel=lambda: self._cancel(cancel), progress=progress)
        if headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
            raise ValueError("Unexpected update download encoding")
        return status, headers

    def _python(self, url, path, size, deadline, cancel, progress):
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=_ssl_context()))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream",
                                                       "Accept-Encoding": "identity"})
        try:
            response = opener.open(request, timeout=min(15, max(.01, deadline - time.monotonic())))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers
        with response, path.open("wb") as stream:
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise ValueError("Unexpected update download encoding")
            length = response.headers.get("Content-Length")
            if length and int(length) > size:
                raise ValueError("Update download exceeds its size limit")
            total = 0
            while True:
                self._cancel(cancel)
                if time.monotonic() >= deadline:
                    raise DownloadTimeout("The update download took too long")
                chunk = response.read1(min(65536, size - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > size:
                    raise ValueError("Update download exceeds its size limit")
                stream.write(chunk)
                progress(total)
            return response.status, response.headers

    def _windows(self, url, path, size, deadline, cancel, progress):
        encoded = base64.b64encode(_PS_DOWNLOAD.encode("utf-16-le")).decode("ascii")
        proc = subprocess.Popen([_powershell_exe(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lines = queue.Queue()
        def receive():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        try:
            proc.stdin.write((json.dumps({"url": url, "path": str(path.resolve()), "ua": USER_AGENT, "max": size}) + "\n").encode("ascii"))
            proc.stdin.close()
            while True:
                self._cancel(cancel)
                if time.monotonic() >= deadline:
                    raise DownloadTimeout("The update download took too long")
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    raise FetchError("Windows download process stopped", network=True)
                result = json.loads(line.decode("utf-8-sig"))
                if "progress" in result:
                    progress(result["progress"])
                elif "error" in result:
                    if result.get("fatal"):
                        raise ValueError("The update exceeded its size limit or used an unexpected encoding")
                    raise FetchError(result["error"], network=True)
                else:
                    return result["status"], result["headers"]
        finally:
            if proc.poll() is None:
                proc.kill()  # Only this owned, single-hop network helper.
            proc.wait()
            reader.join(timeout=2)
            proc.stdout.close()
