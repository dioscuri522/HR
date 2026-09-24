#!/usr/bin/env python3
"""stand.fm のエンドポイント構造を実地調査する（第4次）。

第3次で判明した決定的な手がかり:
  JSバンドルの中に API ルートのテンプレートが埋まっていた。
    /channels/{channel_id}/appending        ← 追加読み込み用と推測される
    /channels/{channel_id}/latest_announcement
    /episodes/{episode_id}/playback
  ベースURLは "}/api/" というテンプレート結合で組み立てられている。

あわせて、エピソードページのHTMLには24桁IDが494件埋まっていた
（チャンネルページは23件のみ）。ここから列挙できる可能性もある。

クエリパラメータによるページングは16通り試して全滅（応答が完全に同一）。
本スクリプトは appending ルートの検証に集中する。
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

CHANNEL_ID = os.environ.get("STANDFM_CHANNEL_ID", "5f5edcc0f04555115dfb0845")
S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
})

BASE = f"https://stand.fm/api/channels/{CHANNEL_ID}"
OBJECT_ID_RE = re.compile(r"\b[0-9a-f]{24}\b")


def section(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70, flush=True)


def fetch(url: str, method: str = "GET", quiet: bool = False, **kw):
    try:
        res = S.request(method, url, timeout=30, **kw)
    except requests.RequestException as exc:
        print(f"[NG ] {method} {url} -> {type(exc).__name__}")
        return None
    finally:
        time.sleep(0.4)
    if not quiet:
        ctype = res.headers.get("content-type", "?").split(";")[0]
        print(f"[{res.status_code}] {method} {url.replace(BASE, '…')}  {len(res.content)}B  {ctype}")
    return res


def as_json(res):
    if res is None or res.status_code != 200 or "json" not in res.headers.get("content-type", ""):
        return None
    try:
        return res.json()
    except ValueError:
        return None


def eps_of(data) -> dict:
    return ((data or {}).get("response") or {}).get("episodes") or {}


base_data = as_json(fetch(BASE, quiet=True)) or {}
base_eps = eps_of(base_data)
ids = list(base_eps)
oldest = ids[-1] if ids else ""
oldest_ts = base_eps.get(oldest, {}).get("publishedAt", 0) if oldest else 0
print(f"基準: {len(ids)} 件 / 最古ID={oldest} / publishedAt={oldest_ts}")

# ---------------------------------------------------------------- A
section("A. /api/channels/{id}/appending の検証")
for suffix in [
    "",
    f"?lastEpisodeId={oldest}",
    f"?episodeId={oldest}",
    f"?publishedAt={oldest_ts}",
    f"?lastPublishedAt={oldest_ts}",
    f"?limit=50&lastEpisodeId={oldest}",
    f"?limit=50&publishedAt={oldest_ts}",
    f"?cursor={oldest}",
    f"?offset=10",
]:
    url = f"{BASE}/appending{suffix}"
    res = fetch(url, headers={"Accept": "application/json"})
    data = as_json(res)
    if data is None:
        continue
    page = eps_of(data)
    new = set(page) - set(ids)
    print(f"    -> {len(page)} 件 / 新規 {len(new)} 件 / "
          f"hasNext={(data.get('response') or {}).get('hasNextEpisode')}"
          f"{'  ★★★ 新規あり' if new else ''}")
    if not page:
        print(f"    トップレベルキー: {list(data)[:10]} / response キー: {list((data.get('response') or {}))[:15]}")

print("--- POST でも試す ---")
for body in [{}, {"lastEpisodeId": oldest}, {"channelId": CHANNEL_ID, "lastEpisodeId": oldest}]:
    res = fetch(f"{BASE}/appending", method="POST", json=body,
                headers={"Accept": "application/json"})
    data = as_json(res)
    if data is not None:
        page = eps_of(data)
        print(f"    body={list(body)} -> {len(page)} 件 / 新規 {len(set(page) - set(ids))} 件")

# ---------------------------------------------------------------- B
section("B. エピソード詳細に前後エピソードへの導線があるか")
detail = as_json(fetch(f"https://stand.fm/api/episodes/{oldest}"))
if detail:
    resp = detail.get("response") or detail
    print(f"    トップレベルキー: {list(detail)}")
    print(f"    response のキー: {list(resp)[:25]}")
    blob = json.dumps(detail, ensure_ascii=False)
    for key in sorted(set(re.findall(r'"([A-Za-z_]*(?:[Nn]ext|[Pp]rev|[Rr]elated|[Nn]eighbo)[A-Za-z_]*)"', blob))):
        print(f"    ★ 前後を示唆するキー: {key}")
    ep_block = (resp.get("episodes") or {}) if isinstance(resp, dict) else {}
    print(f"    response.episodes に含まれる件数: {len(ep_block)}")
    if ep_block:
        for eid, ev in list(ep_block.items())[:6]:
            print(f"      {eid} {str(ev.get('title'))[:40]}")

# ---------------------------------------------------------------- C
section("C. エピソードページHTMLの24桁IDは本当にエピソードIDか")
res = fetch(f"https://stand.fm/episodes/{oldest}")
html_ids: list[str] = []
if res is not None and res.status_code == 200:
    html_ids = sorted(set(OBJECT_ID_RE.findall(res.text)))
    print(f"    HTML内の24桁ID: {len(html_ids)} 件")
    hit = miss = 0
    for cand in html_ids[:12]:
        r = fetch(f"https://stand.fm/api/episodes/{cand}", quiet=True)
        d = as_json(r)
        ok = bool(d) and bool(eps_of(d))
        ch = ""
        if ok:
            first = next(iter(eps_of(d).values()))
            ch = first.get("channelId", "")
            hit += 1
        else:
            miss += 1
        print(f"      {cand} -> {'エピソード' if ok else '×'}"
              f"{' (当チャンネル)' if ch == CHANNEL_ID else (f' (他: {ch})' if ch else '')}")
    print(f"    12件中 エピソード {hit} 件 / それ以外 {miss} 件")

# ---------------------------------------------------------------- D
section("D. その他の判明ルート")
for url in [
    f"{BASE}/latest_announcement",
    f"{BASE}/announcements",
    f"https://stand.fm/api/episodes/{oldest}/playback",
]:
    res = fetch(url, headers={"Accept": "application/json"})
    data = as_json(res)
    if data is not None:
        print(f"    キー: {list(data)[:8]} / {json.dumps(data, ensure_ascii=False)[:200]}")
