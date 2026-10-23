"""Claude API で新着の要約・重要度判定・ポスト案3つを生成する。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from sources import Item
from xtext import LIMIT, x_length

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STYLE_PATH = ROOT / "config" / "style_examples.md"

CATEGORIES = ["新規カード", "制限改訂", "新商品", "イベント", "その他"]

# 本人は自分で文章を書くことが多いので、ポスト案は「たたき台」1つだけ。書く材料としてポイントの箇条書きを付ける
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "importance": {"type": "integer", "description": "0〜100"},
        "summary": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "points": {"type": "array", "items": {"type": "string"}},
        "draft": {"type": "string"},
    },
    "required": ["importance", "summary", "category", "points", "draft"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
あなたは遊戯王OCGを扱うYouTubeチャンネル「ガチネタ！Freeデュエル」の運用者「けん」のSNS担当アシスタントです。
遊戯王の公式（公式X・公式サイト）の新着情報を1件受け取り、次のものを作ります。
けんはポストを自分の言葉で書くことが多いので、「書く材料」を素早く渡すのが一番の目的です。

1. importance（0〜100）: このチャンネルのフォロワー（遊戯王OCGプレイヤー）にとっての注目度。
   目安: 新規カード公開・リミットレギュレーション発表・新商品発表は高め（60〜100）、
   大型イベントは中くらい、店舗リスト更新・キャンペーンの細かな告知・定型的なお知らせは低め（0〜30）。
   遊戯王OCG以外の話題（ラッシュデュエル、アニメ、グッズなど）や、発売済み商品の再告知・クイズ・初心者向け案内も低め。
2. summary: 何が発表されたかを1〜2文で。公式の記述にない情報は足さない。
3. category: 新規カード / 制限改訂 / 新商品 / イベント / その他 のいずれか。
4. points: ポストを書くときにそのまま使える事実の箇条書き（2〜5個）。
   カード名・商品名・発売日・適用日・開催日など、公式の記述にある固有名詞や日付を正確に（「」で囲む）。
   公式の文面にない情報・推測は入れない。装飾文字（𝐈𝐌𝐌𝐎𝐑𝐓𝐀𝐋 など）は普通の文字に直す。
5. draft: X用のポスト案を1つだけ（たたき台）。短めでよい。

ポスト案のルール:
- 日本語で、本文は140文字以内。URLはシステムが末尾に自動で付けるので、本文にURLを書かない。
- けんの口調に寄せる（下の過去ポスト例を参考に）。一人称は単数（「俺」「僕」「自分」など例に合わせる。「僕ら」「我々」は使わない）。
- 根拠は渡された公式情報だけ。未確定情報・リーク・推測を事実として書かない。考察は「〜かも」「〜しそう」など推測だとわかる書き方にする。
- ハッシュタグは0〜2個まで（例: #遊戯王）。
- 動画を添付する前提の案は出さない（KONAMIの動画投稿ガイドラインでXへの動画投稿は不可）。
- 公式の情報量が少ないときも、書いてあること以上を盛らない。テンションは控えめ〜普通。

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
    points: list[str] = field(default_factory=list)


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


def finalize_text(text: str, item: Item) -> str:
    """本文末尾にURLを付ける。公式Xが元ネタなら投稿URL（引用表示になる）。"""
    return text.strip() + "\n" + item.url


def draft(item: Item, settings: dict, client: anthropic.Anthropic | None = None) -> DraftResult | None:
    system = SYSTEM_PROMPT.format(style_examples=load_style_examples())
    data = _request_json(system, build_user_message(item), OUTPUT_SCHEMA, settings, client)
    if data is None:
        return None
    return DraftResult(
        importance=max(0, min(100, int(data.get("importance", 0)))),
        summary=data.get("summary", ""),
        category=data.get("category", "その他"),
        drafts=[Draft(type="たたき台", text=finalize_text(data.get("draft", ""), item))] if data.get("draft") else [],
        points=[p for p in data.get("points", []) if p.strip()],
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

# ポストの型は config/promo_template.md。AI が書くのは {scene_comment}（シーンについての1〜2行）だけ。
PROMO_TEMPLATE_PATH = ROOT / "config" / "promo_template.md"
PROMO_TYPES = ["シーン紹介", "見てほしいポイント", "ひとこと感想"]

PROMO_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "description": "見どころシーンを一言で（20字程度）"},
        "comments": {
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
    "required": ["scene", "comments"],
    "additionalProperties": False,
}

PROMO_SYSTEM_PROMPT = """\
あなたは遊戯王OCGを扱うYouTubeチャンネル「ガチネタ！Freeデュエル」の運用者「けん」のSNS担当アシスタントです。
チャンネルの過去動画を紹介するX用ポストのうち、「切り抜いたシーンについての1〜2行」だけを書きます。
ポストの見出し・動画タイトル・URL・締めの一文は、決まった型でシステムが入れます。
けんは投稿時に、見どころシーンのスクリーンショットを1枚添付します。そのシーンに触れる文章にしてください。

作るもの:
1. scene: 見どころシーンを一言で（20字程度）。視聴者コメントがあればそれを根拠にする。
2. comments: シーンについての文章を3パターン。type は次の3つを1つずつ。
   - シーン紹介: 「○分○秒〜」のように時刻に触れつつ、何が起きるシーンかを紹介する
   - 見てほしいポイント: このシーンのどこに注目してほしいかを伝える
   - ひとこと感想: 作者として、このシーンへの短い感想
   それぞれ1〜2行。長さは「この動画で書ける字数」以内を厳守（X の文字数上限があるため）。その中で、短すぎて味気ない文にはしない。
   視聴者コメントが、プレイミスやルールへの疑問・指摘の内容なら、そこには触れない。

ルール:
- けんの口調に寄せる（下の過去ポスト例を参考に）。一人称は単数。テンションは控えめ〜普通。わざとらしく叫ばない。
- 根拠は動画タイトルと視聴者コメントだけ。書かれていないデッキの動き・結末・カード名を作らない。
  シーンの中身が分からないときは、「ここからの展開」「このターン」のようにぼかして書く。
  タイトルにあるカード名やコンボが、その時刻のシーンで使われているとは限らない。コメントに書かれていない限り決めつけない。
- 視聴者コメントの文をそのまま写さない。内容を参考に、けんの言葉で書く。
- URL・ハッシュタグ・「高評価〜」のような締めの一文は書かない（型に入っている）。

## けんの過去ポスト例（口調の参考）
{style_examples}
"""


@dataclass
class PromoResult:
    scene: str
    drafts: list[Draft] = field(default_factory=list)


def load_promo_template() -> str:
    text = PROMO_TEMPLATE_PATH.read_text(encoding="utf-8") if PROMO_TEMPLATE_PATH.exists() else ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    return text or "{title}\n{url}\n\n{scene_comment}"


def render_promo(template: str, video, scene_url: str, scene_comment: str) -> str:
    from youtube import episode, short_title

    # str.format だとタイトル中の { } で壊れるので、決まった名前だけ置き換える
    values = {
        "{episode}": episode(video.title),
        "{short_title}": short_title(video.title),
        "{title}": video.title,
        "{url}": scene_url,
        "{scene_comment}": scene_comment.strip(),
    }
    out = template
    for key, value in values.items():
        out = out.replace(key, value)
    return out


def promo_comment_budget(template: str, video, scene_url: str, margin: int = 6) -> int:
    """型に入れたあと X の上限（280）に収まる、シーンの一言の全角字数。"""
    fixed = x_length(render_promo(template, video, scene_url, ""))
    return max(20, (LIMIT - fixed - margin) // 2)


def build_promo_message(video, highlight, scene_url: str, budget: int) -> str:
    from youtube import mmss

    lines = [
        f"この動画で書ける字数: 1パターンあたり全角{budget}字以内（改行も1字と数える）",
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
    template = load_promo_template()
    budget = promo_comment_budget(template, video, scene_url)
    data = _request_json(system, build_promo_message(video, highlight, scene_url, budget), PROMO_SCHEMA, settings, client)
    if data is None:
        return None
    return PromoResult(
        scene=data.get("scene", ""),
        drafts=[
            Draft(type=c["type"], text=render_promo(template, video, scene_url, c["text"]))
            for c in data.get("comments", [])
        ],
    )

