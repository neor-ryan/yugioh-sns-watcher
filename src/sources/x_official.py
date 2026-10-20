"""遊戯王OCG公式X（@YuGiOh_OCG_INFO）の新着ポストを X API v2 で取得する。

読み取りは返ってきた投稿数で課金されるため、必ず since_id を付けて新着だけを取る。
エンドポイント（2026-10時点 docs.x.com）:
  GET https://api.x.com/2/users/by/username/{username}
  GET https://api.x.com/2/users/{id}/tweets?since_id=...&max_results=5..100
"""
from __future__ import annotations

import logging
import os

import requests

from . import Item

log = logging.getLogger(__name__)


class XApiError(RuntimeError):
    pass


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


def resolve_user_id(state: dict, cfg: dict) -> str:
    if state.get("x_user_id"):
        return state["x_user_id"]
    data = _get(cfg, f"/users/by/username/{cfg['username']}")
    user_id = data["data"]["id"]
    state["x_user_id"] = user_id
    log.info("X: @%s のユーザーIDは %s", cfg["username"], user_id)
    return user_id


def _post_text(post: dict) -> str:
    # 長文ポストは note_post（旧名 note_tweet）に全文が入る
    for key in ("note_post", "note_tweet"):
        note = post.get(key)
        if isinstance(note, dict) and note.get("text"):
            return note["text"]
    return post.get("text", "")


def check(state: dict, cfg: dict) -> list[Item]:
    """新着を古い順で返し、state["x_since_id"] を更新する。初回は最新IDだけ記録して通知しない。"""
    user_id = resolve_user_id(state, cfg)
    since_id = state.get("x_since_id")
    params = {
        "max_results": cfg.get("max_results", 20) if since_id else 5,
        cfg.get("fields_param", "post.fields"): "created_at,note_post",
    }
    if cfg.get("exclude"):
        params["exclude"] = ",".join(cfg["exclude"])

    if not since_id:
        data = _get(cfg, f"/users/{user_id}/tweets", params)
        newest = data.get("meta", {}).get("newest_id")
        if newest:
            state["x_since_id"] = newest
            log.info("X: 初回実行のため since_id=%s から監視を始めます", newest)
        return []

    params["since_id"] = since_id
    posts: list[dict] = []
    newest_id = since_id
    pages = 0
    while True:
        data = _get(cfg, f"/users/{user_id}/tweets", params)
        meta = data.get("meta", {})
        posts.extend(data.get("data", []))
        if pages == 0 and meta.get("newest_id"):
            newest_id = meta["newest_id"]
        pages += 1
        token = meta.get("next_token")
        if not token or pages >= cfg.get("max_pages", 3):
            break
        params["pagination_token"] = token

    state["x_since_id"] = newest_id
    items = []
    for post in sorted(posts, key=lambda p: int(p["id"])):
        text = _post_text(post)
        items.append(
            Item(
                source="x",
                id=post["id"],
                title=text.splitlines()[0][:80] if text else "",
                text=text,
                url=f"https://x.com/{cfg['username']}/status/{post['id']}",
                published=post.get("created_at"),
            )
        )
    return items
