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
    intent_base = settings["discord"].get("intent_base", "https://x.com/intent/post")
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
    embeds = [head]
    limit = settings["drafts"].get("max_chars", 140)
    for i, d in enumerate(result.drafts, 1):
        n = body_length(d.text)
        over = f" ⚠️{limit}字超え" if n > limit else ""
        link = intent_url(d.text, intent_base)
        desc = f"```\n{d.text}\n```\n[▶ この文面で投稿]({link})"
        if len(desc) > EMBED_DESC_MAX:
            desc = f"```\n{d.text}\n```\n（本文が長すぎて投稿リンクを作れませんでした。コピーして使ってください）"
        embeds.append({"title": f"案{i}：{d.type}（{n}字）{over}", "description": desc, "color": _color(result.importance)})
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


def send(payload: dict, settings: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
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
