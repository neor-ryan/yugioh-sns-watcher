import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import drafter  # noqa: E402
import main  # noqa: E402
import notifier  # noqa: E402
import promo  # noqa: E402
import youtube  # noqa: E402
from drafter import Draft, DraftResult, finalize_text  # noqa: E402
from xtext import x_length  # noqa: E402
from sources import Item, official_site, x_official  # noqa: E402

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
        text = finalize_text("新規カード公開！ #遊戯王", self.item)
        self.assertTrue(text.endswith("https://x.com/YuGiOh_OCG_INFO/status/123"))
        self.assertEqual(text.splitlines()[0], "新規カード公開！ #遊戯王")

    def test_x_length(self):
        self.assertEqual(x_length("あいう"), 6)
        self.assertEqual(x_length("abc"), 3)
        self.assertEqual(x_length("見て https://youtu.be/N9ckjVTq6_0?t=422"), 4 + 1 + 23)
        # けんさんが実際に投稿しようとした文面は 293 で上限オーバー
        ken = (
            "【過去動画紹介】\n\n【#遊戯王】手札1枚で「エクゾディア」揃えるの簡単すぎたwww『HEROエクゾ』フリー対戦【#ガチネタ】#118\n"
            "https://youtu.be/N9ckjVTq6_0?t=422\n\n「青竜の召喚師」を使ったエクゾ回！\n"
            "ラストの「効果ではなく、ルールなんだな！」は我ながら気に入ってるフレーズかも\n\n気になった方はぜひ動画の方もチェック！"
        )
        self.assertEqual(x_length(ken), 293)

    def test_intent_url_roundtrip(self):
        text = "改行\nと #遊戯王 & 記号"
        url = notifier.intent_url(text, "https://x.com/intent/post")
        self.assertEqual(parse_qs(urlsplit(url).query)["text"][0], text)

    def test_embeds_fit_discord_limits(self):
        long_body = "あ" * 140
        drafts = [Draft("たたき台", finalize_text(long_body, self.item))]
        result = DraftResult(80, "要約" * 50, "新規カード", drafts, points=["「鬼神童鵄 タマヨリ」収録", "10/31発売"])
        embeds = notifier.build_embeds(self.item, result, SETTINGS)
        self.assertEqual(len(embeds), 2)
        self.assertIn("・「鬼神童鵄 タマヨリ」収録", embeds[0]["description"])
        self.assertIn("元ネタURLだけ入れて投稿画面を開く", embeds[0]["description"])
        self.assertIn("この文面で投稿", embeds[1]["description"])
        for e in embeds:
            self.assertLessEqual(len(e["description"]), 4096)
            self.assertLessEqual(len(e["title"]), 256)
        for chunk in notifier._chunk_embeds(embeds):
            self.assertLessEqual(sum(notifier._embed_size(e) for e in chunk), 6000)


class XSlotTest(unittest.TestCase):
    def test_snowflake_matches_real_post(self):
        # @YuGiOh_OCG_INFO 2026-10-20 21:00:01 JST の投稿
        post_id = 2112514169035477270
        self.assertLess(x_official.snowflake_at(datetime(2026, 10, 20, 21, 0, 0, tzinfo=main.JST)), post_id)
        self.assertGreater(x_official.snowflake_at(datetime(2026, 10, 20, 21, 0, 2, tzinfo=main.JST)), post_id)

    def test_check_account_requests_only_slot(self):
        start = datetime(2026, 10, 20, 20, 58, tzinfo=main.JST)
        end = datetime(2026, 10, 20, 21, 10, tzinfo=main.JST)
        calls = []

        def fake_get(cfg, path, params=None):
            calls.append(params)
            return {"data": [{"id": "2112514169035477270", "text": "収録カード公開"}], "meta": {}}

        state = {"x_user_id": "618646265", "x_since_id": "100"}  # 旧形式
        orig = x_official._get
        x_official._get = fake_get
        try:
            cfg = {**SETTINGS["x"], "accounts": ["YuGiOh_OCG_INFO"]}
            items, failed = x_official.check(state, cfg, (start, end))
        finally:
            x_official._get = orig
        self.assertEqual(failed, 0)
        self.assertEqual(calls[0]["since_id"], str(x_official.snowflake_at(start)))  # 古い since_id より枠の開始が優先
        self.assertEqual(calls[0]["end_time"], "2026-10-20T12:10:00Z")
        self.assertEqual(items[0].url, "https://x.com/YuGiOh_OCG_INFO/status/2112514169035477270")
        self.assertEqual(state["x_accounts"]["YuGiOh_OCG_INFO"]["since_id"], "2112514169035477270")
        self.assertNotIn("x_since_id", state)

    def test_current_x_slot(self):
        def at(h, m):
            return datetime(2026, 10, 20, h, m, tzinfo=main.JST)

        self.assertIsNone(main.current_x_slot(at(11, 50), SETTINGS))  # 枠の前
        self.assertEqual(main.current_x_slot(at(12, 5), SETTINGS), (at(11, 58), at(12, 10)))
        self.assertEqual(main.current_x_slot(at(13, 17), SETTINGS), (at(11, 58), at(12, 10)))  # 取りこぼし回収
        self.assertIsNone(main.current_x_slot(at(14, 17), SETTINGS))
        self.assertEqual(main.current_x_slot(at(22, 17), SETTINGS), (at(20, 58), at(21, 10)))


class PromoTest(unittest.TestCase):
    def video(self, vid="v1", title="【#遊戯王】対戦【#ガチネタ】#100", published="2025-01-01T00:00:00Z", duration=1200, views=1000):
        return youtube.Video(id=vid, title=title, published=published, duration=duration, views=views)

    def test_parse_duration(self):
        self.assertEqual(youtube.parse_duration("PT32M34S"), 1954)
        self.assertEqual(youtube.parse_duration("PT1H2M"), 3720)

    def test_candidates_filters(self):
        today = date(2026, 10, 22)
        cfg = SETTINGS["promo"]
        vids = [
            self.video("ok"),
            self.video("wpp", title="神回【#ガチネタ】#WPP2"),
            self.video("notice", title="【新企画】ガチネタゼミ、正式スタートします！"),
            self.video("short", duration=60),
            self.video("new", published="2026-10-18T00:00:00Z"),
            self.video("recent", ),
        ]
        history = {"recent": "2026-09-01", "ok": "2026-01-01"}
        got = [v.id for v in promo.candidates(vids, history, cfg, today)]
        self.assertEqual(got, ["ok", "wpp"])

    def test_pick_highlight_prefers_overlap(self):
        v = self.video(duration=1954)
        comments = [
            youtube.Comment(1382, "23:02 故・アーミライルか！", 18),
            youtube.Comment(1855, "30:55 ゲス声になりながら〜好きw", 16),
        ]
        spikes = [(332, 0.05), (1856, 0.04)]
        h = youtube.pick_highlight(v, comments, spikes)
        self.assertEqual((h.seconds, h.reason), (1855, "both"))
        self.assertEqual(youtube.pick_highlight(v, comments, []).seconds, 1382)  # コメントだけ → いいね最多
        self.assertEqual(youtube.pick_highlight(v, [], spikes).reason, "retention")
        self.assertEqual(youtube.pick_highlight(v, [], []).reason, "none")
        # いいね1以下のコメントは使わない
        weak = [youtube.Comment(715, "11:55 ここ素材にできなかったのでは", 0)]
        self.assertEqual(youtube.pick_highlight(v, weak, [(720, 0.03)]).reason, "retention")

    def test_usable_comment(self):
        own = {"textDisplay": "2:36 のカード名表記が誤っています", "authorChannelId": {"value": "UCme"}}
        self.assertFalse(youtube.is_usable_comment(own, "UCme"))
        self.assertFalse(youtube.is_usable_comment({"textDisplay": "11:55 ゴーレムは素材にできなかったのでは"}, "UCme"))
        self.assertTrue(youtube.is_usable_comment({"textDisplay": "30:55 ゲス声好きw", "authorChannelId": {"value": "UCx"}}, "UCme"))

    def test_timestamp_regex(self):
        found = [m.group(0) for m in youtube.TIMESTAMP_RE.finditer("19:12~ と 1:02:03 と 29:50〜")]
        self.assertEqual(found, ["19:12", "29:50"])  # h:mm:ss は対象外

    def test_scene_url_and_due(self):
        v = self.video("abc")
        self.assertEqual(youtube.scene_url(v, 1855), "https://youtu.be/abc?t=1850")
        self.assertEqual(youtube.scene_url(v, 3), "https://youtu.be/abc")
        cfg = SETTINGS["promo"]
        at = lambda h: datetime(2026, 10, 22, h, 17, tzinfo=main.JST)  # noqa: E731
        self.assertFalse(promo.is_due(at(18), {}, cfg))
        self.assertTrue(promo.is_due(at(19), {}, cfg))
        self.assertTrue(promo.is_due(at(22), {"promo": {"last_date": "2026-10-21"}}, cfg))  # 遅れても当日中に出す
        self.assertFalse(promo.is_due(at(22), {"promo": {"last_date": "2026-10-22"}}, cfg))

    def test_short_title(self):
        cases = {
            "【#遊戯王】手札1枚で「エクゾディア」揃えるの簡単すぎたwww『HEROエクゾ』フリー対戦【#ガチネタ】#118":
                "手札1枚で「エクゾディア」揃えるの簡単すぎたwww",
            "【#遊戯王】魂の頂上決戦！俺たちの原点！『マアト』vs『創星神sophia』フリー対戦：Ma'at vs Sophia【#ガチネタ】#100":
                "魂の頂上決戦！俺たちの原点！",
            "【#遊戯王】「TGレシプロドラゴンフライ」2枚あれば「エクゾディア」が全て揃うのは絶対ダメだろ！【#ガチネタ】#201":
                "「TGレシプロドラゴンフライ」2枚あれば「エクゾディア」が全て揃うのは絶対ダメだろ！",
            "【#遊戯王】『究極のブラマジ』vs『至高のレッドアイズ』！【#ガチネタ】#LOCR-01":
                "『究極のブラマジ』vs『至高のレッドアイズ』！",  # 『』始まりは全体を残す
        }
        for title, want in cases.items():
            self.assertEqual(youtube.short_title(title), want)
        self.assertEqual(youtube.episode("…【#ガチネタ】#BETB-02"), "#BETB-02")

    def test_render_promo_template_fits_x(self):
        v = self.video(title="【#遊戯王】手札1枚で「エクゾディア」揃えるの簡単すぎたwww『HEROエクゾ』フリー対戦【#ガチネタ】#118")
        tpl = drafter.load_promo_template()
        url = "https://youtu.be/N9ckjVTq6_0?t=422"
        scene = "「青竜の召喚師」を使ったエクゾ回！\nラストの「効果ではなく、ルールなんだな！」は我ながら気に入ってるフレーズかも"
        text = drafter.render_promo(tpl, v, url, scene)
        self.assertTrue(text.startswith("【過去動画紹介】#118\n手札1枚で「エクゾディア」揃えるの簡単すぎたwww\n"))
        self.assertTrue(text.endswith(url))
        self.assertNotIn("{", text.replace("{謎}", ""))
        self.assertNotIn("<!--", text)
        self.assertLessEqual(x_length(text), 280)
        budget = drafter.promo_comment_budget(tpl, v, url)
        self.assertGreaterEqual(budget, len(scene))  # けんさんの2行が入る余裕がある

    def test_promo_embeds_fit(self):
        v = self.video(duration=1954)
        h = youtube.Highlight(1855, "both", 0.04, [youtube.Comment(1855, "30:55 " + "あ" * 300, 16)])
        drafts = [Draft(t, "い" * 140 + "\nhttps://youtu.be/v1?t=1850") for t in drafter.PROMO_TYPES]
        embeds = notifier.build_promo_embeds(v, h, "https://youtu.be/v1?t=1850", drafter.PromoResult("見どころ", drafts), SETTINGS)
        self.assertEqual(len(embeds), 4)
        self.assertIn("30:50から再生", embeds[0]["description"])
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
