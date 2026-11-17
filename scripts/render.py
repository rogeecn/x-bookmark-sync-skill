#!/usr/bin/env python3
"""Turn one bookmarked tweet (twitter-cli JSON) into one memos memo.

The layout this produces is specified in references/layout.md. Keep the two in
sync: every block below exists because memos' editor renders it well, and the
spec explains why. This module is pure (no network, no files) so it can be
unit-tested and previewed without touching the instance.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

# t.co shortener: twitter-cli exposes expanded targets in `urls` only sometimes,
# so expansion is best-effort and never rewrites text it cannot map.
TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9]+")
# x.com navigation the stub can resolve to (login walls, onboarding, article shells): not content.
INTERNAL_X_RE = re.compile(r"https?://(?:x|twitter)\.com/(?:i/|login|settings|home)\S*")
# Media is rendered from `media`, so links back to it are dropped from the link list.
MEDIA_HOST_RE = re.compile(r"^https?://(?:pbs|video)\.twimg\.com/", re.I)
# A link to the tweet itself, or to another tweet, is provenance, not a resource.
STATUS_RE = re.compile(r"^https?://(?:x|twitter)\.com/[^/]+/status/\d+")
ATX_RE = re.compile(r"^(#{1,6})(\s)")
# A line that is nothing but hashtags (or nothing but a URL) cannot be a headline:
# it carries no sentence, and lifting it would empty the card's first impression.
HASHTAG_ONLY_LINE_RE = re.compile(r"^#\S+(?:\s+#\S+)*$")
BARE_URL_LINE_RE = re.compile(r"^<?https?://\S+>?$")
SENTENCE_END = "。！？!?…"
# Lines twitter-cli prepends to an article dump; the byline already carries them.
ARTICLE_META_RE = re.compile(r"^- (?:Author|Published|URL|Likes|Retweets|Replies|Bookmarks|Views):", re.I)

DEFAULT_TAGS = ("Twitter", "Bookmark")

# Topic tags mirror how the existing archive is already tagged (#github, #AI,
# #Codex, #skill ...). They are only *suggestions* for the agent to accept; the
# deterministic renderer adds nothing but DEFAULT_TAGS unless asked.
TAG_RULES: tuple[tuple[str, str], ...] = (
    (r"github\.com|github\b", "github"),
    (r"claude|anthropic", "Claude"),
    (r"codex|openai", "Codex"),
    (r"cursor\b", "Cursor"),
    (r"\bmcp\b|model context protocol", "MCP"),
    (r"提示词|prompt", "Prompt"),
    (r"智能体|agent", "Agent"),
    (r"skill|技能包", "skill"),
    (r"微调|fine-?tun|推理模型|\bllm\b|大模型", "LLM"),
    (r"前端|react|vue\b|next\.js|tailwind|\bcss\b", "前端"),
    (r"\brust\b", "Rust"),
    (r"\bpython\b", "Python"),
    (r"\bgo 语言|\bgolang\b", "Go"),
    (r"漏洞|\bcve-|exploit|提权|逆向", "安全"),
    (r"爬虫|scrap|crawler", "爬虫"),
    (r"赚钱|变现|副业|接单", "搞钱"),
    (r"论文|paper|arxiv", "论文"),
    (r"教程|tutorial|指南|保姆级", "教程"),
    (r"设计|design|ui\b|figma", "设计"),
)


# ---------------------------------------------------------------- small helpers


def format_count(value: int | None) -> str:
    """6,369 -> 6.4k, 12345 -> 1.2万. Absent or zero counts are dropped upstream."""
    if not value:
        return "0"
    if value >= 10000:
        return f"{value / 10000:.1f}万".replace(".0万", "万")
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(value)


def squeeze(text: str) -> str:
    """Normalise newlines and collapse runs of blank lines: memos honours every one."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def demote_headings(markdown: str, levels: int = 2) -> str:
    """Push the tweet's own headings below the card title so they cannot outrank it."""

    def repl(match: re.Match[str]) -> str:
        return "#" * min(len(match.group(1)) + levels, 6) + match.group(2)

    return "\n".join(ATX_RE.sub(repl, line) for line in markdown.split("\n"))


def quote_block(markdown: str) -> str:
    lines = []
    for line in markdown.split("\n"):
        lines.append(f"> {line}".rstrip() if line.strip() else ">")
    return "\n".join(lines)


def normalize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#").strip()
    tag = re.sub(r"\s+", "-", tag)
    return re.sub(r"[^\w\-\u4e00-\u9fff]", "", tag)


# --------------------------------------------------------------- mining layers


def expand_links(
    text: str, urls: list[str] | None, links: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    """Swap t.co stubs for the real target, so no link in a memo points at t.co.

    `links` is a resolved stub -> target map (sync.py builds it by following the
    redirect through the proxy). Without it we fall back to pairing the stubs
    with twitter-cli's expanded URLs, but only on an exact 1:1 count: twitter-cli
    reports fewer expansions than the text has stubs more often than not, and a
    wrong pairing would silently point a sentence at the wrong site.
    """
    urls = [u for u in (urls or []) if u]
    known = {stub: target for stub, target in (links or {}).items() if target}
    if not known and urls:
        stubs = TCO_RE.findall(text)
        if len(stubs) == len(urls):
            known = dict(zip(stubs, urls))
    if known:
        text = TCO_RE.sub(lambda match: known.get(match.group(0), match.group(0)), text)
    return text, [u for u in urls if not MEDIA_HOST_RE.match(u)]


def strip_self_links(text: str, tweet_ids: list[str] | tuple[str, ...]) -> str:
    """Drop links that resolve back to the tweet already on the card.

    A stub at the end of a tweet almost always points at that tweet's own video
    or photo; expanding it would append a link to the card itself. The media
    block and the quote block already carry those, so the token goes away.
    """
    def blank(match: re.Match) -> str:
        # `[@who](https://x.com/who/status/123)` is attribution, not a stray self-link
        return match.group(0) if match.string[max(0, match.start() - 2) : match.start()] == "](" else ""

    for tweet_id in tweet_ids:
        if not tweet_id:
            continue
        pattern = re.compile(rf"https?://(?:x|twitter)\.com/[^/\s)]+/status/{tweet_id}[^)\s]*")
        text = pattern.sub(blank, text)
    text = INTERNAL_X_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


def unused_links(text: str, urls: list[str] | None, tweet_id: str) -> list[str]:
    """URLs worth showing as link cards: not media, not this tweet, not already inline."""
    seen: set[str] = set()
    result = []
    for url in urls or []:
        url = (url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if MEDIA_HOST_RE.match(url) or STATUS_RE.match(url):
            continue
        if tweet_id and f"/status/{tweet_id}" in url:
            continue
        if url in text:  # already expanded inline by expand_links
            continue
        result.append(url)
    return result


def rewrite_links(text: str, links: dict[str, str], tweet_ids: list[str] | None = None) -> str:
    """Stub-only repair: swap t.co targets inside an existing memo, layout untouched.

    Used for historical memos whose tweet object is gone — the layout is not
    ours to redo, but a dead short link is still worth fixing.
    """
    known = {stub: target for stub, target in (links or {}).items() if target}
    if known:
        text = TCO_RE.sub(lambda match: known.get(match.group(0), match.group(0)), text)
    return strip_self_links(text, tweet_ids or [])


def split_lead(text: str) -> tuple[str, str]:
    """Return (headline, body), or ("", body) when no honest headline exists.

    Three strategies, in order of how much they read like a title:

    1. The tweet's own short first line (<=70 chars, with a real body behind it).
       Removed from the body so it is not printed twice.
    2. The first sentence, when the tweet is one long paragraph. Anything shorter
       than 12 chars is a punchline, not a title, and is left alone.
    3. Nothing. A one-liner or a bare link deserves no headline; the caller (the
       agent, in plan.json) can write one, and an empty title beats a fake one.
    """
    body = text.strip("\n")
    if not body.strip():
        return "", ""

    # Leading link rows and tag rows are scaffolding, not sentences: the author
    # often opens with them, and the headline lives on the line after.
    lines = body.split("\n")
    head_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip() and not BARE_URL_LINE_RE.match(line.strip()) and not HASHTAG_ONLY_LINE_RE.match(line.strip())
        ),
        None,
    )
    if head_idx is None:  # nothing but links and tags
        return "", body

    candidate = lines[head_idx].strip()

    # 1. short first line
    rest_lines = lines[:head_idx] + lines[head_idx + 1 :]
    rest = "\n".join(rest_lines).strip("\n")
    # The body has to stay substantial, or the "headline" was the whole tweet.
    if len(candidate) <= 70 and len(rest) >= max(20, len(body) * 0.3):
        return candidate, rest

    # 2. first sentence of a long paragraph
    for position, char in enumerate(candidate[:70]):
        if char in SENTENCE_END and position >= 12:
            headline = candidate[: position + 1].rstrip("。，, ")
            tail = candidate[position + 1 :].lstrip()
            rest = "\n".join(lines[:head_idx] + ([tail] if tail else []) + lines[head_idx + 1 :]).strip("\n")
            if len(rest) >= 12:
                return headline, rest
            break

    return "", body


def same_heading(left: str, right: str) -> bool:
    """True when a headline and the text's first line say the same thing.

    Titles often arrive from outside (plan.json, `--title`, articleTitle) in a
    form that differs only by trailing punctuation or truncation, and printing
    both would repeat the sentence at the top of the card.
    """

    def norm(value: str) -> str:
        return re.sub(r"[\s\u3000，。！？、；：,.;:…!?'\"]+", " ", value or "").strip().casefold()

    left_n, right_n = norm(left), norm(right)
    if not left_n or not right_n:
        return False
    if left_n == right_n:
        return True
    if right_n.endswith("…"):
        return left_n.startswith(right_n[:-1].strip())
    if left_n.endswith("…"):
        return right_n.startswith(left_n[:-1].strip())
    return False


def suggest_tags(tweet: dict) -> list[str]:
    """Topic tags the archive already uses, matched against text and outbound links."""
    haystack = " ".join(
        [
            tweet.get("text") or "",
            tweet.get("articleTitle") or "",
            " ".join(tweet.get("urls") or []),
            (tweet.get("quotedTweet") or {}).get("text") or "",
        ]
    ).lower()
    tags: list[str] = []
    for pattern, tag in TAG_RULES:
        if tag in tags or tag in DEFAULT_TAGS:
            continue
        if re.search(pattern, haystack, re.I):
            tags.append(tag)
    return tags[:3]


def metrics_line(tweet: dict) -> str:
    metrics = tweet.get("metrics") or {}
    parts = []
    for key, icon in (
        ("views", "👁"),
        ("bookmarks", "🔖"),
        ("likes", "♥"),
        ("replies", "💬"),
        ("retweets", "🔁"),
        ("quotes", "❝"),
    ):
        value = metrics.get(key) or 0
        if value:
            parts.append(f"{icon}{format_count(value)}")
    return " ".join(parts)


def posted_at(tweet: dict, saved_at: datetime) -> str:
    """Same-year tweets show MM-DD; older ones keep the year, since this is an archive."""
    raw = tweet.get("createdAtISO") or tweet.get("createdAtLocal") or ""
    parsed = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is None:
        return (tweet.get("createdAtLocal") or "").strip()
    if parsed.year == saved_at.year:
        return parsed.strftime("%m-%d %H:%M")
    return parsed.strftime("%Y-%m-%d %H:%M")


def tweet_url(tweet: dict) -> str:
    handle = ((tweet.get("author") or {}).get("screenName") or "i").strip() or "i"
    return f"https://x.com/{handle}/status/{tweet.get('id', '')}"


def media_blocks(tweet: dict) -> list[str]:
    photos, videos = [], []
    for item in tweet.get("media") or []:
        kind = (item.get("type") or "").lower()
        url = item.get("url") or ""
        if not url:
            continue
        if kind == "photo":
            # `?name=large` is the 1200px rendition; the bare path is a thumbnail.
            photos.append(f"![]({url}{'&' if '?' in url else '?'}name=large)")
        else:
            width, height = item.get("width"), item.get("height")
            dims = f" · {width}×{height}" if width and height else ""
            label = "视频" if kind == "video" else kind or "媒体"
            videos.append(f"🎬 [{label}{dims}]({url})")
    return photos + videos


def quoted_block(tweet: dict, links: dict[str, str] | None = None) -> str:
    quoted = tweet.get("quotedTweet") or {}
    text = (quoted.get("text") or "").strip()
    if text:
        text, _ = expand_links(text, quoted.get("urls"), links)
        text = strip_self_links(text, [str(quoted.get("id") or "")]).strip()
    if not text:
        return ""
    if not text:
        return ""
    author = quoted.get("author") or {}
    handle = (author.get("screenName") or "").strip()
    name = (author.get("name") or "").strip()
    attribution = " ".join(part for part in (f"**@{handle}**" if handle else "", name) if part)
    link = f" · [引用](https://x.com/{handle or 'i'}/status/{quoted.get('id', '')})"
    return quote_block(f"{attribution}{link}\n\n{demote_headings(squeeze(text))}")


def article_block(article: str | None, headline: str = "") -> str:
    """Full article text folds away: the card stays scannable, the archive stays complete.

    `twitter article -m` prefixes a metadata bullet list (author/published/counts)
    that the card's byline already states, and repeats the title as an H1 that the
    card title already says. Both are stripped so the fold opens on real prose.
    """
    if not article or not article.strip():
        return ""
    lines = article.strip("\n").split("\n")
    kept_title, index = None, 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
        elif ARTICLE_META_RE.match(line):
            index += 1
        elif line.startswith("# "):
            if not same_heading(line[2:], headline):
                kept_title = line
            index += 1
        else:
            break

    body = squeeze(demote_headings("\n".join(([kept_title] if kept_title else []) + lines[index:])))
    if not body:
        return ""
    return f"<details><summary>📄 全文 · {len(body)} 字</summary>\n\n{body}\n\n</details>"


# ------------------------------------------------------------------- the layout


def render_tweet(
    tweet: dict,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    article: str | None = None,
    saved_at: datetime | None = None,
    auto_tags: bool = False,
    links: dict[str, str] | None = None,
) -> str:
    saved_at = saved_at or datetime.now(timezone.utc)
    author = tweet.get("author") or {}
    handle = (author.get("screenName") or "").strip()
    name = (author.get("name") or "").strip()

    text, leftover = expand_links(tweet.get("text") or "", tweet.get("urls"), links)
    text = squeeze(strip_self_links(text, [str(tweet.get("id") or "")]))
    article_title = (tweet.get("articleTitle") or "").strip()

    lead, rest = split_lead(text)
    if title:
        headline = title.strip()
        body = rest if same_heading(lead, headline) else text
    elif article_title:
        headline = article_title
        body = rest if same_heading(lead, headline) else text
    else:
        headline, body = lead, rest

    blocks: list[str] = []

    # 1. Headline. h3 is 1.125x body in memos: a card title without shouting in a timeline.
    if headline:
        blocks.append(f"### {headline}")

    # 2. Byline: who, when, and how much signal the tweet carried.
    byline = []
    if tweet.get("isRetweet") and tweet.get("retweetedBy"):
        byline.append(f"🔁 @{tweet['retweetedBy']}")
    if handle:
        byline.append(f"[@{handle}](https://x.com/{handle})")
    if name and name.strip().casefold() != handle.casefold():
        byline.append(f"{name} ✓" if author.get("verified") else name)
    byline.append(posted_at(tweet, saved_at))
    metrics = metrics_line(tweet)
    if metrics:
        byline.append(metrics)
    blocks.append(" · ".join(part for part in byline if part))

    # 3. The tweet itself, links expanded when the pairing was trustworthy.
    if body.strip():
        blocks.append(demote_headings(body))

    # 4. What it was reacting to: usually where the substance is.
    quoted = quoted_block(tweet, links)
    if quoted:
        blocks.append(quoted)

    # 5. Media.
    blocks.extend(media_blocks(tweet))

    # 6. Outbound resources as bare links: memos renders each as a link preview card.
    links = unused_links(text, leftover or tweet.get("urls"), str(tweet.get("id") or ""))
    if links:
        blocks.append("**🔗 链接**")
        blocks.extend(links)

    # 7. Long-form article, folded.
    article_md = article_block(article, headline)
    if article_md:
        blocks.append(article_md)

    # 8. Provenance, then tags. Tags last so the tag row stays the visible footer.
    blocks.append(" · ".join([f"[原推]({tweet_url(tweet)})", f"存档 {saved_at.strftime('%Y-%m-%d')}"]))

    tag_names = list(DEFAULT_TAGS)
    for tag in (tags or []) + (suggest_tags(tweet) if auto_tags else []):
        cleaned = normalize_tag(tag)
        if cleaned and cleaned.lower() not in {t.lower() for t in tag_names}:
            tag_names.append(cleaned)
    blocks.append(" ".join(f"#{tag}" for tag in tag_names))

    return squeeze("\n\n".join(block for block in blocks if block and block.strip()))


# ------------------------------------------------------------------------- CLI


def load_tweets(args) -> list[dict]:
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            raw = json.load(handle)
    else:
        raw = json.load(sys.stdin)
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(data, dict):
        data = data.get("items", [data])
    items = list(data)
    if args.id:
        items = [item for item in items if str(item.get("id")) == str(args.id)]
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render bookmarked tweets as memos markdown.")
    parser.add_argument("--file", help="twitter-cli JSON (default: stdin)")
    parser.add_argument("--id", help="render only this tweet id")
    parser.add_argument("--title", help="override the headline")
    parser.add_argument("--tag", action="append", default=[], help="extra topic tag (repeatable)")
    parser.add_argument("--article", help="markdown file of the tweet's article, folded into the card")
    parser.add_argument("--auto-tags", action="store_true", help="append suggested topic tags")
    parser.add_argument("--links", help="JSON file of resolved t.co -> real URL")
    parser.add_argument("--json", action="store_true", help="emit [{id, markdown}] instead of raw markdown")
    args = parser.parse_args(argv)

    article = None
    if args.article:
        with open(args.article, encoding="utf-8") as handle:
            article = handle.read()

    links = None
    if args.links:
        with open(args.links, encoding="utf-8") as handle:
            links = json.load(handle)

    tweets = load_tweets(args)
    if not tweets:
        print("no matching tweet", file=sys.stderr)
        return 1

    rendered = [
        {
            "id": tweet.get("id"),
            "markdown": render_tweet(
                tweet,
                title=args.title,
                tags=args.tag,
                article=article,
                auto_tags=args.auto_tags,
                links=links,
            ),
        }
        for tweet in tweets
    ]
    if args.json:
        json.dump(rendered, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print("\n\n".join(item["markdown"] for item in rendered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
