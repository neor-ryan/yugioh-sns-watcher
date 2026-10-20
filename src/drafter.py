"""Claude API で新着の要約・重要度判定・ポスト案3つを生成する。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from sources import Item

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STYLE_PATH = ROOT / "config" / "style_examples.md"

CATEGORIES = ["新規カード", "制限改訂", "新商品", "イベント", "その他"]
DRAFT_TYPES = ["速報", "感想・考察", "動画/サイト誘導"]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "importance": {"type": "integer", "description": "0〜100"},
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": DRAFT_TYPES},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["importance", "summary", "category", "drafts"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
あなたは遊戯王OCGを扱うYouTubeチャンネル「ガチネタ！Freeデュエル」の運用者「けん」のSNS担当アシスタントです。
遊戯王OCG公式（公式X・公式サイト）の新着情報を1件受け取り、次の3つを作ります。

1. importance（0〜100）: このチャンネルのフォロワー（遊戯王OCGプレイヤー）にとっての注目度。
   目安: 新規カード公開・リミットレギュレーション発表・新商品発表は高め（60〜100）、
   大型イベントは中くらい、店舗リスト更新・キャンペーンの細かな告知・定型的なお知らせは低め（0〜30）。
2. summary: 何が発表されたかを1〜2文で。公式の記述にない情報は足さない。
3. category: 新規カード / 制限改訂 / 新商品 / イベント / その他 のいずれか。
4. drafts: X用のポスト案を3つ。type は「速報」「感想・考察」「動画/サイト誘導」を1つずつ。

ポスト案のルール:
- 日本語で、本文は140文字以内。URLはシステムが末尾に自動で付けるので、本文にURLを書かない。
- けんの口調に寄せる（下の過去ポスト例を参考に）。一人称は単数（「俺」「僕」「自分」など例に合わせる。「僕ら」「我々」は使わない）。
- 根拠は渡された公式情報だけ。未確定情報・リーク・推測を事実として書かない。考察は「〜かも」「〜しそう」など推測だとわかる書き方にする。
- ハッシュタグは0〜2個まで（例: #遊戯王）。
- 動画を添付する前提の案は出さない（KONAMIの動画投稿ガイドラインでXへの動画投稿は不可）。「動画/サイト誘導」は、YouTubeやサイトで詳しく話す・まとめる、という誘導の文面にする。
- 公式の情報量が少ないときも、書いてあること以上を盛らない。

## けんの過去ポスト例（口調の参考）
{style_examples}
"""


@dataclass
class Draft:
    type: str
    text: str


@dataclass
class DraftResult:
    importance: int
    summary: str
    category: str
    drafts: list[Draft] = field(default_factory=list)


def load_style_examples() -> str:
    if STYLE_PATH.exists():
        text = re.sub(r"<!--.*?-->", "", STYLE_PATH.read_text(encoding="utf-8"), flags=re.S).strip()
        if text:
            return text
    return "（まだ登録されていません。丁寧すぎない、親しみやすい口調で書いてください。）"


def build_user_message(item: Item) -> str:
    source = "遊戯王OCG公式X（@YuGiOh_OCG_INFO）のポスト" if item.source == "x" else "遊戯王OCG公式サイトのニュース"
    lines = [f"ソース: {source}"]
    if item.published:
        lines.append(f"公開日時: {item.published}")
    if item.source == "site":
        lines.append(f"タイトル: {item.title}")
    lines.append(f"URL: {item.url}")
    lines.append("")
    lines.append("本文:" if item.source == "x" else "（公式サイトは一覧のタイトルのみ取得しています）")
    if item.source == "x":
        lines.append(item.text)
    return "\n".join(lines)


def finalize_text(text: str, item: Item, draft_type: str, settings: dict) -> str:
    """本文末尾にURLを付ける。公式Xが元ネタなら投稿URL（引用表示になる）。"""
    text = text.strip()
    urls = [item.url]
    promo = (settings.get("drafts") or {}).get("promo_url")
    if draft_type == "動画/サイト誘導" and promo:
        urls.insert(0, promo)
    return text + "\n" + "\n".join(urls)


def draft(item: Item, settings: dict, client: anthropic.Anthropic | None = None) -> DraftResult | None:
    cfg = settings["claude"]
    system = SYSTEM_PROMPT.format(style_examples=load_style_examples())
    kwargs = dict(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 16000),
        system=system,
        cache_control={"type": "ephemeral"},
        output_config={
            "effort": cfg.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": build_user_message(item)}],
    )
    try:
        client = client or anthropic.Anthropic()
        if cfg.get("fallbacks", True):
            # 安全分類器による誤拒否時、サーバー側で別モデルに自動で切り替える
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            response = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        log.error("Claude API レート制限: %s", e)
        return None
    except anthropic.APIStatusError as e:
        log.error("Claude API エラー %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.error("Claude API に接続できません: %s", e)
        return None
    except (anthropic.AnthropicError, TypeError) as e:  # APIキー未設定など
        log.error("Claude API を呼べません: %s", e)
        return None

    if response.stop_reason == "refusal":
        log.warning("Claude が生成を拒否しました: %s", getattr(response, "stop_details", None))
        return None
    if response.stop_reason == "max_tokens":
        log.warning("Claude の出力が max_tokens で途切れました")
        return None

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.error("Claude の出力がJSONとして読めません: %s", text[:300])
        return None

    drafts = [
        Draft(type=d["type"], text=finalize_text(d["text"], item, d["type"], settings))
        for d in data.get("drafts", [])
    ]
    return DraftResult(
        importance=max(0, min(100, int(data.get("importance", 0)))),
        summary=data.get("summary", ""),
        category=data.get("category", "その他"),
        drafts=drafts,
    )


def body_length(text: str) -> int:
    """URL行を除いた本文の文字数。"""
    return len("\n".join(line for line in text.splitlines() if not line.startswith("http")).strip())
