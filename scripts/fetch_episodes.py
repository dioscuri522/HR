#!/usr/bin/env python3
"""stand.fm のチャンネルから全エピソードのメタデータを取得する。

実地調査（scripts/probe.py、第1〜5次）で確定した経路:

    GET /api/channels/{channelId}/appending?limit=2000
        → response.episodes に全エピソード（1033件）が入り、
          hasNextEpisode は False になる。カーソルは不要。

  判明している注意点:
    - 公開RSSは存在しない（channelRssUrl は null、/rss 系はすべて404）
    - /api/channels/{id}（appending なし）は limit を無視して常に最新10件
    - totalDuration の単位はミリ秒
    - episodes は dict で、キーの並びは publishedAt の昇順
    - 音声URLはこの応答に含まれない。各エピソードの
      /api/episodes/{id} から取得する（scripts/run.py が実行時に解決）
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CHANNEL_ID = os.environ.get("STANDFM_CHANNEL_ID", "5f5edcc0f04555115dfb0845")
LIMIT = int(os.environ.get("FETCH_LIMIT", "2000"))
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT_SEC", "60"))

UA = os.environ.get(
    "FETCH_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)
APPEND_URL = f"https://stand.fm/api/channels/{CHANNEL_ID}/appending"


def iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "ja,en;q=0.8",
    })

    print(f"取得: {APPEND_URL}?limit={LIMIT}")
    res = session.get(APPEND_URL, params={"limit": LIMIT}, timeout=TIMEOUT)
    res.raise_for_status()
    body = res.json().get("response") or {}
    raw = body.get("episodes") or {}
    has_next = bool(body.get("hasNextEpisode"))
    print(f"応答: {len(raw)} 件 / hasNextEpisode={has_next} / {len(res.content)} バイト")

    if has_next:
        # limit が足りていない。取りこぼしたまま進めると分析の土台が欠ける。
        print(f"警告: hasNextEpisode が True のまま。FETCH_LIMIT={LIMIT} を増やすこと。")

    episodes = []
    for ep_id, ep in raw.items():
        duration_ms = ep.get("totalDuration") or 0
        episodes.append({
            "id": ep_id,
            "title": ep.get("title") or "",
            "description": ep.get("description") or "",
            "published_at": iso(ep.get("publishedAt")),
            "duration_sec": round(duration_ms / 1000, 1),
            "page_url": f"https://stand.fm/episodes/{ep_id}",
            "is_supporter_only": bool(ep.get("isSupporterOnly")),
            "is_unlisted": bool(ep.get("isUnlisted")),
            "require_content_ticket": bool(ep.get("requireContentTicket")),
            "comment_count": ep.get("commentCount"),
        })

    episodes.sort(key=lambda e: (e["published_at"] or "", e["id"]))

    # 既存メタデータとマージし、過去に取得した情報を消さない
    path = DATA / "episodes.json"
    merged: dict[str, dict] = {}
    if path.exists():
        for ep in json.loads(path.read_text(encoding="utf-8")):
            merged[ep["id"]] = ep
    for ep in episodes:
        merged[ep["id"]] = {**merged.get(ep["id"], {}), **ep}
    ordered = sorted(merged.values(), key=lambda e: (e.get("published_at") or "", e["id"]))
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_sec = sum(e.get("duration_sec") or 0 for e in ordered)
    restricted = sum(
        1 for e in ordered
        if e.get("is_supporter_only") or e.get("is_unlisted") or e.get("require_content_ticket")
    )
    report = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "channel_id": CHANNEL_ID,
        "endpoint": f"{APPEND_URL}?limit={LIMIT}",
        "episode_count": len(ordered),
        "has_next_episode": has_next,
        "total_hours": round(total_sec / 3600, 1),
        "mean_minutes": round(total_sec / 60 / max(len(ordered), 1), 1),
        "oldest": ordered[0]["published_at"] if ordered else None,
        "newest": ordered[-1]["published_at"] if ordered else None,
        "restricted_count": restricted,
    }
    (DATA / "fetch_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
