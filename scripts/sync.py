#!/usr/bin/env python3
"""Orchestrate: twitter-cli -> (dedupe) -> render.py -> memos skill -> id-ledger.

Nothing here re-implements the three skills it stitches together:
  * bookmarks come from `twitter bookmarks` (public-clis/twitter-cli)
  * memos are written by the memos skill's own CLI (rogeecn/memos-skill)
  * dedupe state lives in an ID ledger (rogeecn/cf-id-ledger id-ledger-query)

Subcommands
  seed     backfill the ledger from memos already in the instance (run once)
  plan     fetch bookmarks, drop everything already synced, write plan.json
  publish  push planned items to memos and record them in the ledger
  render   print one item's markdown (preview / debugging)

Typical incremental run:  plan -> (agent writes titles+tags in plan.json) -> publish
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render  # noqa: E402  (sibling module)

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROJECT = "x-bookmark"
LEDGER_SELFTEST_PROJECT = "x-bookmark-skill-selftest"
# An id counts as "this memo's tweet" only where the archive puts it: the leading
# author link of the old template, or the `[原推]` footer of the card. Ids inside
# quoted blocks belong to *other* tweets — counting them would wrongly mark a
# bookmarked tweet as synced just because it was once quoted.
LEADING_ID_RE = re.compile(r"^\[[^\]]*\]\(https?://(?:x|twitter)\.com/[^/)]+/status/(\d+)\)")
PROVENANCE_ID_RE = re.compile(r"\[原推\]\(https?://(?:x|twitter)\.com/[^/)]+/status/(\d+)\)")


# ------------------------------------------------------------------ discovery


def find_script(explicit: str | None, candidates: list[str], env_var: str, what: str) -> Path:
    """Locate a dependency's CLI: explicit flag > env var > known install paths."""
    paths = []
    if explicit:
        paths.append(explicit)
    if os.environ.get(env_var):
        paths.append(os.environ[env_var])
    paths.extend(candidates)
    for candidate in paths:
        path = Path(os.path.expanduser(candidate))
        if path.is_file():
            return path
    raise SystemExit(
        f"{what} not found. Install it or pass its path explicitly "
        f"(checked: {', '.join(paths)}; env {env_var})."
    )


def memos_script(explicit: str | None) -> Path:
    return find_script(
        explicit,
        [
            "~/.agents/skills/memos/scripts/memos.py",
            "~/.pi/agent/skills/memos/scripts/memos.py",  # same layout, older pi home
            "~/.claude/skills/memos/scripts/memos.py",
        ],
        "MEMOS_SKILL",
        "memos skill CLI",
    )


def twitter_bin(explicit: str | None) -> str:
    """Binary rather than a file, so PATH lookup is allowed."""
    for candidate in (explicit, os.environ.get("TWITTER_CLI"), "twitter"):
        if not candidate:
            continue
        if "/" in candidate:
            if Path(os.path.expanduser(candidate)).is_file():
                return candidate
            continue
        from shutil import which

        resolved = which(candidate)
        if resolved:
            return resolved
    raise SystemExit("twitter-cli not found. Install it or set TWITTER_CLI=/path/to/twitter.")


# --------------------------------------------------------------------- runner


def run_json(cmd: list[str], stdin: str | None = None, timeout: int = 180) -> dict:
    proc = subprocess.run(
        cmd,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"expected JSON from {' '.join(cmd)}: {exc}\n{proc.stdout[:400]}")


# ---------------------------------------------------------------- memos access


def memos_list(memos: Path, limit: int, tag: str | None = None) -> list[dict]:
    cmd = [sys.executable, str(memos), "list", "-n", str(limit), "--json"]
    if tag:
        cmd += ["--tag", tag]
    data = run_json(cmd)
    items = data.get("memos") or data.get("items") or data.get("data") or []
    return [item for item in items if isinstance(item, dict)]


def memo_text(memo: dict) -> str:
    """Accept either the raw memos API shape or the memos skill's flattened shape."""
    for key in ("content", "body", "text", "markdown"):
        value = memo.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def memo_tweet_ids(memos: Path, scan_limit: int, tag: str) -> set[str]:
    """Every tweet id referenced by an existing memo — the second dedupe source.

    The ledger is authoritative once seeded, but memos is the ground truth that
    survives a lost/wiped ledger, so both are consulted.
    """
    ids: set[str] = set()
    for memo in memos_list(memos, scan_limit, tag):
        text = memo_text(memo).lstrip()
        if not text:
            continue
        ids.update(PROVENANCE_ID_RE.findall(text))
        first_line = text.split("\n", 1)[0]
        leading = LEADING_ID_RE.match(first_line)
        if leading:
            ids.add(leading.group(1))
    return ids


def memos_publish(memos: Path, markdown: str, tags: list[str], visibility: str | None) -> dict:
    cmd = [sys.executable, str(memos), "publish", "--file", "-", "--json"]
    for tag in tags:
        cmd += ["--tag", tag]
    if visibility:
        cmd += ["--visibility", visibility]
    return run_json(cmd, stdin=markdown)


# ---------------------------------------------------------------------- ledger

LEDGER_UA = "x-bookmark-sync/1.0"  # Cloudflare answers 1010 to urllib's default UA


class LedgerError(Exception):
    """Ledger read/write failed; advisory (degrade) unless the call site is strict."""


class Ledger:
    """Thin id-ledger-query client: ids-in, ids-out, first-write-wins timestamps.

    Non-strict by default: if the ledger is down, sync continues on the memos
    scan alone (it is the ground truth anyway) instead of blocking the run.
    `seed` flips to strict, because a silent no-op there would look like success.
    """

    def __init__(self, base_url: str | None, token: str | None, project: str, strict: bool = False):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.project = project
        self.strict = strict

    def degrade(self, action: str, exc: Exception) -> None:
        if self.strict:
            raise SystemExit(str(exc))
        print(f"warn: ledger {action} failed, continuing without it ({exc})", file=sys.stderr)

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    def project_url(self, suffix: str = "") -> str:
        return f"{self.base}/v1/projects/{urllib.parse.quote(self.project, safe='')}/ids{suffix}"

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        """Cloudflare closes idle keep-alive sockets, so transient failures are retried."""
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", LEDGER_UA)
        if body:
            request.add_header("Content-Type", "application/json")

        failure = None
        for attempt in range(3):
            if attempt:
                time.sleep(attempt)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                failure = LedgerError(f"ledger {method} {url} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
                if exc.code < 500:
                    raise failure  # a bad key or token will not fix itself
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                failure = LedgerError(f"ledger unreachable: {getattr(exc, 'reason', exc)}")
        raise failure

    def add(self, ids: list[str]) -> dict:
        if not self.enabled or not ids:
            return {"received": 0, "inserted": 0}
        try:
            return self._request("POST", self.project_url(), {"ids": ids})
        except LedgerError as exc:
            self.degrade("write", exc)
            return {"received": len(ids), "inserted": 0, "error": str(exc)}

    def existing(self, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
        """Page through the whole project; the API caps limit at 1000."""
        if not self.enabled:
            return []
        try:
            return self._all(since, until)
        except LedgerError as exc:
            self.degrade("read", exc)
            return []

    def _all(self, since: datetime | None, until: datetime | None) -> list[dict]:
        items: list[dict] = []
        cursor = None
        # The API treats `since` as required; the epoch asks for everything.
        since = since or datetime(1970, 1, 1, tzinfo=timezone.utc)
        while True:
            query = {"limit": "1000"}
            if since:
                query["since"] = since.isoformat().replace("+00:00", "Z")
            if until:
                query["until"] = until.isoformat().replace("+00:00", "Z")
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", self.project_url("?" + urllib.parse.urlencode(query)))
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return items


def ledger_from_env(args) -> Ledger:
    return Ledger(
        args.ledger_url or os.environ.get("ID_LEDGER_URL"),
        args.ledger_token or os.environ.get("ID_LEDGER_TOKEN"),
        args.ledger_project,
        strict=bool(getattr(args, "strict_ledger", False)),
    )


# ------------------------------------------------------------------ bookmarks


def fetch_bookmarks(twitter: str, count: int) -> list[dict]:
    data = run_json([twitter, "bookmarks", "-n", str(count), "--json"], timeout=300)
    tweets = data.get("data") or data.get("items") or []
    return [tweet for tweet in tweets if tweet.get("id")]


def article_markdown(twitter: str, tweet_id: str) -> str | None:
    """Only tweets carrying an articleTitle have one; failures stay non-fatal."""
    try:
        proc = subprocess.run(
            [twitter, "article", str(tweet_id), "-m"],
            text=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    body = proc.stdout.strip()
    return body or None


# ------------------------------------------------------------------ commands


def cmd_seed(args) -> int:
    """Backfill the ledger from memos that already exist, so the first run is incremental.

    Every historical memo embeds its tweet's canonical /status/<id> URL, so the
    instance itself is a complete record of what has been synced.
    """
    memos = memos_script(args.memos)
    ledger = ledger_from_env(args)
    ledger.strict = True  # a seed that silently writes nothing is worse than a failure
    if not ledger.enabled:
        raise SystemExit("ledger not configured: set ID_LEDGER_URL and ID_LEDGER_TOKEN.")

    ids = sorted(memo_tweet_ids(memos, args.scan_limit, args.tag))
    if not ids:
        print(json.dumps({"ok": True, "found": 0, "inserted": 0, "note": "no ids found in memos"}, ensure_ascii=False))
        return 0

    inserted = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        result = ledger.add(chunk)
        inserted += int(result.get("inserted") or 0)
        print(f"  backfilled {min(start + 500, len(ids))}/{len(ids)}", file=sys.stderr)

    print(
        json.dumps(
            {"ok": True, "project": ledger.project, "found": len(ids), "inserted": inserted},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_plan(args) -> int:
    memos = memos_script(args.memos)
    ledger = ledger_from_env(args)
    twitter = twitter_bin(args.twitter)

    tweets = fetch_bookmarks(twitter, args.count)
    synced_ledger = {item.get("id") for item in ledger.existing() if item.get("id")}
    synced_memos = memo_tweet_ids(memos, args.scan_limit, args.tag)
    known = synced_ledger | synced_memos

    items = []
    for tweet in tweets:
        if str(tweet["id"]) in known:
            continue
        headline, _body = render.split_lead(render.squeeze(tweet.get("text") or ""))
        items.append(
            {
                "id": str(tweet["id"]),
                "tweet": tweet,
                "title": (tweet.get("articleTitle") or "").strip() or headline,
                "tags": [],
                "suggested_tags": render.suggest_tags(tweet),
                "has_article": bool(tweet.get("articleTitle")),
                "status": "new",
            }
        )

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": ledger.project if ledger.enabled else None,
        "tag": args.tag,
        "ledger": "ok" if ledger.enabled and synced_ledger else ("empty" if ledger.enabled else "disabled"),
        "counts": {
            "bookmarks": len(tweets),
            "synced_ledger": len(synced_ledger),
            "synced_memos": len(synced_memos),
            "new": len(items),
        },
        "items": items,
    }
    out = Path(args.out)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "plan": str(out), **plan["counts"]}, ensure_ascii=False))
    return 0


def cmd_publish(args) -> int:
    memos = memos_script(args.memos)
    ledger = ledger_from_env(args)
    twitter = twitter_bin(args.twitter) if args.articles else None

    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    saved_at = datetime.now(timezone.utc)

    # Re-check the ledger once, so re-running an old plan cannot double-publish.
    known = {item.get("id") for item in ledger.existing() if item.get("id")}
    for item in plan.get("items", []):
        if item.get("status", "new") == "new" and str(item["id"]) in known:
            item["status"] = "skip"
            item["skip_reason"] = "already in ledger"

    published, skipped, failed = [], [], []
    pending = [item for item in plan.get("items", []) if item.get("status", "new") == "new"]
    if args.limit:
        pending = pending[: args.limit]

    for index, item in enumerate(pending, 1):
        tweet_id = str(item["id"])
        try:
            article = None
            if twitter and item.get("has_article"):
                article = article_markdown(twitter, tweet_id)
            markdown = render.render_tweet(
                item["tweet"],
                title=item.get("title") or None,
                tags=item.get("tags") or [],
                article=article,
                saved_at=saved_at,
                auto_tags=args.auto_tags,
            )
            if args.dry_run:
                published.append({"id": tweet_id, "dry_run": True})
                print(markdown)
                print("\n" + "-" * 60, file=sys.stderr)
                continue
            result = memos_publish(memos, markdown, args.tag_list, args.visibility)
            if not result.get("ok", True):
                raise SystemExit(str(result.get("error") or result))
            item["status"] = "published"
            item["memo_url"] = result.get("url") or result.get("id")
            published.append({"id": tweet_id, "url": item["memo_url"], "chars": result.get("chars") or len(markdown)})
            print(f"[{index}/{len(pending)}] published {tweet_id}", file=sys.stderr)
            try:
                ledger.add([tweet_id])
            except (SystemExit, LedgerError) as exc:
                # The memo exists; the memos scan still covers dedupe, so this is not a failure.
                print(f"[{index}/{len(pending)}] warn: ledger write skipped for {tweet_id} ({exc})", file=sys.stderr)
        except SystemExit as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
            failed.append({"id": tweet_id, "error": str(exc)})
            print(f"[{index}/{len(pending)}] FAILED {tweet_id}: {exc}", file=sys.stderr)
        except Exception as exc:  # keep the loop alive; one bad tweet must not stop sync
            item["status"] = "failed"
            item["error"] = f"{type(exc).__name__}: {exc}"
            failed.append({"id": tweet_id, "error": item["error"]})
            print(f"[{index}/{len(pending)}] FAILED {tweet_id}: {exc}", file=sys.stderr)
        time.sleep(args.delay)

    skipped = [
        {"id": item["id"], "reason": item.get("skip_reason") or "marked skip"}
        for item in plan.get("items", [])
        if item.get("status") == "skip"
    ]
    if not args.dry_run:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    report = {
        "ok": not failed,
        "dry_run": bool(args.dry_run),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "visibility": args.visibility or "instance default",
        "published": published,
        "failed": failed,
        "skipped": skipped,
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"ok": report["ok"], "published": len(published), "failed": len(failed), "skipped": len(skipped)},
            ensure_ascii=False,
        )
    )
    return 0 if report["ok"] else 1


def cmd_render(args) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    wanted = set(args.id or [])
    for item in plan.get("items", []):
        if wanted and str(item["id"]) not in wanted:
            continue
        article = None
        if args.articles:
            article = article_markdown(twitter_bin(args.twitter), str(item["id"]))
        print(
            render.render_tweet(
                item["tweet"],
                title=item.get("title") or None,
                tags=item.get("tags") or [],
                article=article,
                auto_tags=args.auto_tags,
            )
        )
        print("\n" + "-" * 60 + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--memos", help="path to memos skill's memos.py")
    parser.add_argument("--twitter", help="path to twitter-cli binary")
    parser.add_argument("--ledger-url", help="id-ledger base URL (default: $ID_LEDGER_URL)")
    parser.add_argument("--ledger-token", help="id-ledger token (default: $ID_LEDGER_TOKEN)")
    parser.add_argument("--ledger-project", default=DEFAULT_PROJECT, help=f"ledger project key (default: {DEFAULT_PROJECT})")
    parser.add_argument("--tag", default="Bookmark", help="tag used to find existing sync memos (default: Bookmark)")
    parser.add_argument("--strict-ledger", action="store_true", help="fail instead of degrading when the ledger is unreachable")
    parser.add_argument("--scan-limit", type=int, default=2000, help="how many existing memos to scan for ids")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="backfill the ledger from memos already in the instance")
    seed.set_defaults(func=cmd_seed)

    plan = sub.add_parser("plan", help="fetch bookmarks and write a plan of unsynced ones")
    plan.add_argument("--count", type=int, default=60, help="how many bookmarks to fetch (default: 60)")
    plan.add_argument("--out", default="plan.json", help="plan output path (default: plan.json)")
    plan.set_defaults(func=cmd_plan)

    publish = sub.add_parser("publish", help="publish planned items and record them in the ledger")
    publish.add_argument("--plan", default="plan.json")
    publish.add_argument("--tag-list", nargs="*", default=[], help="extra tags for every memo")
    publish.add_argument("--visibility", choices=["private", "protected", "public"], help="default: instance default")
    publish.add_argument("--auto-tags", action="store_true", help="append suggested topic tags")
    publish.add_argument("--articles", action="store_true", help="fetch full article text for article tweets")
    publish.add_argument("--limit", type=int, help="publish at most N items this run")
    publish.add_argument("--delay", type=float, default=0.3, help="seconds between publishes (default: 0.3)")
    publish.add_argument("--dry-run", action="store_true", help="print the markdown instead of publishing")
    publish.add_argument("--report", default="sync-report.json", help="report path")
    publish.set_defaults(func=cmd_publish)

    preview = sub.add_parser("render", help="print planned items' markdown")
    preview.add_argument("--plan", default="plan.json")
    preview.add_argument("--id", action="append", help="only this tweet id (repeatable)")
    preview.add_argument("--articles", action="store_true")
    preview.add_argument("--auto-tags", action="store_true")
    preview.set_defaults(func=cmd_render)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
