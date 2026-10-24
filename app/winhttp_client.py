"""Bounded, streaming Windows WinHTTP requests using only the standard library.

WinHttpRequest COM uses this same Windows HTTP stack, but buffers ResponseBody
before returning it. Calling winhttp.dll directly lets us enforce the byte cap
while reading. Redirect decisions and decompression belong to fetcher.py.
"""
import ctypes
import math
import os
import time
from ctypes import wintypes
from email.parser import Parser
from http.client import HTTPMessage
from urllib.parse import urlsplit


class DownloadTimeout(TimeoutError):
    pass


class ResponseTooLarge(ValueError):
    pass


class WinHttpClient:
    def __init__(self):
        self._dll = None
        if os.name == "nt":
            try:
                self._dll = ctypes.WinDLL("winhttp.dll", use_last_error=True)
                self._bind()
            except (OSError, AttributeError):
                self._dll = None

    @property
    def available(self):
        return self._dll is not None

    def _bind(self):
        handle, ptr = ctypes.c_void_p, ctypes.c_void_p
        dword, text = wintypes.DWORD, wintypes.LPCWSTR
        signatures = {
            "WinHttpOpen": (handle, [text, dword, text, text, dword]),
            "WinHttpConnect": (handle, [handle, text, wintypes.WORD, dword]),
            "WinHttpOpenRequest": (handle, [handle, text, text, text, text, ptr, dword]),
            "WinHttpSetTimeouts": (wintypes.BOOL, [handle] + [ctypes.c_int] * 4),
            "WinHttpSetOption": (wintypes.BOOL, [handle, dword, ptr, dword]),
            "WinHttpSendRequest": (wintypes.BOOL, [handle, text, dword, ptr, dword, dword, ctypes.c_size_t]),
            "WinHttpReceiveResponse": (wintypes.BOOL, [handle, ptr]),
            "WinHttpQueryHeaders": (wintypes.BOOL, [handle, dword, text, ptr, ctypes.POINTER(dword), ptr]),
            "WinHttpQueryDataAvailable": (wintypes.BOOL, [handle, ctypes.POINTER(dword)]),
            "WinHttpReadData": (wintypes.BOOL, [handle, ptr, dword, ctypes.POINTER(dword)]),
            "WinHttpCloseHandle": (wintypes.BOOL, [handle]),
        }
        for name, (result, args) in signatures.items():
            func = getattr(self._dll, name)
            func.restype, func.argtypes = result, args

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def _timeouts(self, handle, deadline, timeout):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DownloadTimeout()
        milliseconds = max(1, math.ceil(min(timeout, remaining) * 1000))
        self._check(self._dll.WinHttpSetTimeouts(handle, *([milliseconds] * 4)))

    def _headers(self, request):
        status, size = wintypes.DWORD(), wintypes.DWORD(ctypes.sizeof(wintypes.DWORD))
        self._check(self._dll.WinHttpQueryHeaders(
            request, 19 | 0x20000000, None, ctypes.byref(status), ctypes.byref(size), None))
        size = wintypes.DWORD()
        result = self._dll.WinHttpQueryHeaders(request, 22, None, None, ctypes.byref(size), None)
        if not result and ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            self._check(result)
        if size.value > 65536:
            raise ResponseTooLarge("The response headers are too large")
        buffer = ctypes.create_unicode_buffer(max(1, size.value // ctypes.sizeof(ctypes.c_wchar)))
        self._check(self._dll.WinHttpQueryHeaders(request, 22, None, buffer, ctypes.byref(size), None))
        # RAW_HEADERS_CRLF includes the HTTP status line, followed by field lines.
        fields = buffer.value.partition("\r\n")[2]
        return status.value, Parser(_class=HTTPMessage).parsestr(fields)

    def request(self, url, user_agent, headers, *, deadline, timeout, max_bytes):
        """One HTTP(S) hop, no automatic auth/cookies/redirects; raw capped body."""
        if not self.available:
            raise OSError("Windows WinHTTP is not available")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Only http:// and https:// addresses are supported")
        if parts.username is not None or parts.password is not None:
            raise ValueError("Addresses containing login credentials are not supported")
        for key, value in headers.items():
            if any(c in key + str(value) for c in ("\r", "\n", "\0")):
                raise ValueError("Invalid HTTP header")
        header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        handles = []
        try:
            # AUTOMATIC_PROXY uses system/per-user proxy settings on Windows 8.1+.
            # Do not change the machine configuration or fall back to a direct route.
            session = self._check(self._dll.WinHttpOpen(user_agent, 4, None, None, 0))
            handles.append(session)
            self._timeouts(session, deadline, timeout)
            connection = self._check(self._dll.WinHttpConnect(
                session, parts.hostname.encode("idna").decode("ascii"),
                parts.port or (443 if parts.scheme == "https" else 80), 0))
            handles.append(connection)
            request = self._check(self._dll.WinHttpOpenRequest(
                connection, "GET", target, None, None, None,
                0x00800000 if parts.scheme == "https" else 0))
            handles.append(request)
            # Disable cookies, redirects, automatic authentication and keep-alive.
            # In particular, never send default Windows credentials to an origin.
            disabled = wintypes.DWORD(0x1 | 0x2 | 0x4 | 0x8)
            self._check(self._dll.WinHttpSetOption(request, 63, ctypes.byref(disabled), ctypes.sizeof(disabled)))
            # Default certificate/hostname validation remains enabled.
            self._timeouts(request, deadline, timeout)
            header_chars = len(header_text.encode("utf-16-le")) // 2
            self._check(self._dll.WinHttpSendRequest(request, header_text, header_chars, None, 0, 0, 0))
            self._timeouts(request, deadline, timeout)
            self._check(self._dll.WinHttpReceiveResponse(request, None))
            self._timeouts(request, deadline, timeout)
            status, response_headers = self._headers(request)
            # Errors, redirects and 304 need only their headers. Close the handles
            # immediately rather than buffering an untrusted error/redirect body.
            if status >= 300:
                return status, b"", response_headers
            length = response_headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ResponseTooLarge("The response is too large")
            chunks, total = [], 0
            buffer = ctypes.create_string_buffer(65536)
            while True:
                self._timeouts(request, deadline, timeout)
                available = wintypes.DWORD()
                self._check(self._dll.WinHttpQueryDataAvailable(request, ctypes.byref(available)))
                if time.monotonic() >= deadline:
                    raise DownloadTimeout()
                if not available.value:
                    return status, b"".join(chunks), response_headers
                self._timeouts(request, deadline, timeout)
                count = wintypes.DWORD()
                # Read at most one byte beyond the remaining budget, so allocation
                # stays bounded even when the server omits Content-Length.
                # Request only bytes already available: a synchronous ReadData of
                # a full 64 KiB can otherwise wait for a continuously trickling body.
                amount = min(available.value, len(buffer), max_bytes - total + 1)
                self._check(self._dll.WinHttpReadData(request, buffer, amount, ctypes.byref(count)))
                if time.monotonic() >= deadline:
                    raise DownloadTimeout()
                if not count.value:
                    return status, b"".join(chunks), response_headers
                total += count.value
                if total > max_bytes:
                    raise ResponseTooLarge("The response is too large")
                chunks.append(buffer.raw[:count.value])
        except OSError:
            if time.monotonic() >= deadline:
                raise DownloadTimeout() from None
            raise
        finally:
            for handle in reversed(handles):
                self._dll.WinHttpCloseHandle(handle)
