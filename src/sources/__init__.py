from dataclasses import dataclass


@dataclass
class Item:
    """監視ソースから取得した新着1件。"""

    source: str  # "x" | "site"
    id: str
    title: str
    text: str
    url: str
    published: str | None = None
