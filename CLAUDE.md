# 遊戯王SNSウォッチャー（仮）

遊戯王OCGの公式情報を監視し、新着があればDiscordに「要約＋ポスト案＋投稿リンク」を通知するツール。
投稿は自動では行わない。通知のリンクから本文入りのX投稿画面を開き、本人が調整して投稿する。

運用者は個人（けん）。YouTubeチャンネル「ガチネタ！Freeデュエル」のSNS運用を楽にするのが目的。

---

## 1. 全体の流れ

```
[監視] 公式X / 公式サイトの新着を差分チェック
   ↓ 新着あり
[判定・下書き] Claude APIで要約・重要度判定・ポスト案3つを生成
   ↓
[通知] Discord Webhookに送信（ポスト案ごとに「この文面で投稿」リンク）
   ↓
[投稿] 本人がリンクを開き、X投稿画面で調整して手動投稿
```

## 2. 監視対象（MVP）

| ソース | 取得方法 | 備考 |
|---|---|---|
| 遊戯王OCG公式X `@YuGiOh_OCG_INFO` | X API v2 のユーザー投稿取得（`since_id` で新着のみ） | アプリ専用のBearerトークンで読み取りのみ。21:00 に収録カード公開 |
| 遊☆戯☆王オフィシャルサイトX `@yu_gi_oh_jp` | 同上 | 12:00 に「CARDニュース(OCG)」更新 |

- 公式Xは15分おきに宣伝ポスト（Q&Aクイズ、収録済みカードの再告知、初心者向け案内など）を出すが、取り上げる価値があるのは 12:00 / 21:00 ちょうどの投稿だけ（本人確認済み、2026-10）。そのため X は **11:58〜12:10 / 20:58〜21:10 の時間枠に投稿されたものだけ** を取る。
| 遊戯王OCG公式サイト `https://www.yugioh-card.com/japan/` | HTML取得→ニュース一覧をパース | 実装前にページ構造・RSSの有無・robots.txtを確認すること |

- X APIは従量課金。読み取りは返ってきた投稿の件数で課金されるため、**必ず `since_id` を使い新着だけを取る**。
- エンドポイントやベースURLは変更が多いので、実装前に https://docs.x.com で最新を確認する。
- 公式サイトへのアクセスは控えめに（集中監視中でも2〜3分間隔、通常時は1時間おき）。User-Agentにツール名を入れる。

## 3. 実行スケジュール

公式情報は **12:00 と 21:00（JST）** にほぼ集中する。

- **集中監視**：JST 11:50 に起動し、12:40 まで約60〜90秒間隔でポーリング。21時台も同様（20:50〜21:40）。
  - GitHub Actionsの定時起動は数分〜十数分遅れることがあるため、「早めに起動してジョブ内でループ」する。
- **通常監視**：1時間おきに1回だけチェック（把握漏れ対策）。集中監視の時間帯はスキップ。

GitHub Actions の cron（UTC表記）：

```
集中監視(昼): 50 2 * * *    # JST 11:50
集中監視(夜): 50 11 * * *   # JST 20:50
通常監視:     17 * * * *    # 毎時17分（00分は混むので避ける）
```

- **実際には GitHub の定時実行は数時間遅れたり飛ばされたりした（2026-10-21：昼の集中監視が5時間遅れ、夜は起動せず、毎時は4回だけ）**。そのため Google Apps Script（`tools/gas_trigger.gs`、けんのGoogleアカウントで1分ごとに実行）から `workflow_dispatch` で起動する。GitHub 側の `schedule` は予備として残している（遅れて起動しても、時間外なら1回チェックするだけで害はない）。
- ワークフローには `concurrency` を設定し、同時実行で状態ファイルが壊れないようにする。
- 実行時間の都合上、リポジトリは **public** を想定（秘密情報はすべてSecrets）。privateにする場合は監視時間を短縮する。

## 4. 状態管理

`state/state.json` に以下を保存し、実行後にActionsからコミットして戻す。

```json
{
  "x_accounts": {"YuGiOh_OCG_INFO": {"user_id": "初回に解決してキャッシュ", "since_id": "最後に処理した投稿ID"}},
  "site_seen_ids": ["処理済みのニュースのURLまたはID（直近200件程度）"],
  "last_run": "ISO8601"
}
```

- 初回実行時は「既存の投稿はすべて既読扱い」にして、過去分を一気に通知しないようにする。

## 5. 判定・下書き（Claude API）

- モデル：`claude-sonnet-5-5`
- 入力：新着1件の本文（＋公式サイトならタイトル・URL）、`config/style_examples.md` の過去ポスト
- 出力はJSONで受け取る：

```json
{
  "importance": 0-100,
  "summary": "何が発表されたかを1〜2文で",
  "category": "新規カード | 制限改訂 | 新商品 | イベント | その他",
  "drafts": [
    {"type": "速報", "text": "..."},
    {"type": "感想・考察", "text": "..."},
    {"type": "動画/サイト誘導", "text": "..."}
  ]
}
```

ポスト案のルール：

- 日本語で140文字以内（URLは別枠で末尾に付ける前提）
- 本人の口調に寄せる（`style_examples.md` を参照）。一人称は単数
- 公式が出した情報だけを根拠にし、未確定情報・リーク・推測を事実として書かない
- ハッシュタグは0〜2個まで（例：`#遊戯王`）
- 動画の添付を前提にした案は出さない（KONAMIの動画投稿ガイドラインでXへの動画投稿は不可のため）
- 公式Xの投稿が元ネタの場合、本文末尾にその投稿URLを入れる（引用表示になる）

`importance` が低いもの（設定値未満、初期値30）は通知を省略するか、まとめて1通にする。

## 6. 通知（Discord）

- Discord Webhookに送信。1件の新着につき1メッセージ。
- 構成：
  - 見出し：カテゴリ＋要約
  - 元ネタのリンク（公式X投稿 / 公式サイト記事）
  - ポスト案3つ。それぞれに「この文面で投稿」リンク
- 投稿リンクの形式：`https://x.com/intent/post?text=<URLエンコードした本文>`
  - 動かない場合は `https://twitter.com/intent/tweet?text=...` を試す
- Discordの文字数制限（本文2000字、embed説明4096字）に注意。

## 7. 構成

```
.
├── CLAUDE.md
├── requirements.txt          # requests, beautifulsoup4, anthropic, pyyaml
├── .env.example
├── config/
│   ├── settings.yaml         # 監視時間帯・間隔・重要度しきい値など
│   └── style_examples.md     # 過去に反応が良かった自分のポスト
├── state/
│   └── state.json
├── src/
│   ├── main.py               # --mode window | sweep, --dry-run, --test-notify, --sample N
│   ├── sources/
│   │   ├── __init__.py       # Item（新着1件）
│   │   ├── x_official.py
│   │   └── official_site.py
│   ├── drafter.py
│   ├── notifier.py
│   └── state.py
├── tests/
│   └── test_basic.py
└── .github/workflows/
    ├── window.yml
    └── sweep.yml
```

## 8. Secrets / 環境変数

| 名前 | 用途 |
|---|---|
| `ANTHROPIC_API_KEY` | ポスト案の生成 |
| `X_BEARER_TOKEN` | 公式Xの読み取り（未設定なら公式Xはスキップして公式サイトだけ監視） |
| `DISCORD_WEBHOOK_URL` | 通知 |

ローカルでは `.env` から読む（`.env` は `.gitignore` に入れる）。

## 9. 動作確認

- `python src/main.py --dry-run`：取得と下書きまで行い、通知せずに標準出力へ（状態は保存しない）
- `python src/main.py --dry-run --sample 2 --sources site`：既読を無視して最新2件で下書きを試す
- `python src/main.py --test-notify`：ダミーの新着でDiscord通知まで確認（Claudeを呼べなければダミー下書き）
- `python -m unittest discover tests`：パース・URL・Discord分割のテスト（ネット不要）
- 実装は「公式サイト取得 → 下書き → Discord通知」をローカルで通してから、X API、GitHub Actionsの順に足す

## 10. コスト管理

- X Developer Consoleで月の支出上限を設定する
- 運用1日目の後に請求画面を確認し、ポーリング間隔を調整する
- Claude APIは新着があったときだけ呼ぶ

## 11. 今回やらないこと（将来の候補）

- 同ジャンルのYouTube動向の監視
- どの案を使ったか・反応はどうだったかの記録と、提案への反映
- Discordボットのボタンでの再生成・トーン変更
- X全体のキーワード検索（読み取り課金が重いため）

---

## 実装メモ（2026-10 時点の調査結果）

- **公式サイト**：RSSなし、`robots.txt` は404（制限なし）。`/japan/news/` に `section.news-list` が4つ（おしらせ・重要なおしらせ・キャンペーン・ルール改訂、計80件弱）。各 `li > a` にタイトルと `<time>YYYY/M/D</time>`。IDは正規化した絶対URL（`//` を1つに畳む。`#date...` のアンカーは記事の区別に使うので残す）。
  - `site_seen_ids` は上限200件だが、ページに載っているIDは上限を超えても消さない（消すと再通知になる）。
  - 一覧から取るのはタイトルだけ。記事本文は取りに行かない（アクセスを控えめにするため）。
- **X API**：`GET https://api.x.com/2/users/by/username/{username}` → `GET /2/users/{id}/tweets?since_id=...&end_time=...`。docs.x.com ではフィールド指定が `post.fields`、長文は `note_post`（旧 `tweet.fields` / `note_tweet`）。400が返る場合は `settings.yaml` の `x.fields_param` を `tweet.fields` に戻す。リプライ・リポストは除外。
  - 時間枠の絞り込みは API 側でやる（枠外の投稿は返させない＝課金されない）。`since_id = max(保存済みID, 枠の開始時刻から作ったSnowflake ID)`、`end_time = 枠の終了`。投稿IDは `(ミリ秒 - 1288834974657) << 22` で時刻から作れる。`since_id+end_time` / `start_time+end_time` / `since_id+until_id` のどれも期待どおり動くことを確認済み。
  - X は枠の開始から90分だけチェックする（`slot_active_minutes`）。集中監視が起動しなかった日も、次の毎時チェック（13:17 / 22:17）で拾える。
  - 旧形式の状態（`x_user_id` / `x_since_id`）は読み込み時に `x_accounts.YuGiOh_OCG_INFO` へ移す。
- **Claude API**：構造化出力（`output_config.format` の JSON Schema）で受け取る。`effort: medium`。安全分類器の誤拒否対策として server-side fallback（`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`）を有効化。問題があれば `claude.fallbacks: false`。
  - URL はモデルに書かせず、コード側で本文末尾に付ける（X元ネタなら投稿URL＝引用表示、サイトなら記事URL）。
- **Discord**：投稿リンクは日本語をURLエンコードすると1案あたり1300字前後になるため、案ごとに embed を分け、embed合計6000字を超える場合はメッセージを分割する。
- **エラー**：取得・通知に失敗しても監視は止めず、最後に終了コード1で返す（Actions上で赤くなる）。下書き生成に失敗した新着は「新着あり」だけ通知する。
