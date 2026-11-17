#!/usr/bin/env python3
"""Layout regression tests: pure functions, no network, no instance.

Run: python3 scripts/test_render.py
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render  # noqa: E402

SAVED_AT = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def tweet(**overrides) -> dict:
    base = {
        "id": "2122286309855338953",
        "text": "这个动画库可以啊，直接抄了\n\n线条动画 + 弹簧物理，React 直接用\nhttps://t.co/abc123\n\n#前端",
        "author": {"id": "1", "name": "Kokonut", "screenName": "kokonutlabs", "verified": False},
        "metrics": {"likes": 113, "retweets": 13, "replies": 8, "quotes": 1, "views": 6369, "bookmarks": 186},
        "createdAtISO": "2026-11-16T11:36:00.000Z",
        "media": [],
        "urls": ["https://github.com/kokonut-labs/kokonutui"],
        "lang": "zh",
    }
    base.update(overrides)
    return base


class Counts(unittest.TestCase):
    def test_compact_counts(self):
        self.assertEqual(render.format_count(999), "999")
        self.assertEqual(render.format_count(6369), "6.4k")
        self.assertEqual(render.format_count(12345), "1.2万")
        self.assertEqual(render.format_count(20000), "2万")
        self.assertEqual(render.format_count(None), "0")


class Headline(unittest.TestCase):
    def test_short_first_line_is_lifted_out_of_body(self):
        head, body = render.split_lead("这个动画库可以啊，直接抄了\n\n线条动画 + 弹簧物理，React 直接用，还能接 GSAP 时间线做编排")
        self.assertEqual(head, "这个动画库可以啊，直接抄了")
        self.assertNotIn("直接抄了", body, "lifted headline must not stay in the body")
        self.assertIn("弹簧物理", body)

    def test_long_paragraph_splits_at_first_sentence(self):
        text = "先把这个观点说清楚，因为后面全是论证。然后是一大堆补充说明，长度足够长了，长到可以撑起正文。"
        head, body = render.split_lead(text)
        self.assertEqual(head, "先把这个观点说清楚，因为后面全是论证")
        self.assertTrue(body.startswith("然后是一大堆补充说明"))

    def test_no_honest_headline_leaves_title_empty(self):
        text = "这是一个很长的单段推文，没有任何句末标点就一路写下去写到超出六十个字符还在继续写着呢"
        head, body = render.split_lead(text)
        self.assertEqual(head, "")
        self.assertEqual(body, text)

    def test_one_liner_gets_no_headline(self):
        self.assertEqual(render.split_lead("卧槽！巨漂亮"), ("", "卧槽！巨漂亮"))

    def test_leading_bare_url_line_is_skipped(self):
        head, body = render.split_lead(
            "https://t.co/zzz\n\n各种旋钮开关和推子\n\n还有一个 midi 调音台，打开声音感受一下，真想用这一套做一个项目啊"
        )
        self.assertEqual(head, "各种旋钮开关和推子")
        self.assertIn("t.co/zzz", body, "the media link must survive in the body")

    def test_hashtag_only_line_is_not_a_headline(self):
        head, body = render.split_lead("#前端 #AI\n\n正文内容在这里，长度足够长了，用来验证标签行不会被当成标题")
        self.assertEqual(head, "")
        self.assertIn("#前端 #AI", body, "a hashtag row stays where it was")

    def test_leading_hashtag_row_is_skipped_for_the_next_line(self):
        head, body = render.split_lead("#Codex \n最近熬了几个通宵，记录一点 Codex 的使用心得\n\n1. 关于子智能体的灵活使用，还有很多细节要展开说明")
        self.assertEqual(head, "最近熬了几个通宵，记录一点 Codex 的使用心得")
        self.assertIn("#Codex", body)

    def test_prose_line_starting_with_hashtag_is_a_headline(self):
        head, _ = render.split_lead("#每日推荐 抖音全自动评论管理神器开源\n\n正文在这里，长度足够长了，用来验证这一行可以当标题")
        self.assertEqual(head, "#每日推荐 抖音全自动评论管理神器开源")

    def test_short_rest_is_not_split(self):
        head, body = render.split_lead("标题行\n短")
        self.assertIn("标题行", body)



class Links(unittest.TestCase):
    def test_expands_when_stubs_and_urls_match_one_to_one(self):
        text, _ = render.expand_links("看这个 https://t.co/aaa 和 https://t.co/bbb", ["https://a.dev", "https://b.dev"])
        self.assertEqual(text, "看这个 https://a.dev 和 https://b.dev")

    def test_keeps_stubs_on_mismatch(self):
        text, links = render.expand_links("https://t.co/aaa https://t.co/bbb", ["https://only-one.dev"])
        self.assertIn("t.co/aaa", text)
        self.assertIn("https://only-one.dev", links)

    def test_resolved_stub_map_beats_the_counts(self):
        """10 stubs vs 5 urls is the common case; the 301 map settles it without guessing."""
        stubs = " ".join(f"https://t.co/s{i}" for i in range(10))
        mapping = {f"https://t.co/s{i}": f"https://site{i}.dev" for i in range(10)}
        text, _ = render.expand_links(stubs, ["https://only-five.dev"] * 5, mapping)
        self.assertNotIn("t.co/", text)
        self.assertIn("https://site9.dev", text)

    def test_self_link_is_dropped_not_expanded(self):
        text = "看了这个视频 https://x.com/who/status/999/video/1"
        self.assertEqual(render.strip_self_links(text, ["999"]), "看了这个视频")

    def test_author_link_survives_its_closing_paren(self):
        text = "[@who](https://x.com/who/status/999)\n\n正文"
        self.assertEqual(render.strip_self_links(text, ["999"]), text)

    def test_internal_x_navigation_is_dropped(self):
        text = "在 https://x.com/i/jf/onboarding/web?mode=login 看"
        self.assertEqual(render.strip_self_links(text, []), "在  看")

    def test_rewrite_links_leaves_the_layout_alone(self):
        text = "[@who](https://x.com/who/status/999)\n\n正文 https://t.co/zzz"
        out = render.rewrite_links(text, {"https://t.co/zzz": "https://real.dev"})
        self.assertEqual(out, "[@who](https://x.com/who/status/999)\n\n正文 https://real.dev")

    def test_resolved_stub_is_rendered_inline(self):
        card = render.render_tweet(
            tweet(text="看这个\nhttps://t.co/abc123", urls=[], media=[]),
            links={"https://t.co/abc123": "https://github.com/kokonut-labs/kokonutui"},
            saved_at=SAVED_AT,
        )
        self.assertIn("https://github.com/kokonut-labs/kokonutui", card)
        self.assertNotIn("t.co/", card)

    def test_unused_links_drops_media_status_and_inline(self):
        links = render.unused_links(
            "正文已含 https://inline.dev",
            [
                "https://inline.dev",
                "https://pbs.twimg.com/media/x.jpg",
                "https://x.com/foo/status/2122286309855338953",
                "https://github.com/a/b",
                "https://github.com/a/b",
            ],
            "2122286309855338953",
        )
        self.assertEqual(links, ["https://github.com/a/b"])


class Media(unittest.TestCase):
    def test_photo_uses_large_rendition(self):
        blocks = render.media_blocks(tweet(media=[{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "width": 1200, "height": 800}]))
        self.assertEqual(blocks, ["![](https://pbs.twimg.com/media/a.jpg?name=large)"])

    def test_video_becomes_labelled_link(self):
        blocks = render.media_blocks(tweet(media=[{"type": "video", "url": "https://video.twimg.com/v.mp4", "width": 1920, "height": 1080}]))
        self.assertIn("🎬", blocks[0])
        self.assertIn("1920×1080", blocks[0])


class BylineRewrite(unittest.TestCase):
    """Existing memos get the same byline without a re-render or a source tweet."""

    def test_old_byline_collapses_to_name_and_time(self):
        text = "[@Khazix0918](https://x.com/Khazix0918) · 数字生命卡兹克 ✓ · 11-16 09:59 · 👁17.4万 🔖3.8k ♥3.5k 💬169 🔁425 ❝54\n\n正文"
        self.assertEqual(
            render.normalize_byline(text),
            "[@数字生命卡兹克](https://x.com/Khazix0918) · 11-16 09:59\n\n正文",
        )

    def test_rewrite_works_under_a_headline(self):
        text = "### 标题\n\n[@dotey](https://x.com/dotey) · 宝玉 ✓ · 08-06 16:30 · 👁12.8万\n\n正文"
        self.assertIn("[@宝玉](https://x.com/dotey) · 08-06 16:30", render.normalize_byline(text))

    def test_byline_without_time_or_name_is_left_alone(self):
        text = "[@zalegzl](https://x.com/zalegzl)\n\n正文"
        self.assertEqual(render.normalize_byline(text), text)

    def test_retweet_marker_survives(self):
        text = "🔁 @rep · [@other](https://x.com/other) · Some Name ✓ · 11-16 09:59 · 👁1"
        self.assertEqual(render.normalize_byline(text), "🔁 @rep · [@Some Name](https://x.com/other) · 11-16 09:59")

    def test_idempotent(self):
        text = "[@a](https://x.com/a) · 名字 · 11-16 09:59 · 👁1"
        once = render.normalize_byline(text)
        self.assertEqual(render.normalize_byline(once), once)


class Quoting(unittest.TestCase):
    def test_quote_that_evaporates_is_not_rendered(self):
        quote = {"id": "444", "text": "https://t.co/self", "author": {"screenName": "a", "name": "A"}}
        card = render.render_tweet(
            tweet(quotedTweet=quote), links={"https://t.co/self": "https://x.com/a/status/444"}, saved_at=SAVED_AT
        )
        self.assertNotIn("> **@a**", card)

    def test_every_line_is_quoted(self):
        block = render.quoted_block(
            tweet(quotedTweet={"id": "9", "text": "第一段\n\n第二段", "author": {"screenName": "lonely", "name": "Lonely"}})
        )
        self.assertTrue(all(line.startswith(">") for line in block.split("\n")))
        self.assertIn(">", block.split("\n")[2], "blank lines inside a quote need a bare '>'")

    def test_headings_inside_quote_are_demoted(self):
        block = render.quoted_block(tweet(quotedTweet={"id": "9", "text": "## 小标题\n正文", "author": {"screenName": "x"}}))
        self.assertNotIn("> ## ", block)
        self.assertIn("> #### ", block)


class TagSuggestion(unittest.TestCase):
    def test_matches_archive_vocabulary(self):
        self.assertIn("github", render.suggest_tags(tweet()))
        self.assertIn("前端", render.suggest_tags(tweet()))

    def test_never_suggests_structural_tags(self):
        tags = render.suggest_tags(tweet(text="#Twitter #Bookmark 内容"))
        self.assertNotIn("Twitter", tags)
        self.assertNotIn("Bookmark", tags)

    def test_capped_at_three(self):
        noisy = tweet(text="github claude codex mcp prompt agent llm 前端 rust python", urls=["https://github.com/a/b"])
        self.assertLessEqual(len(render.suggest_tags(noisy)), 3)


class FullCard(unittest.TestCase):
    def setUp(self):
        self.md = render.render_tweet(tweet(), saved_at=SAVED_AT)

    def test_first_line_is_h3_not_h1(self):
        self.assertTrue(self.md.startswith("### "), self.md.split("\n")[0])

    def test_headline_not_duplicated_in_body(self):
        self.assertEqual(self.md.count("这个动画库可以啊"), 1)

    def test_byline_is_name_and_time_only(self):
        """The name is what readers recognise; counts are noise while scanning a list."""
        byline = self.md.split("\n")[2]
        self.assertEqual(byline, "[@Kokonut](https://x.com/kokonutlabs) · 11-16 11:36")

    def test_metrics_are_opt_in(self):
        md = render.render_tweet(tweet(), saved_at=SAVED_AT, show_metrics=True)
        self.assertIn("👁6.4k", md)
        self.assertIn("🔖186", md)

    def test_zero_metrics_are_dropped_when_asked_for(self):
        md = render.render_tweet(tweet(metrics={"views": 0, "likes": 3}), saved_at=SAVED_AT, show_metrics=True)
        self.assertIn("♥3", md)
        self.assertNotIn("👁", md)

    def test_handle_is_only_the_href(self):
        self.assertNotIn("@kokonutlabs)", self.md.split("\n")[2].replace("](https://x.com/kokonutlabs)", ""))

    def test_short_link_expanded_and_not_billed_twice(self):
        self.assertIn("https://github.com/kokonut-labs/kokonutui", self.md)
        self.assertNotIn("🔗 链接", self.md)

    def test_footer_and_tags_are_last(self):
        lines = [line for line in self.md.split("\n") if line.strip()]
        self.assertTrue(lines[-1].startswith("#Twitter #Bookmark"), lines[-1])
        self.assertIn("[原推](https://x.com/kokonutlabs/status/2122286309855338953)", lines[-2])
        self.assertIn("存档 2026-11-17", lines[-2])

    def test_no_blank_line_runs(self):
        self.assertNotIn("\n\n\n", self.md)

    def test_retweet_is_flagged(self):
        md = render.render_tweet(tweet(isRetweet=True, retweetedBy="someone"), saved_at=SAVED_AT)
        self.assertIn("🔁 @someone", md)

    def test_verified_mark_is_gone(self):
        md = render.render_tweet(tweet(author={"name": "Kokonut Labs", "screenName": "k", "verified": True}), saved_at=SAVED_AT)
        self.assertIn("[@Kokonut Labs](https://x.com/k)", md)
        self.assertNotIn("✓", md)

    def test_brackets_in_display_name_do_not_break_the_link(self):
        md = render.render_tweet(tweet(author={"name": "阿川 | AI [thinking]", "screenName": "ai"}), saved_at=SAVED_AT)
        self.assertIn("[@阿川 | AI (thinking)](https://x.com/ai)", md)

    def test_name_equal_to_handle_is_not_repeated(self):
        md = render.render_tweet(tweet(author={"name": "KokonutLabs", "screenName": "kokonutlabs"}), saved_at=SAVED_AT)
        self.assertIn("[@KokonutLabs](https://x.com/kokonutlabs) · ", md)
        self.assertEqual(md.count("KokonutLabs"), 1)

    def test_own_headings_demoted(self):
        md = render.render_tweet(tweet(text="开头一句\n\n## 原文小标题\n\n正文内容在这里，足够长了吧"), saved_at=SAVED_AT)
        self.assertNotIn("\n## ", md)
        self.assertIn("#### 原文小标题", md)

    def test_article_title_wins(self):
        md = render.render_tweet(tweet(articleTitle="真正的文章标题"), saved_at=SAVED_AT)
        self.assertTrue(md.startswith("### 真正的文章标题"))

    def test_title_override_wins_over_article(self):
        md = render.render_tweet(tweet(articleTitle="文章标题"), title="我改的标题", saved_at=SAVED_AT)
        self.assertTrue(md.startswith("### 我改的标题"))

    def test_article_folds_into_details(self):
        md = render.render_tweet(tweet(), article="# 长文\n\n正文\n\n## 小节\n内容", saved_at=SAVED_AT)
        self.assertIn("<details><summary>📄 全文 · ", md)
        self.assertIn("\n\n<details>", md, "details needs a blank line before it or it will not render")
        self.assertIn("</details>", md)
        self.assertNotIn("\n# 长文", md)

    def test_article_metadata_header_is_dropped(self):
        dump = "# 标题\n\n- Author: @a (A)\n- Published: Sun Sep 20 02:43:24 +0000 2026\n- URL: https://x.com/a/status/1\n- Likes: 1.1K\n\n作者：someone\n\n正文第一段。"
        md = render.render_tweet(tweet(articleTitle="标题"), article=dump, saved_at=SAVED_AT)
        folded = md.split("<summary>", 1)[1]
        self.assertNotIn("Author:", folded)
        self.assertNotIn("Likes:", folded)
        self.assertIn("正文第一段", folded)
        self.assertNotIn("#### 标题", folded, "the card title already says it")

    def test_article_h1_survives_when_it_differs_from_the_card_title(self):
        dump = "# 文章自己的题目\n\n- Author: @a\n\n正文。"
        md = render.render_tweet(tweet(), article=dump, saved_at=SAVED_AT)
        self.assertIn("### 文章自己的题目", md)
        self.assertNotIn("- Author:", md.split("<summary>", 1)[1])

    def test_empty_article_renders_nothing(self):
        self.assertNotIn("<details>", render.render_tweet(tweet(), article="   ", saved_at=SAVED_AT))

    def test_extra_tags_dedupe_and_normalise(self):
        md = render.render_tweet(tweet(), tags=["前端", "#Twitter", "two words"], saved_at=SAVED_AT)
        tag_line = md.split("\n")[-1]
        self.assertEqual(tag_line, "#Twitter #Bookmark #前端 #two-words")

    def test_auto_tags_are_appended(self):
        md = render.render_tweet(tweet(), auto_tags=True, saved_at=SAVED_AT)
        self.assertIn("#github", md.split("\n")[-1])

    def test_quoted_tweet_renders_above_media(self):
        md = render.render_tweet(
            tweet(quotedTweet={"id": "9", "text": "引用正文", "author": {"screenName": "q", "name": "Q"}}, media=[{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}]),
            saved_at=SAVED_AT,
        )
        self.assertLess(md.index("引用正文"), md.index("![]("))

    def test_photo_and_video_both_render(self):
        md = render.render_tweet(
            tweet(
                media=[
                    {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"},
                    {"type": "video", "url": "https://video.twimg.com/v.mp4", "width": 1280, "height": 720},
                ]
            ),
            saved_at=SAVED_AT,
        )
        self.assertIn("?name=large", md)
        self.assertIn("🎬", md)

    def test_older_year_keeps_year(self):
        md = render.render_tweet(tweet(createdAtISO="2024-03-02T08:00:00Z"), saved_at=SAVED_AT)
        self.assertIn("2024-03-02 08:00", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
