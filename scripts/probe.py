#!/usr/bin/env python3
"""stand.fm のエンドポイント構造を実地調査する（第2次）。

第1次調査で判明した事実:
  - 公開RSSは存在しない（/rss 系はすべて404、channelRssUrl も null）
  - /api/channels/{id} が 200 で JSON を返し、response.episodes に
    エピソードのメタデータが入る（キーがエピソードID）
  - ただしその応答に音声URLは含まれない
  - response.hasNextEpisode = true → ページングが存在する

本スクリプトで確定させたいのは次の3点:
  A. エピソードのフィールド全容と尺の分布（処理時間の見積もりに要る）
  B. 全1033件を辿るためのページング方式
  C. 音声URLを返すエンドポイント

出力は tail で全体が読めるよう簡潔に保つ。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time

import requests

CHANNEL_ID = os.environ.get("STANDFM_CHANNEL_ID", "5f5edcc0f04555115dfb0845")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})

BASE = f"https://stand.fm/api/channels/{CHANNEL_ID}"
URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")
API_PATH_RE = re.compile(r"[\"'](/api/[A-Za-z0-9/_.\-]+)[\"']")
AUDIO_RE = re.compile(r"\.(m4a|mp3|aac|wav|m3u8)|/audio|/sounds?/", re.I)


def section(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70, flush=True)


def fetch(url: str, **kw):
    try:
        res = S.get(url, timeout=30, **kw)
    except requests.RequestException as exc:
        print(f"[NG ] {url} -> {type(exc).__name__}")
        return None
    finally:
        time.sleep(0.6)
    ctype = res.headers.get("content-type", "?").split(";")[0]
    print(f"[{res.status_code}] {url}  {len(res.content)}B  {ctype}")
    return res


def as_json(res):
    if res is None or res.status_code != 200:
        return None
    if "json" not in res.headers.get("content-type", ""):
        return None
    try:
        return res.json()
    except ValueError:
        return None


def episodes_of(data) -> dict:
    if not isinstance(data, dict):
        return {}
    return (data.get("response") or {}).get("episodes") or {}


def audio_urls_in(text: str) -> list[str]:
    return sorted({u for u in URL_RE.findall(text) if AUDIO_RE.search(u)})


# ---------------------------------------------------------------- A
section("A. エピソードのフィールド全容と尺の分布")
first = as_json(fetch(BASE))
eps = episodes_of(first)
print(f"エピソード件数（1ページ目）: {len(eps)}")
print(f"hasNextEpisode: {(first or {}).get('response', {}).get('hasNextEpisode')}")
if eps:
    sample = next(iter(eps.values()))
    print("--- 1件分の全フィールド ---")
    print(json.dumps(sample, ensure_ascii=False, indent=2)[:2500])
    durations = [e.get("totalDuration") or 0 for e in eps.values()]
    print("--- 尺（秒） ---")
    print(f"  件数 {len(durations)} / 合計 {sum(durations):.0f} / 平均 {statistics.mean(durations):.0f}")
    print(f"  中央値 {statistics.median(durations):.0f} / 最小 {min(durations):.0f} / 最大 {max(durations):.0f}")
    flags = {
        "isSupporterOnly": sum(1 for e in eps.values() if e.get("isSupporterOnly")),
        "isUnlisted": sum(1 for e in eps.values() if e.get("isUnlisted")),
        "requireContentTicket": sum(1 for e in eps.values() if e.get("requireContentTicket")),
    }
    print(f"--- 取得できない可能性のあるもの: {flags} ---")

# ---------------------------------------------------------------- B
section("B. ページング方式の特定")
ids = list(eps.keys())
last_id = ids[-1] if ids else ""
oldest_ts = min((e.get("publishedAt") or 0) for e in eps.values()) if eps else 0
for params in [
    f"?limit=100",
    f"?limit=50&lastEpisodeId={last_id}",
    f"?lastEpisodeId={last_id}",
    f"?cursor={last_id}",
    f"?offset={len(ids)}",
    f"?page=2",
    f"?before={oldest_ts}",
    f"?publishedAt={oldest_ts}",
    f"?maxPublishedAt={oldest_ts}",
]:
    data = as_json(fetch(BASE + params, headers={"Accept": "application/json"}))
    page = episodes_of(data)
    new = set(page) - set(ids)
    print(f"    -> {len(page)} 件 / 1ページ目と重複しないもの {len(new)} 件 / "
          f"hasNext={(data or {}).get('response', {}).get('hasNextEpisode')}")

# ---------------------------------------------------------------- C
section("C. 音声URLを返すエンドポイントの特定")
target = ids[0] if ids else ""
print(f"対象エピソードID: {target}")
for url in [
    f"https://stand.fm/api/episodes/{target}",
    f"https://stand.fm/api/episode/{target}",
    f"https://stand.fm/api/episodes/{target}/audio",
    f"https://stand.fm/api/episodes/{target}/play",
    f"https://stand.fm/api/channels/{CHANNEL_ID}/episodes/{target}",
]:
    res = fetch(url, headers={"Accept": "application/json"})
    if res is None or res.status_code != 200:
        continue
    found = audio_urls_in(res.text)
    print(f"    -> 音声らしきURL {len(found)} 件: {found[:5]}")
    data = as_json(res)
    if data is not None and not found:
        blob = json.dumps(data, ensure_ascii=False)
        print(f"    -> URL全件: {sorted(set(URL_RE.findall(blob)))[:12]}")

print("--- エピソードページ(HTML) ---")
res = fetch(f"https://stand.fm/episodes/{target}")
if res is not None and res.status_code == 200:
    html = res.text
    print(f"    音声らしきURL: {audio_urls_in(html)[:5]}")
    for m in re.findall(r'<meta[^>]+(?:property|name)="([^"]+)"[^>]+content="([^"]*)"', html)[:25]:
        if any(k in m[0] for k in ("audio", "og:", "twitter:player")):
            print(f"    meta {m[0]} = {m[1][:120]}")

# ---------------------------------------------------------------- D
section("D. JSバンドル内の /api/ パス一覧（本当の経路はここに書かれている）")
res = fetch(f"https://stand.fm/channels/{CHANNEL_ID}")
paths: set[str] = set()
if res is not None and res.status_code == 200:
    srcs = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', res.text)))
    print(f"バンドル数: {len(srcs)}")
    for src in srcs[:8]:
        url = src if src.startswith("http") else f"https://stand.fm{src}"
        js = fetch(url)
        if js is not None and js.status_code == 200:
            paths |= set(API_PATH_RE.findall(js.text))
print(f"--- 検出された /api/ パス {len(paths)} 件 ---")
for path in sorted(paths)[:100]:
    print("  " + path)
