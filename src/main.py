"""遊戯王SNSウォッチャー エントリーポイント。

  python src/main.py --mode sweep            # 1回だけチェック（通常監視）
  python src/main.py --mode window           # 集中監視（時間帯の終わりまでループ）
  python src/main.py --dry-run --sample 2    # 取得と下書きだけ。通知・状態保存なし
  python src/main.py --test-notify           # ダミーの新着でDiscord通知を確認
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import drafter
import notifier
import state as state_mod
from drafter import Draft, DraftResult
from sources import Item, official_site, x_official

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
JST = timezone(timedelta(hours=9))

log = logging.getLogger("watcher")


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> dict:
    return yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def window_bounds(now: datetime, w: dict) -> tuple[datetime, datetime]:
    sh, sm = _hm(w["start"])
    eh, em = _hm(w["end"])
    return (
        now.replace(hour=sh, minute=sm, second=0, microsecond=0),
        now.replace(hour=eh, minute=em, second=0, microsecond=0),
    )


def current_window_end(now: datetime, settings: dict, early_margin_min: int = 30) -> datetime | None:
    for w in settings["window"]["times_jst"]:
        start, end = window_bounds(now, w)
        if start - timedelta(minutes=early_margin_min) <= now <= end:
            return end
    return None


def in_window(now: datetime, settings: dict) -> bool:
    margin = timedelta(minutes=settings["sweep"].get("skip_margin_minutes", 10))
    for w in settings["window"]["times_jst"]:
        start, end = window_bounds(now, w)
        if start - margin <= now <= end + margin:
            return True
    return False


def current_x_slot(now: datetime, settings: dict) -> tuple[datetime, datetime] | None:
    """X を見る時間枠（例 11:58〜12:10）のうち、今チェックすべきもの。

    枠の開始から slot_active_minutes の間は有効。集中監視が起動しなかった日も、
    次の毎時チェック（例 13:17）で枠内の投稿を拾えるように長めにとっている。
    """
    active = timedelta(minutes=settings["x"].get("slot_active_minutes", 90))
    for s in settings["x"]["slots_jst"]:
        start, end = window_bounds(now, s)
        if start <= now <= start + active:
            return start, end
    return None


class Runner:
    def __init__(self, settings: dict, args: argparse.Namespace):
        self.settings = settings
        self.args = args
        self.dry_run = args.dry_run
        self.state = state_mod.load()
        self.errors = 0
        self.low_items: list[tuple[Item, DraftResult]] = []
        self.x_enabled = "x" in args.sources and bool(os.environ.get("X_BEARER_TOKEN"))
        if "x" in args.sources and not self.x_enabled:
            log.warning("X_BEARER_TOKEN が無いので公式Xの監視はスキップします")

    # --- 取得 ---
    def check_site(self) -> list[Item]:
        cfg = self.settings["site"]
        try:
            if self.args.sample:
                return official_site.fetch_news(cfg)[: self.args.sample]
            return official_site.check(self.state, cfg)
        except Exception as e:  # ネットワーク障害などで監視全体を止めない
            log.error("公式サイトの取得に失敗: %s", e)
            self.errors += 1
            return []

    def check_x(self) -> list[Item]:
        cfg = {**self.settings["x"], "user_agent": self.settings["site"]["user_agent"]}
        try:
            if self.args.sample:
                # 時間枠を無視して直近24時間の最新投稿を各アカウント5件まで取る（状態は書き換えない）
                now = datetime.now(JST)
                sample_state = {
                    "x_accounts": {
                        k: {"user_id": v.get("user_id"), "since_id": None}
                        for k, v in self.state.get("x_accounts", {}).items()
                    }
                }
                items, _ = x_official.check(
                    sample_state, {**cfg, "max_results": 5, "max_pages": 1}, (now - timedelta(days=1), now)
                )
                return items[-self.args.sample :]
            slot = current_x_slot(datetime.now(JST), self.settings)
            if slot is None:
                return []
            items, failed = x_official.check(self.state, cfg, slot)
            self.errors += failed
            return items
        except Exception as e:
            log.error("公式Xの取得に失敗: %s", e)
            self.errors += 1
            return []

    # --- 処理 ---
    def process(self, items: list[Item]) -> None:
        threshold = self.settings["importance_threshold"]
        for item in items:
            log.info("新着: [%s] %s", item.source, item.title or item.url)
            result = drafter.draft(item, self.settings)
            if self.dry_run:
                print_result(item, result, self.settings)
                continue
            try:
                if result is None:
                    notifier.notify_raw(item, self.settings, "下書きの生成に失敗しました")
                elif result.importance < threshold:
                    log.info("重要度 %d < %d のため個別通知しません", result.importance, threshold)
                    if self.settings.get("low_importance") == "digest":
                        self.low_items.append((item, result))
                else:
                    notifier.notify_item(item, result, self.settings)
            except Exception as e:
                log.error("Discord 通知に失敗: %s", e)
                self.errors += 1

    def flush_digest(self) -> None:
        if self.low_items and not self.dry_run:
            try:
                notifier.notify_digest(self.low_items, self.settings)
            except Exception as e:
                log.error("まとめ通知に失敗: %s", e)
                self.errors += 1
        self.low_items = []

    def save(self) -> None:
        if not self.dry_run and not self.args.sample:
            state_mod.save(self.state)

    # --- モード ---
    def run_once(self) -> None:
        items: list[Item] = []
        if self.x_enabled:
            items += self.check_x()
        if "site" in self.args.sources:
            items += self.check_site()
        self.save()
        self.process(items)
        self.flush_digest()

    def run_window(self, end: datetime) -> None:
        wcfg = self.settings["window"]
        log.info("集中監視を %s (JST) まで続けます", end.strftime("%H:%M"))
        next_x = next_site = time.monotonic()
        while datetime.now(JST) < end:
            now = time.monotonic()
            items: list[Item] = []
            if self.x_enabled and now >= next_x:
                items += self.check_x()
                next_x = now + wcfg["x_interval_seconds"]
            if "site" in self.args.sources and now >= next_site:
                items += self.check_site()
                next_site = now + wcfg["site_interval_seconds"]
            if items:
                self.save()
                self.process(items)
            next_due = min(next_x if self.x_enabled else float("inf"),
                           next_site if "site" in self.args.sources else float("inf"))
            remaining = (end - datetime.now(JST)).total_seconds()
            sleep_for = min(max(next_due - time.monotonic(), 1), remaining)
            if sleep_for <= 0 or next_due == float("inf"):
                break
            time.sleep(sleep_for)
        self.save()
        self.flush_digest()


def print_result(item: Item, result: DraftResult | None, settings: dict) -> None:
    base = settings["discord"].get("intent_base", "https://x.com/intent/post")
    print("=" * 70)
    print(f"[{item.source}] {item.title}\n{item.url}  ({item.published})")
    if result is None:
        print("  → 下書き生成に失敗（通知時は新着のみ知らせます）")
        return
    print(f"重要度 {result.importance} / {result.category} / {result.summary}")
    for i, d in enumerate(result.drafts, 1):
        print(f"--- 案{i} {d.type}（本文 {drafter.body_length(d.text)} 字）")
        print(d.text)
        print(f"    投稿リンク: {notifier.intent_url(d.text, base)[:120]}…")


def test_notify(settings: dict) -> None:
    item = Item(
        source="site",
        id="https://www.yugioh-card.com/japan/test/",
        title="【テスト】遊戯王SNSウォッチャーの通知テスト",
        text="【テスト】遊戯王SNSウォッチャーの通知テスト",
        url="https://www.yugioh-card.com/japan/",
        published=datetime.now(JST).strftime("%Y/%m/%d %H:%M"),
    )
    result = None
    try:
        result = drafter.draft(item, settings)
    except Exception as e:  # APIキー未設定でも通知だけは確認できるようにする
        log.warning("Claude を呼べなかったのでダミーの下書きで通知します: %s", e)
    if result is None:
        texts = [
            ("速報", "【テスト】通知のテストです。このリンクからX投稿画面が開けばOK！ #遊戯王"),
            ("感想・考察", "テスト投稿の文面です。改行も\nちゃんと入るか確認。"),
            ("動画/サイト誘導", "詳しくは動画で話します（テスト）"),
        ]
        result = DraftResult(
            importance=80,
            summary="通知テスト用のダミーです",
            category="その他",
            drafts=[Draft(t, drafter.finalize_text(x, item, t, settings)) for t, x in texts],
        )
    notifier.notify_item(item, result, settings)
    log.info("テスト通知を送信しました")


def main() -> int:
    parser = argparse.ArgumentParser(description="遊戯王OCG公式情報の監視とポスト案通知")
    parser.add_argument("--mode", choices=["window", "sweep"], default="sweep")
    parser.add_argument("--dry-run", action="store_true", help="通知・状態保存をせず標準出力に出す")
    parser.add_argument("--test-notify", action="store_true", help="ダミーの新着でDiscord通知を確認")
    parser.add_argument("--sample", type=int, default=0, help="既読を無視して最新N件を新着扱いにする（--dry-run用）")
    parser.add_argument("--sources", default="x,site", help="監視するソース（カンマ区切り: x,site）")
    parser.add_argument("--minutes", type=int, help="window モードの継続時間を指定（時間帯外での手動実行用）")
    parser.add_argument("--force", action="store_true", help="sweep を集中監視の時間帯でも実行する")
    args = parser.parse_args()
    args.sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.sample:
        args.dry_run = True

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    settings = load_settings()

    if args.test_notify:
        test_notify(settings)
        return 0

    runner = Runner(settings, args)
    now = datetime.now(JST)
    if args.mode == "sweep":
        if in_window(now, settings) and not args.force:
            log.info("集中監視の時間帯なので通常監視はスキップします")
            return 0
        runner.run_once()
    else:
        end = now + timedelta(minutes=args.minutes) if args.minutes else current_window_end(now, settings)
        if end is None:
            log.info("集中監視の時間帯外です（手動で試すなら --minutes を指定）。1回だけチェックします")
            runner.run_once()
        else:
            runner.run_window(end)

    if runner.errors:
        log.error("エラーが %d 件ありました", runner.errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
