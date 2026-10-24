import json
import tempfile
import time
import unittest
from pathlib import Path

import _path  # noqa: F401

import config
import updates
from fetcher import FetchError, Response


class FakeSettings:
    def __init__(self, **values):
        self.values = {"check_updates": True, **values}

    def get(self, key):
        return self.values[key]


class FakeFetcher:
    def __init__(self, outcome):
        self.outcome = outcome
        self.urls = []

    def get(self, url, etag=None, last_modified=None, accept=None, block_local=False):
        self.urls.append(url)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        body = self.outcome if isinstance(self.outcome, bytes) else json.dumps(self.outcome).encode()
        return Response(200, url, body, "application/json", None, None, "python")


def release(tag, url=None, name="Release"):
    return {"tag_name": tag, "name": name, "published_at": "2026-11-01T10:00:00Z",
            "html_url": url or f"https://github.com/{config.GITHUB_REPO}/releases/tag/{tag}"}


class UpdateCheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def checker(self, outcome, **settings):
        return updates.UpdateChecker(FakeFetcher(outcome), FakeSettings(**settings), self.data)

    def test_parse_version(self):
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates.parse_version("2.0"), (2, 0, 0))
        for bad in ("latest", "v1", "1.2.3-beta", "", None):
            self.assertIsNone(updates.parse_version(bad), bad)
        self.assertGreater(updates.parse_version("1.10.0"), updates.parse_version("1.9.9"))

    def test_newer_release_is_reported(self):
        c = self.checker(release("v99.0.0", name="<b>Big</b> update"))
        status = c.check()
        self.assertTrue(status["available"])
        self.assertEqual(status["latest"]["version"], "99.0.0")
        self.assertEqual(status["latest"]["name"], "Big update")
        self.assertEqual(status["latest"]["url"], f"https://github.com/{config.GITHUB_REPO}/releases/tag/v99.0.0")
        self.assertEqual(c.fetcher.urls, [updates.API_URL])

    def test_same_or_older_release_is_not_an_update(self):
        for tag in (f"v{config.VERSION}", "v0.0.1"):
            self.assertFalse(self.checker(release(tag)).check()["available"], tag)

    def test_link_outside_the_repo_is_replaced(self):
        status = self.checker(release("v99.0.0", url="https://evil.example/download.exe")).check()
        self.assertEqual(status["latest"]["url"], updates.RELEASES_URL)

    def test_no_release_yet(self):
        status = self.checker(FetchError("HTTP 404: the site was not found", 404)).check()
        self.assertEqual((status["available"], status["latest"], status["error"]), (False, None, None))
        self.assertIsNotNone(status["checked_at"])

    def test_errors_are_reported_not_raised(self):
        self.assertIn("Could not connect", self.checker(FetchError("Could not connect: no route", network=True)).check()["error"])
        self.assertIn("unexpected reply", self.checker(b"<html>rate limited</html>").check()["error"])
        self.assertIn("unexpected reply", self.checker(release("nightly")).check()["error"])

    def test_status_survives_a_restart(self):
        self.checker(release("v99.0.0")).check()
        reloaded = self.checker(FetchError("unused"))
        self.assertTrue(reloaded.status()["available"])

    def test_checks_at_most_daily_and_only_when_enabled(self):
        c = self.checker(release("v99.0.0"))
        self.assertTrue(c.due())
        c.check()
        self.assertFalse(c.due())
        c.state["checked_at"] = time.time() - updates.CHECK_EVERY - 1
        self.assertTrue(c.due())
        self.assertFalse(self.checker(release("v99.0.0"), check_updates=False).due())


if __name__ == "__main__":
    unittest.main()
