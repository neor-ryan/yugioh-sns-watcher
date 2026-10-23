"""過去動画の宣伝：毎日1本選び、見どころシーン付きのポスト案を Discord に届ける。"""
from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, timedelta, timezone

import drafter
import notifier
import youtube
from xtext import x_length

log = logging.getLogger(__name__)

def candidates(videos: list[youtube.Video], history: dict[str, str], cfg: dict, today: date) -> list[youtube.Video]:
    """宣伝できる動画：対戦動画（通し番号あり）・一定の長さ以上・公開から一定日数・最近宣伝していない。"""
    min_published = today - timedelta(days=cfg.get("min_age_days", 30))
    recent = today - timedelta(days=cfg.get("repeat_after_days", 120))
    out = []
    for v in videos:
        if not youtube.episode(v.title) or v.duration < cfg.get("min_duration_seconds", 300):
            continue
        if date.fromisoformat(v.published[:10]) > min_published:
            continue
        last = history.get(v.id)
        if last and date.fromisoformat(last) > recent:
            continue
        out.append(v)
    return out


def choose(cands: list[youtube.Video], rng: random.Random) -> youtube.Video:
    """再生数の平方根で重み付けしてランダムに選ぶ（人気動画が出やすいが、偏りすぎない）。"""
    return rng.choices(cands, weights=[math.sqrt(v.views + 1) for v in cands], k=1)[0]


def is_due(now_jst: datetime, state: dict, cfg: dict) -> bool:
    """今日まだ宣伝しておらず、配信時刻を過ぎていれば True（遅れた日もその日のうちに出す）。"""
    return now_jst.hour >= cfg.get("hour_jst", 19) and state.get("promo", {}).get("last_date") != now_jst.date().isoformat()


def run(state: dict, settings: dict, dry_run: bool = False, rng: random.Random | None = None) -> bool:
    """1本選んで通知する。成功したら True。dry_run では標準出力に出し、状態も変えない。"""
    cfg = settings["promo"]
    rng = rng or random.Random()
    today = datetime.now(timezone(timedelta(hours=9))).date()
    promo_state = state.setdefault("promo", {"last_date": None, "history": {}})

    videos = youtube.list_videos(cfg["channel_handle"])
    cands = candidates(videos, promo_state["history"], cfg, today)
    if not cands:
        log.warning("宣伝できる動画がありません（全動画 %d 本）", len(videos))
        return False
    log.info("宣伝候補 %d 本 / 全 %d 本", len(cands), len(videos))

    video = choose(cands, rng)
    highlight = youtube.find_highlight(video)
    url = youtube.scene_url(video, highlight.seconds)
    log.info("宣伝する動画: %s（見どころ %s, %s）", video.title, youtube.mmss(highlight.seconds), highlight.reason)

    result = drafter.draft_promo(video, highlight, url, settings)
    if result is None:  # 下書き失敗でも、型だけ埋めた案と見どころは知らせる
        text = drafter.render_promo(drafter.load_promo_template(), video, url, "（ここにシーンの一言）")
        result = drafter.PromoResult(scene="", drafts=[drafter.Draft(type="型のみ", text=text)])

    if dry_run:
        print("=" * 70)
        print(video.title, f"({video.views:,}回)")
        print("見どころ:", youtube.mmss(highlight.seconds), highlight.reason, result.scene, url)
        for c in highlight.comments:
            print("  コメント:", c.likes, " ".join(c.text.split())[:80])
        for d in result.drafts:
            print(f"--- {d.type}（X換算 {x_length(d.text)}/280）\n{d.text}")
        return True

    notifier.notify_promo(video, highlight, url, result, settings)
    promo_state["last_date"] = today.isoformat()
    promo_state["history"][video.id] = today.isoformat()
    return True
