"""Open windows survive pauses; only confirmed closes or Quit end the session."""
import unittest

import _path  # noqa: F401

from lifecycle import WindowTracker


class WindowLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.windows = WindowTracker(clock=lambda: self.now)

    def test_open_window_survives_hours_without_browser_timers(self):
        self.windows.heartbeat("background-window")
        self.now += 24 * 60 * 60
        self.assertIsNone(self.windows.shutdown_reason())
        self.windows.heartbeat("background-window")  # resumes without restarting
        self.assertIsNone(self.windows.shutdown_reason())

    def test_update_lease_holds_startup_and_close_but_explicit_quit_wins(self):
        self.windows.hold_update(True)
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason())
        self.windows.heartbeat("updating")
        self.windows.bye("updating")
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason())
        self.windows.request_quit()
        self.assertEqual(self.windows.shutdown_reason(), "Quit requested")

    def test_closing_one_window_does_not_stop_a_paused_second_window(self):
        self.windows.heartbeat("one")
        self.windows.heartbeat("two")
        self.windows.bye("one")
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason())
        self.windows.bye("two")
        self.now += 24
        self.assertIsNone(self.windows.shutdown_reason())
        self.now += 1
        self.assertEqual(self.windows.shutdown_reason(), "All registered windows closed")

    def test_update_inherits_all_paused_documents_and_preserves_close_tracking(self):
        self.windows.heartbeat("one")
        self.windows.heartbeat("two")
        replacement = WindowTracker(clock=lambda: self.now)
        replacement.inherit(self.windows.open_ids())
        self.now += 24 * 3600  # Every browser can remain suspended beyond startup grace.
        self.assertIsNone(replacement.shutdown_reason())
        replacement.heartbeat("one")
        replacement.bye("one")
        self.now += 3600
        self.assertIsNone(replacement.shutdown_reason())
        replacement.heartbeat("two")
        replacement.bye("two")
        self.now += 25
        self.assertEqual(replacement.shutdown_reason(), "All registered windows closed")
        replacement.heartbeat("two")
        self.assertEqual(replacement.shutdown_reason(), "All registered windows closed")

    def test_reload_or_new_window_during_close_grace_keeps_server(self):
        self.windows.heartbeat("old-document")
        self.windows.bye("old-document")
        self.now += 20
        self.windows.heartbeat("new-document")
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason())

    def test_late_heartbeat_does_not_revive_a_closed_document(self):
        self.windows.heartbeat("one")
        self.windows.bye("one")
        self.windows.heartbeat("one")
        self.now += 25
        self.assertEqual(self.windows.shutdown_reason(), "All registered windows closed")

    def test_duplicate_or_unknown_close_does_not_reset_grace(self):
        self.windows.heartbeat("one")
        self.windows.bye("one")
        self.now += 20
        self.windows.bye("one")
        self.windows.bye("unknown")
        self.now += 5
        self.assertEqual(self.windows.shutdown_reason(), "All registered windows closed")

    def test_close_before_first_heartbeat_cannot_register_late(self):
        self.windows.bye("closing-document")
        self.windows.heartbeat("closing-document")
        self.now += 120
        self.assertEqual(self.windows.shutdown_reason(), "First window did not connect")

    def test_failed_first_window_still_times_out(self):
        self.now += 119
        self.assertIsNone(self.windows.shutdown_reason())
        self.now += 1
        self.assertEqual(self.windows.shutdown_reason(), "First window did not connect")

    def test_quit_overrides_open_windows_and_stay_mode(self):
        self.windows.heartbeat("one")
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason(stay=True))
        self.windows.request_quit()
        self.assertEqual(self.windows.shutdown_reason(), "Quit requested")
        self.assertEqual(self.windows.shutdown_reason(stay=True), "Quit requested")

    def test_stay_ignores_startup_and_normal_close(self):
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason(stay=True))
        self.windows.heartbeat("one")
        self.windows.bye("one")
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason(stay=True))

    def test_invalid_window_ids_do_not_change_lifetime(self):
        self.windows.heartbeat("one")
        for value in (None, 7, {}, "", "a" * 65, "with spaces", "bad\r\nid"):
            for method in (self.windows.heartbeat, self.windows.bye):
                with self.assertRaises(ValueError):
                    method(value)
        self.now += 3600
        self.assertIsNone(self.windows.shutdown_reason())
