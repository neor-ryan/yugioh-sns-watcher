"""遊戯王OCG公式サイトのニュース一覧を取得して新着を返す。

2026-10時点の構造: /japan/news/ に section.news-list が4つ（おしらせ・重要なおしらせ・
キャンペーン・ルール改訂）あり、各 li > a.news にタイトルと <time>YYYY/M/D</time>。
RSSは無し、robots.txt は404（制限なし）。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from . import Item

log = logging.getLogger(__name__)


def normalize_url(href: str, base: str) -> str:
    url = urljoin(base, href.strip())
    parts = urlsplit(url)
    # 公式サイトは "/japan/event//limitregulation/" のような二重スラッシュが混ざる
    path = re.sub(r"/{2,}", "/", parts.path)
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, parts.fragment))


def parse_news(html: str, base_url: str) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    seen: set[str] = set()
    for a in soup.select("section.news-list li a[href]"):
        time_tag = a.find("time")
        published = time_tag.get_text(strip=True) if time_tag else None
        if time_tag:
            time_tag.extract()
        title = " ".join(a.get_text(" ", strip=True).split())
        url = normalize_url(a["href"], base_url)
        if not title or url in seen:
            continue
        seen.add(url)
        items.append(Item(source="site", id=url, title=title, text=title, url=url, published=published))
    return items


def fetch_news(cfg: dict) -> list[Item]:
    url = cfg["news_url"]
    resp = requests.get(url, headers={"User-Agent": cfg["user_agent"]}, timeout=20)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    items = parse_news(resp.text, url)
    if not items:
        log.warning("公式サイトのニュースが0件でした。ページ構造が変わった可能性があります: %s", url)
    return items


def check(state: dict, cfg: dict) -> list[Item]:
    """新着を返し、state["site_seen_ids"] を更新する。初回は全件既読扱い。"""
    items = fetch_news(cfg)
    if not items:
        return []
    seen: list[str] = state.get("site_seen_ids") or []
    current_ids = [it.id for it in items]

    if not seen:
        log.info("公式サイト: 初回実行のため %d 件を既読扱いにします", len(items))
        state["site_seen_ids"] = current_ids
        return []

    seen_set = set(seen)
    new_items = [it for it in items if it.id not in seen_set]

    # 新しいIDを先頭に。ページに今載っているIDは件数上限を超えても消さない（消すと再通知になる）
    merged = list(dict.fromkeys([it.id for it in new_items] + seen))
    limit = max(cfg.get("seen_limit", 200), len(current_ids))
    on_page = set(current_ids)
    kept = [sid for sid in merged if sid in on_page]
    kept += [sid for sid in merged if sid not in on_page][: limit - len(kept)]
    state["site_seen_ids"] = kept
    return new_items
