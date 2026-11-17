#!/usr/bin/env python3
"""Tests for the orchestration layer: id extraction and ledger resilience.

Run with `python3 scripts/test_sync.py`. No network, no memos instance.
"""

from __future__ import annotations

import http.client
import io
import sys
import unittest
import urllib.error

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sync  # noqa: E402


class IdExtraction(unittest.TestCase):
    """Dedupe may only trust the memo's own subject, never a quoted tweet."""

    def test_leading_author_link_is_the_subject(self):
        text = "[@someone](https://x.com/someone/status/111)\n\n正文\n\n#Twitter #Bookmark"
        self.assertEqual(sync.PROVENANCE_ID_RE.findall(text), [])
        self.assertEqual(sync.LEADING_ID_RE.match(text.split("\n")[0]).group(1), "111")

    def test_provenance_footer_is_the_subject(self):
        text = "### 标题\n\n[@someone](https://x.com/someone)\n\n正文\n\n[原推](https://x.com/someone/status/222) · 存档 2026-11-17"
        self.assertEqual(sync.PROVENANCE_ID_RE.findall(text), ["222"])

    def test_quoted_link_is_not_the_subject(self):
        text = (
            "[@critic](https://x.com/critic/status/333)\n\n"
            "> **@author** [?](https://x.com/author/status/444)\n"
            "> 引用正文\n\n#Twitter #Bookmark"
        )
        self.assertEqual(sync.PROVENANCE_ID_RE.findall(text), [])
        self.assertEqual(sync.LEADING_ID_RE.match(text.split("\n")[0]).group(1), "333")


class LedgerResilience(unittest.TestCase):
    """A flaky ledger must never turn a published memo into a reported failure."""

    def ledger(self, strict: bool = False) -> sync.Ledger:
        return sync.Ledger("https://ledger.test", "token", "x-bookmark", strict=strict)

    def test_dropped_connection_degrades_to_empty(self):
        original = sync.urllib.request.urlopen
        sync.urllib.request.urlopen = self._drop
        try:
            self.assertEqual(self.ledger().existing(), [])
        finally:
            sync.urllib.request.urlopen = original

    def test_dropped_connection_is_fatal_in_strict_mode(self):
        original = sync.urllib.request.urlopen
        sync.urllib.request.urlopen = self._drop
        try:
            with self.assertRaises(SystemExit):
                self.ledger(strict=True).existing()
        finally:
            sync.urllib.request.urlopen = original

    def test_client_error_is_not_retried(self):
        original = sync.urllib.request.urlopen
        calls = []

        def unauthorized(request, timeout=30):
            calls.append(1)
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad token"}'))

        sync.urllib.request.urlopen = unauthorized
        try:
            self.assertEqual(self.ledger().existing(), [])
        finally:
            sync.urllib.request.urlopen = original
        self.assertEqual(len(calls), 1, "a bad token will not fix itself on retry")

    @staticmethod
    def _drop(request, timeout=30):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")


if __name__ == "__main__":
    unittest.main(verbosity=2)
