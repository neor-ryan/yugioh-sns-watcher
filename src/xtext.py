"""X（twitter-text）と同じ数え方で文字数を測る。

日本語など全角は1文字＝2、半角は1、URLは長さに関係なく23。上限は280（日本語だけなら実質140字）。
"""
from __future__ import annotations

import re

LIMIT = 280
URL_WEIGHT = 23
URL_RE = re.compile(r"https?://\S+")


def _weight(ch: str) -> int:
    c = ord(ch)
    if c <= 0x10FF or 0x2000 <= c <= 0x200D or 0x2010 <= c <= 0x201F or 0x2032 <= c <= 0x2037:
        return 1
    return 2


def x_length(text: str) -> int:
    urls = URL_RE.findall(text)
    rest = URL_RE.sub("", text)
    return sum(_weight(ch) for ch in rest) + URL_WEIGHT * len(urls)
