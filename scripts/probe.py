#!/usr/bin/env python3
"""stand.fm のページングを実地検証する（第5次・最終）。

第4次の結果:
  GET /api/channels/{id}/appending?limit=50&lastEpisodeId=X → 50件（新規40件）
  limit が効くことは確定した。

ただし前回はカーソルに「最新のエピソードID」を渡していた。
episodes は dict であり、キーの並び順は publishedAt の昇順だった
（ids[0] が最古、ids[-1] が最新）。カーソルには最古のIDを渡すべきなので、
本スクリプトでは publishedAt で明示的にソートして最小値を使う。

検証するのは次の2点だけ:
  A. limit の上限はどこか
  B. カーソルを正しく渡せば本当に全1033件を辿れるか
"""
from __future__ import annotations

import os
import time

import requests

CHANNEL_ID = os.environ.get("STANDFM_CHANNEL_ID", "5f5edcc0f04555115dfb0845")
S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "ja,en;q=0.8",
})
APPEND = f"https://stand.fm/api/channels/{CHANNEL_ID}/appending"


def section(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70, flush=True)


def page(**params) -> tuple[dict, bool]:
    """1ページ取得して (エピソードdict, hasNextEpisode) を返す。"""
    try:
        res = S.get(APPEND, params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"  [NG] {params} -> {type(exc).__name__}")
        return {}, False
    finally:
        time.sleep(0.5)
    if res.status_code != 200 or "json" not in res.headers.get("content-type", ""):
        print(f"  [{res.status_code}] {params}")
        return {}, False
    body = res.json().get("response") or {}
    return body.get("episodes") or {}, bool(body.get("hasNextEpisode"))


def oldest_of(eps: dict) -> tuple[str, int]:
    """publishedAt が最小のエピソードの (id, publishedAt) を返す。"""
    if not eps:
        return "", 0
    eid = min(eps, key=lambda k: eps[k].get("publishedAt") or 0)
    return eid, eps[eid].get("publishedAt") or 0


# ---------------------------------------------------------------- A
section("A. limit の上限")
for limit in (10, 50, 100, 200, 500, 1200):
    eps, has_next = page(limit=limit)
    print(f"  limit={limit:<5} -> {len(eps):<5} 件 / hasNext={has_next}")

# ---------------------------------------------------------------- B
section("B. カーソルを最古IDにしてページングを実走")
for cursor_name in ("lastEpisodeId", "publishedAt"):
    print(f"\n--- カーソル: {cursor_name} ---")
    seen: dict[str, dict] = {}
    eps, has_next = page(limit=50)
    seen.update(eps)
    cursor_id, cursor_ts = oldest_of(eps)
    print(f"  1ページ目: {len(eps)} 件 / 累計 {len(seen)} / 最古 {cursor_id}")

    for i in range(2, 12):
        value = cursor_id if cursor_name == "lastEpisodeId" else cursor_ts
        eps, has_next = page(limit=50, **{cursor_name: value})
        new = set(eps) - set(seen)
        seen.update(eps)
        print(f"  {i}ページ目: {len(eps)} 件 / 新規 {len(new)} / 累計 {len(seen)} / hasNext={has_next}")
        if not new:
            print("  → 新規が出なくなったため打ち切り")
            break
        cursor_id, cursor_ts = oldest_of(eps)
        if not has_next:
            print("  → hasNextEpisode が False になった")
            break

    if seen:
        oldest_id, oldest_ts = oldest_of(seen)
        newest_ts = max((e.get("publishedAt") or 0) for e in seen.values())
        import datetime as dt
        fmt = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        print(f"  結果: 累計 {len(seen)} 件 / 期間 {fmt(oldest_ts)} 〜 {fmt(newest_ts)}")
