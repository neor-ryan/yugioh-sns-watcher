"""自分のチャンネルの過去動画・コメント・視聴者維持率を読み、見どころシーンを探す。

- 動画一覧・コメント：YouTube Data API v3（APIキー YOUTUBE_API_KEY）
- 視聴者維持率：YouTube Analytics API（チャンネル所有者の許可が必要。YOUTUBE_CLIENT_ID /
  YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN。tools/google_auth.py で取得）
  未設定なら維持率は使わず、コメントのタイムスタンプだけで見どころを選ぶ。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date

import requests

log = logging.getLogger(__name__)

DATA_API = "https://www.googleapis.com/youtube/v3"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
TIMESTAMP_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")


@dataclass
class Video:
    id: str
    title: str
    published: str  # ISO8601
    duration: int  # 秒
    views: int
    thumbnail: str | None = None


@dataclass
class Comment:
    seconds: int
    text: str
    likes: int


@dataclass
class Highlight:
    seconds: int
    reason: str  # "both" | "comment" | "retention" | "none"
    lift: float | None = None  # 維持率が前後より何割高いか
    comments: list[Comment] = field(default_factory=list)  # 見どころ付近のコメント


def parse_duration(iso: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def mmss(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def _data_get(path: str, **params) -> dict:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY が設定されていません")
    resp = requests.get(f"{DATA_API}/{path}", params={**params, "key": key}, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"YouTube Data API {path} {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def list_videos(handle: str) -> list[Video]:
    ch = _data_get("channels", part="contentDetails", forHandle=handle)["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids: list[str] = []
    token = None
    while True:
        page = _data_get(
            "playlistItems", part="contentDetails", playlistId=uploads, maxResults=50, **({"pageToken": token} if token else {})
        )
        ids += [i["contentDetails"]["videoId"] for i in page["items"]]
        token = page.get("nextPageToken")
        if not token:
            break
    videos: list[Video] = []
    for i in range(0, len(ids), 50):
        for v in _data_get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i : i + 50]))["items"]:
            thumbs = v["snippet"].get("thumbnails", {})
            best = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium") if k in thumbs), None)
            videos.append(
                Video(
                    id=v["id"],
                    title=v["snippet"]["title"],
                    published=v["snippet"]["publishedAt"],
                    duration=parse_duration(v["contentDetails"]["duration"]),
                    views=int(v["statistics"].get("viewCount", 0)),
                    thumbnail=best,
                )
            )
    return videos


def timestamp_comments(video: Video, max_results: int = 100) -> list[Comment]:
    try:
        data = _data_get(
            "commentThreads", part="snippet", videoId=video.id, maxResults=max_results, order="relevance", textFormat="plainText"
        )
    except RuntimeError as e:  # コメント無効の動画など
        log.warning("コメントを取得できません（%s）: %s", video.id, e)
        return []
    out = []
    for th in data.get("items", []):
        s = th["snippet"]["topLevelComment"]["snippet"]
        for m in TIMESTAMP_RE.finditer(s["textDisplay"]):
            t = int(m.group(1)) * 60 + int(m.group(2))
            if 0 < t < video.duration:
                out.append(Comment(seconds=t, text=s["textDisplay"].strip(), likes=int(s.get("likeCount", 0))))
    return out


def _analytics_token() -> str | None:
    cid, secret, refresh = (os.environ.get(k) for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"))
    if not (cid and secret and refresh):
        return None
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={"client_id": cid, "client_secret": secret, "refresh_token": refresh, "grant_type": "refresh_token"},
        timeout=20,
    )
    if resp.status_code >= 400:
        log.error("YouTube Analytics の許可が無効です（tools/google_auth.py で取り直す）: %s", resp.text[:200])
        return None
    return resp.json()["access_token"]


def retention(video: Video, token: str) -> list[tuple[float, float]]:
    """[(動画内の位置 0〜1, 視聴維持の比率)] を100点。"""
    resp = requests.get(
        ANALYTICS_API,
        params={
            "ids": "channel==MINE",
            "startDate": "2015-01-01",
            "endDate": date.today().isoformat(),
            "metrics": "audienceWatchRatio",
            "dimensions": "elapsedVideoTimeRatio",
            "filters": f"video=={video.id}",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code >= 400:
        log.warning("視聴者維持率を取得できません（%s）: %s", video.id, resp.text[:200])
        return []
    return [(r[0], r[1]) for r in resp.json().get("rows", [])]


def retention_spikes(points: list[tuple[float, float]], duration: int, top: int = 4) -> list[tuple[int, float]]:
    """前後5点の平均より高く見られている地点 [(秒, 上昇率)]。冒頭5%・最後3%は除く。"""
    w = [p[1] for p in points]
    cands = []
    for i in range(5, len(w) - 3):
        around = w[max(0, i - 5) : i] + w[i + 1 : i + 6]
        base = sum(around) / len(around)
        if base > 0:
            cands.append((w[i] / base - 1, int(points[i][0] * duration)))
    cands.sort(reverse=True)
    picked: list[tuple[int, float]] = []
    for lift, t in cands:
        if lift <= 0.02:
            break
        if all(abs(t - p) > duration * 0.05 for p, _ in picked):
            picked.append((t, lift))
        if len(picked) == top:
            break
    return picked


def pick_highlight(video: Video, comments: list[Comment], spikes: list[tuple[int, float]], near: int = 45) -> Highlight:
    """維持率の山とコメントが重なる所 → いいねの多いコメント → 維持率の山 の順で選ぶ。"""
    def nearby(t: int) -> list[Comment]:
        return sorted((c for c in comments if abs(c.seconds - t) <= near), key=lambda c: -c.likes)

    both = [(t, lift, nearby(t)) for t, lift in spikes if nearby(t)]
    if both:
        t, lift, cs = max(both, key=lambda x: (sum(c.likes for c in x[2]), x[1]))
        return Highlight(seconds=cs[0].seconds, reason="both", lift=lift, comments=cs[:3])
    if comments:
        best = max(comments, key=lambda c: c.likes)
        return Highlight(seconds=best.seconds, reason="comment", comments=nearby(best.seconds)[:3])
    if spikes:
        t, lift = max(spikes, key=lambda x: x[1])
        return Highlight(seconds=t, reason="retention", lift=lift)
    return Highlight(seconds=0, reason="none")


def find_highlight(video: Video) -> Highlight:
    comments = timestamp_comments(video)
    spikes: list[tuple[int, float]] = []
    token = _analytics_token()
    if token:
        spikes = retention_spikes(retention(video, token), video.duration)
    return pick_highlight(video, comments, spikes)


def scene_url(video: Video, seconds: int, lead: int = 5) -> str:
    """見どころの少し前から再生するリンク。"""
    t = max(0, seconds - lead)
    return f"https://youtu.be/{video.id}" + (f"?t={t}" if t else "")
