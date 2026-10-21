"""Discord Webhook への通知。"""
from __future__ import annotations

import logging
import os
import time
from urllib.parse import quote

import requests

from drafter import DraftResult, body_length
from sources import Item

log = logging.getLogger(__name__)

# Discordの上限: embed説明4096字 / embedタイトル256字 / 1メッセージのembed合計6000字・10個
EMBED_DESC_MAX = 4096
EMBED_TITLE_MAX = 256
MESSAGE_EMBED_TOTAL_MAX = 5900  # 6000 から少し余裕を持たせる

SOURCE_LABEL = {"x": "公式X", "site": "公式サイト"}


def intent_url(text: str, base: str) -> str:
    return f"{base}?text={quote(text, safe='')}"


def _color(importance: int) -> int:
    if importance >= 70:
        return 0xE74C3C
    if importance >= 40:
        return 0xF1C40F
    return 0x95A5A6


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _embed_size(embed: dict) -> int:
    return len(embed.get("title", "")) + len(embed.get("description", ""))


def build_embeds(item: Item, result: DraftResult, settings: dict) -> list[dict]:
    head = {
        "title": _clip(f"【{result.category}】{result.summary}", EMBED_TITLE_MAX),
        "url": item.url,
        "color": _color(result.importance),
        "description": _clip(
            f"重要度 **{result.importance}** ・ {SOURCE_LABEL.get(item.source, item.source)}\n"
            f"元ネタ: [{_clip(item.title or item.url, 100)}]({item.url})",
            EMBED_DESC_MAX,
        ),
    }
    return [head] + draft_embeds(result.drafts, _color(result.importance), settings)


def draft_embeds(drafts, color: int, settings: dict) -> list[dict]:
    """ポスト案1つにつき embed 1つ（本文＋「この文面で投稿」リンク）。"""
    intent_base = settings["discord"].get("intent_base", "https://x.com/intent/post")
    limit = settings["drafts"].get("max_chars", 140)
    embeds = []
    for i, d in enumerate(drafts, 1):
        n = body_length(d.text)
        over = f" ⚠️{limit}字超え" if n > limit else ""
        link = intent_url(d.text, intent_base)
        desc = f"```\n{d.text}\n```\n[▶ この文面で投稿]({link})"
        if len(desc) > EMBED_DESC_MAX:
            desc = f"```\n{d.text}\n```\n（本文が長すぎて投稿リンクを作れませんでした。コピーして使ってください）"
        embeds.append({"title": f"案{i}：{d.type}（{n}字）{over}", "description": desc, "color": color})
    return embeds


def _chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """embed合計6000字・10個の上限に収まるようにメッセージを分ける。"""
    chunks: list[list[dict]] = [[]]
    total = 0
    for e in embeds:
        size = _embed_size(e)
        if chunks[-1] and (total + size > MESSAGE_EMBED_TOTAL_MAX or len(chunks[-1]) >= 10):
            chunks.append([])
            total = 0
        chunks[-1].append(e)
        total += size
    return chunks


def send(payload: dict, settings: dict, webhook_env: str = "DISCORD_WEBHOOK_URL") -> None:
    """webhook_env の Webhook に送る。未設定なら DISCORD_WEBHOOK_URL（メインのチャンネル）に送る。"""
    url = os.environ.get(webhook_env) or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    payload = {"username": settings["discord"].get("username", "遊戯王SNSウォッチャー"), **payload}
    for attempt in range(3):
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 429:
            wait = float(resp.json().get("retry_after", 1))
            log.warning("Discord レート制限。%.1f 秒待ちます", wait)
            time.sleep(wait + 0.5)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Discord {resp.status_code}: {resp.text[:300]}")
        return
    raise RuntimeError("Discord への送信がレート制限で失敗しました")


def notify_item(item: Item, result: DraftResult, settings: dict) -> None:
    for chunk in _chunk_embeds(build_embeds(item, result, settings)):
        send({"embeds": chunk}, settings)


PROMO_COLOR = 0x9B59B6
REASON_LABEL = {
    "both": "視聴者維持率の山＋時刻付きコメント",
    "comment": "時刻付きコメント",
    "retention": "視聴者維持率の山",
    "none": "見どころを特定できず（冒頭から）",
}


def build_promo_embeds(video, highlight, scene_url: str, result, settings: dict) -> list[dict]:
    from youtube import mmss

    lines = [
        f"{video.views:,}回再生 ・ {video.published[:10]} 公開 ・ {mmss(video.duration)}",
        "",
    ]
    if highlight.reason != "none":
        lift = f"（前後より{highlight.lift * 100:.0f}%多く視聴）" if highlight.lift else ""
        lines.append(f"**見どころ：{mmss(highlight.seconds)}** {result.scene}")
        lines.append(f"根拠：{REASON_LABEL[highlight.reason]}{lift}")
        for c in highlight.comments[:2]:
            lines.append(f"> {_clip(' '.join(c.text.split()), 100)}（♥{c.likes}）")
        lines.append("")
        lines.append(f"[▶ 見どころシーンを開く（{mmss(max(0, highlight.seconds - 5))}から再生）]({scene_url})")
        lines.append("一時停止して右クリック →「動画フレームをコピー」で画像にできます")
    else:
        lines.append(f"[▶ 動画を開く]({scene_url})")
    head = {
        "title": _clip(f"【過去動画の宣伝】{video.title}", EMBED_TITLE_MAX),
        "url": f"https://youtu.be/{video.id}",
        "color": PROMO_COLOR,
        "description": _clip("\n".join(lines), EMBED_DESC_MAX),
    }
    if video.thumbnail:
        head["thumbnail"] = {"url": video.thumbnail}
    return [head] + draft_embeds(result.drafts, PROMO_COLOR, settings)


def notify_promo(video, highlight, scene_url: str, result, settings: dict) -> None:
    # 宣伝は専用チャンネルへ（DISCORD_PROMO_WEBHOOK_URL が無ければメインのチャンネル）
    for chunk in _chunk_embeds(build_promo_embeds(video, highlight, scene_url, result, settings)):
        send({"embeds": chunk}, settings, webhook_env="DISCORD_PROMO_WEBHOOK_URL")


def notify_raw(item: Item, settings: dict, reason: str) -> None:
    """下書き生成に失敗したときでも、新着があったことだけは知らせる。"""
    send(
        {
            "embeds": [
                {
                    "title": _clip(f"【新着】{item.title or item.url}", EMBED_TITLE_MAX),
                    "url": item.url,
                    "color": 0x3498DB,
                    "description": _clip(
                        f"{SOURCE_LABEL.get(item.source, item.source)} ・ {reason}\n\n{item.text}", EMBED_DESC_MAX
                    ),
                }
            ]
        },
        settings,
    )


def notify_digest(entries: list[tuple[Item, DraftResult]], settings: dict) -> None:
    """重要度の低い新着をまとめて1通にする。"""
    lines = [
        f"・[{r.category}] {_clip(r.summary, 120)}（重要度{r.importance}）[元ネタ]({it.url})"
        for it, r in entries
    ]
    desc = ""
    for line in lines:
        if len(desc) + len(line) + 1 > EMBED_DESC_MAX:
            break
        desc += line + "\n"
    send(
        {"embeds": [{"title": f"重要度の低い新着 {len(entries)} 件", "color": 0x95A5A6, "description": desc.strip()}]},
        settings,
    )
