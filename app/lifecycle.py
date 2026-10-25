"""Window lifetime without assuming that browser timers keep running."""
import re
import threading
import time

STARTUP_GRACE = 120
CLOSE_GRACE = 25


def window_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("A valid window ID is required")
    return value


class WindowTracker:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._started = self._clock()
        self._seen_window = False
        self._open = set()
        self._closed = set()
        self._closed_at = None
        self._quit = False
        self._updating = False

    def hold_update(self, active):
        with self._lock:
            self._updating = bool(active)

    def reset_startup(self):
        with self._lock:
            self._started = self._clock()

    def open_ids(self):
        with self._lock:
            return sorted(self._open)

    def inherit(self, identifiers):
        """Transfer known-open documents across an authenticated update restart."""
        identifiers = {window_id(value) for value in identifiers}
        with self._lock:
            self._open.update(identifiers - self._closed)
            if self._open:
                self._seen_window = True
                self._closed_at = None

    def heartbeat(self, identifier):
        identifier = window_id(identifier)
        with self._lock:
            # A heartbeat already in flight can arrive after the close beacon.
            # A new document gets a new ID; never revive a document that closed.
            if identifier in self._closed:
                return
            self._open.add(identifier)
            self._seen_window = True
            self._closed_at = None

    def bye(self, identifier):
        identifier = window_id(identifier)
        with self._lock:
            self._closed.add(identifier)
            if identifier not in self._open:
                return
            self._open.remove(identifier)
            if not self._open:
                self._closed_at = self._clock()

    def request_quit(self):
        with self._lock:
            self._quit = True

    def shutdown_reason(self, stay=False):
        with self._lock:
            if self._quit:
                return "Quit requested"
            if stay or self._open or self._updating:
                return None
            now = self._clock()
            if self._closed_at is not None and now - self._closed_at >= CLOSE_GRACE:
                return "All registered windows closed"
            if not self._seen_window and now - self._started >= STARTUP_GRACE:
                return "First window did not connect"
            # No heartbeat does not prove a window closed: its timers may be
            # frozen, the machine may have slept, or the close beacon was lost.
            return None
