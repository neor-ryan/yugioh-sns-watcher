"""遊戯王の公式X（@YuGiOh_OCG_INFO, @yu_gi_oh_jp）の新着ポストを X API v2 で取得する。

公式は15分おきに宣伝ポストを出すが、取り上げる価値のある情報（収録カード公開・CARDニュース）は
12:00 と 21:00 ちょうどに出る。そこで「時間枠」（例 11:58〜12:10）に投稿されたものだけを取る。
読み取りは返ってきた投稿数で課金されるので、枠外の投稿は API 側で除外して返させない:
  since_id = max(前回処理したID, 枠の開始時刻に相当するID)、end_time = 枠の終了時刻

エンドポイント（2026-10時点 docs.x.com）:
  GET https://api.x.com/2/users/by/username/{username}
  GET https://api.x.com/2/users/{id}/tweets?since_id=...&end_time=...&max_results=5..100
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from . import Item

log = logging.getLogger(__name__)

# X の投稿ID（Snowflake）は上位ビットが作成時刻（ミリ秒、この値からの経過）
SNOWFLAKE_EPOCH_MS = 1288834974657


class XApiError(RuntimeError):
    pass


def snowflake_at(dt: datetime) -> int:
    """その時刻より後に作られた投稿だけが since_id で返るような投稿IDを作る。"""
    return (int(dt.timestamp() * 1000) - SNOWFLAKE_EPOCH_MS) << 22


def _get(cfg: dict, path: str, params: dict | None = None) -> dict:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise XApiError("X_BEARER_TOKEN が設定されていません")
    resp = requests.get(
        cfg["api_base"].rstrip("/") + path,
        params=params,
        headers={"Authorization": f"Bearer {token}", "User-Agent": cfg["user_agent"]},
        timeout=20,
    )
    if resp.status_code == 429:
        reset = resp.headers.get("x-rate-limit-reset")
        raise XApiError(f"X API レート制限（429）。reset={reset}")
    if resp.status_code >= 400:
        raise XApiError(f"X API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _account_state(state: dict, username: str) -> dict:
    accounts = state.setdefault("x_accounts", {})
    # 旧形式（公式OCGアカウント1つだけ）からの移行
    if not accounts and state.get("x_user_id"):
        accounts["YuGiOh_OCG_INFO"] = {"user_id": state["x_user_id"], "since_id": state.get("x_since_id")}
    state.pop("x_user_id", None)
    state.pop("x_since_id", None)
    return accounts.setdefault(username, {"user_id": None, "since_id": None})


def resolve_user_id(acct: dict, cfg: dict, username: str) -> str:
    if acct.get("user_id"):
        return acct["user_id"]
    data = _get(cfg, f"/users/by/username/{username}")
    acct["user_id"] = data["data"]["id"]
    log.info("X: @%s のユーザーIDは %s", username, acct["user_id"])
    return acct["user_id"]


def _post_text(post: dict) -> str:
    # 長文ポストは note_post（旧名 note_tweet）に全文が入る
    for key in ("note_post", "note_tweet"):
        note = post.get(key)
        if isinstance(note, dict) and note.get("text"):
            return note["text"]
    return post.get("text", "")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_account(state: dict, cfg: dict, username: str, slot: tuple[datetime, datetime]) -> list[Item]:
    acct = _account_state(state, username)
    user_id = resolve_user_id(acct, cfg, username)
    start, end = slot
    # end_time は現在時刻より少し前でないと弾かれることがある
    end = min(end, datetime.now(timezone.utc) - timedelta(seconds=15))
    if end <= start:
        return []
    since_id = max(int(acct.get("since_id") or 0), snowflake_at(start))
    params = {
        "since_id": str(since_id),
        "end_time": _iso(end),
        "max_results": cfg.get("max_results", 20),
        cfg.get("fields_param", "post.fields"): "created_at,note_post",
    }
    if cfg.get("exclude"):
        params["exclude"] = ",".join(cfg["exclude"])

    posts: list[dict] = []
    for _ in range(cfg.get("max_pages", 3)):
        data = _get(cfg, f"/users/{user_id}/tweets", params)
        posts.extend(data.get("data", []))
        token = data.get("meta", {}).get("next_token")
        if not token:
            break
        params["pagination_token"] = token

    if posts:
        acct["since_id"] = str(max(since_id, *(int(p["id"]) for p in posts)))
    items = []
    for post in sorted(posts, key=lambda p: int(p["id"])):
        text = _post_text(post)
        items.append(
            Item(
                source="x",
                id=post["id"],
                title=text.splitlines()[0][:80] if text else "",
                text=text,
                url=f"https://x.com/{username}/status/{post['id']}",
                published=post.get("created_at"),
            )
        )
    return items


def check(state: dict, cfg: dict, slot: tuple[datetime, datetime]) -> tuple[list[Item], int]:
    """時間枠 slot に投稿された新着を、全アカウント分まとめて古い順で返す。(新着, 失敗したアカウント数)"""
    items: list[Item] = []
    failed = 0
    for username in cfg["accounts"]:
        try:
            items += check_account(state, cfg, username, slot)
        except (XApiError, requests.RequestException) as e:
            log.error("X: @%s の取得に失敗: %s", username, e)
            failed += 1
    return sorted(items, key=lambda it: int(it.id)), failed
