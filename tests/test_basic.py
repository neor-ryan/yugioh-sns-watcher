import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import main  # noqa: E402
import notifier  # noqa: E402
from drafter import Draft, DraftResult, body_length, finalize_text  # noqa: E402
from sources import Item, official_site  # noqa: E402

BASE = "https://www.yugioh-card.com/japan/news/"
HTML = """
<section class="news-list" id="information"><ul class="news-list">
<li class="information"><a href="/japan/event//limitregulation/?list=202610" class="news marker ">「リミットレギュレーション」2026年10月01日適用リスト<time>2026/9/24</time></a></li>
<li class="information"><a href="https://www.konami.com/yugioh/ots/" class="news marker external">店舗名称変更 <time>2026/3/19</time></a></li>
</ul></section>
<section class="news-list" id="revision"><ul class="news-list">
<li class="revision"><a href="/japan/revision/#date20260701a" class="news">「メンタルマスター」ルール改訂</a></li>
</ul></section>
"""

SETTINGS = main.load_settings()


class SiteTest(unittest.TestCase):
    def test_parse(self):
        items = official_site.parse_news(HTML, BASE)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].id, "https://www.yugioh-card.com/japan/event/limitregulation/?list=202610")
        self.assertEqual(items[0].published, "2026/9/24")
        self.assertNotIn("2026/9/24", items[0].title)
        self.assertEqual(items[2].id, "https://www.yugioh-card.com/japan/revision/#date20260701a")

    def test_check_first_run_and_new(self):
        state = {"site_seen_ids": []}
        items = official_site.parse_news(HTML, BASE)
        official_site.fetch_news = lambda cfg: items
        self.assertEqual(official_site.check(state, {"seen_limit": 2}), [])
        self.assertEqual(len(state["site_seen_ids"]), 3)

        new = Item("site", "https://example.com/new", "新着", "新着", "https://example.com/new")
        official_site.fetch_news = lambda cfg: [new] + items
        got = official_site.check(state, {"seen_limit": 2})
        self.assertEqual([i.id for i in got], [new.id])
        # ページ上のIDは上限(2)を超えても残る
        self.assertEqual(set(state["site_seen_ids"]), {new.id, *[i.id for i in items]})
        self.assertEqual(official_site.check(state, {"seen_limit": 2}), [])


class DraftAndNotifyTest(unittest.TestCase):
    def setUp(self):
        self.item = Item("x", "123", "t", "本文", "https://x.com/YuGiOh_OCG_INFO/status/123")

    def test_finalize_appends_source_url(self):
        text = finalize_text("新規カード公開！ #遊戯王", self.item, "速報", SETTINGS)
        self.assertTrue(text.endswith("https://x.com/YuGiOh_OCG_INFO/status/123"))
        self.assertEqual(body_length(text), len("新規カード公開！ #遊戯王"))

    def test_intent_url_roundtrip(self):
        text = "改行\nと #遊戯王 & 記号"
        url = notifier.intent_url(text, "https://x.com/intent/post")
        self.assertEqual(parse_qs(urlsplit(url).query)["text"][0], text)

    def test_embeds_fit_discord_limits(self):
        long_body = "あ" * 140
        drafts = [Draft(t, finalize_text(long_body, self.item, t, SETTINGS)) for t in ("速報", "感想・考察", "動画/サイト誘導")]
        result = DraftResult(80, "要約" * 50, "新規カード", drafts)
        embeds = notifier.build_embeds(self.item, result, SETTINGS)
        self.assertEqual(len(embeds), 4)
        for e in embeds:
            self.assertLessEqual(len(e["description"]), 4096)
            self.assertLessEqual(len(e["title"]), 256)
            self.assertIn("この文面で投稿", embeds[1]["description"])
        for chunk in notifier._chunk_embeds(embeds):
            self.assertLessEqual(sum(notifier._embed_size(e) for e in chunk), 6000)


class ScheduleTest(unittest.TestCase):
    def at(self, h, m):
        return datetime(2026, 10, 20, h, m, tzinfo=main.JST)

    def test_window_end(self):
        self.assertEqual(main.current_window_end(self.at(11, 50), SETTINGS), self.at(12, 40))
        self.assertEqual(main.current_window_end(self.at(12, 5), SETTINGS), self.at(12, 40))  # 起動遅延
        self.assertEqual(main.current_window_end(self.at(20, 55), SETTINGS), self.at(21, 40))
        self.assertIsNone(main.current_window_end(self.at(15, 0), SETTINGS))

    def test_sweep_skips_windows(self):
        self.assertTrue(main.in_window(self.at(12, 17), SETTINGS))
        self.assertTrue(main.in_window(self.at(21, 17), SETTINGS))
        self.assertFalse(main.in_window(self.at(11, 17), SETTINGS))
        self.assertFalse(main.in_window(self.at(13, 17), SETTINGS))


if __name__ == "__main__":
    unittest.main()
