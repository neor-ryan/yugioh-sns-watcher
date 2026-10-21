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
遊戯王の公式（公式X・公式サイト）の新着情報を1件受け取り、次の3つを作ります。

1. importance（0〜100）: このチャンネルのフォロワー（遊戯王OCGプレイヤー）にとっての注目度。
   目安: 新規カード公開・リミットレギュレーション発表・新商品発表は高め（60〜100）、
   大型イベントは中くらい、店舗リスト更新・キャンペーンの細かな告知・定型的なお知らせは低め（0〜30）。
   遊戯王OCG以外の話題（ラッシュデュエル、アニメ、グッズなど）や、発売済み商品の再告知・クイズ・初心者向け案内も低め。
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
    if item.source == "x":
        source = f"遊戯王の公式X（@{item.url.split('/')[3]}）のポスト"
    else:
        source = "遊戯王OCG公式サイトのニュース"
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
    system = SYSTEM_PROMPT.format(style_examples=load_style_examples())
    data = _request_json(system, build_user_message(item), OUTPUT_SCHEMA, settings, client)
    if data is None:
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


def _request_json(
    system: str, user_message: str, schema: dict, settings: dict, client: anthropic.Anthropic | None = None
) -> dict | None:
    """構造化出力で Claude を呼び、JSON を dict で返す。失敗したら None。"""
    cfg = settings["claude"]
    kwargs = dict(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 16000),
        system=system,
        cache_control={"type": "ephemeral"},
        output_config={
            "effort": cfg.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": user_message}],
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
        return json.loads(text)
    except json.JSONDecodeError:
        log.error("Claude の出力がJSONとして読めません: %s", text[:300])
        return None


# ---- 過去動画の宣伝 ----

PROMO_TYPES = ["見どころ紹介", "振り返り", "おすすめ"]

PROMO_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "description": "見どころシーンを一言で（20字程度）"},
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": PROMO_TYPES},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scene", "drafts"],
    "additionalProperties": False,
}

PROMO_SYSTEM_PROMPT = """\
あなたは遊戯王OCGを扱うYouTubeチャンネル「ガチネタ！Freeデュエル」の運用者「けん」のSNS担当アシスタントです。
チャンネルの過去動画を1本受け取り、その動画を改めて紹介するX用のポスト案を作ります。
けんは投稿時に、見どころシーンのスクリーンショットを1枚添付します。

作るもの:
1. scene: 見どころシーンを一言で（20字程度）。視聴者コメントがあればそれを根拠にする。
2. drafts: ポスト案を3つ。type は「見どころ紹介」「振り返り」「おすすめ」を1つずつ。
   - 見どころ紹介: 添付するシーンに注目させる（「○分○秒〜のここ」のように時刻に触れてもよい）
   - 振り返り: 投稿当時を振り返る、作者目線の一言
   - おすすめ: まだ見ていない人に向けて、どんな人に刺さる動画かを伝える

ルール:
- 日本語で、本文は140文字以内。動画のURLはシステムが末尾に付けるので、本文にURLを書かない。
- けんの口調に寄せる（下の過去ポスト例を参考に）。一人称は単数。テンションは控えめ〜普通。わざとらしく叫ばない。
- 根拠は動画タイトルと視聴者コメントだけ。書かれていないデッキの動き・結末・カード名を作らない。
- 視聴者コメントは引用せず、内容を参考にする。コメントした人を特定できる書き方はしない。
- ハッシュタグは0〜2個まで（例: #遊戯王 #ガチネタ）。
- 動画を添付する前提の案は出さない（KONAMIの動画投稿ガイドラインでXへの動画投稿は不可）。
- 「過去動画の宣伝です」のような前置きは書かない。

## けんの過去ポスト例（口調の参考）
{style_examples}
"""


@dataclass
class PromoResult:
    scene: str
    drafts: list[Draft] = field(default_factory=list)


def build_promo_message(video, highlight, scene_url: str) -> str:
    from youtube import mmss

    lines = [
        f"動画タイトル: {video.title}",
        f"公開日: {video.published[:10]}",
        f"再生回数: {video.views:,}回",
        f"長さ: {mmss(video.duration)}",
    ]
    if highlight.reason == "none":
        lines.append("見どころシーン: 特定できなかった（動画全体を紹介する）")
    else:
        basis = {
            "both": "視聴者維持率が前後より高く、視聴者のコメントも集まっている場所",
            "comment": "視聴者のコメントで時刻付きで触れられている場所",
            "retention": "視聴者維持率が前後より高い（よく見返されている）場所",
        }[highlight.reason]
        lines.append(f"見どころシーン: {mmss(highlight.seconds)}（{basis}）")
    if highlight.comments:
        lines.append("見どころ付近の視聴者コメント:")
        lines += [f"- {c.text[:200]}（いいね{c.likes}）" for c in highlight.comments]
    lines.append(f"リンク: {scene_url}")
    return "\n".join(lines)


def draft_promo(video, highlight, scene_url: str, settings: dict, client: anthropic.Anthropic | None = None) -> PromoResult | None:
    system = PROMO_SYSTEM_PROMPT.format(style_examples=load_style_examples())
    data = _request_json(system, build_promo_message(video, highlight, scene_url), PROMO_SCHEMA, settings, client)
    if data is None:
        return None
    return PromoResult(
        scene=data.get("scene", ""),
        drafts=[Draft(type=d["type"], text=d["text"].strip() + "\n" + scene_url) for d in data.get("drafts", [])],
    )


def body_length(text: str) -> int:
    """URL行を除いた本文の文字数。"""
    return len("\n".join(line for line in text.splitlines() if not line.startswith("http")).strip())
