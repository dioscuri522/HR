#!/usr/bin/env python3
"""stand.fm のエンドポイント構造を実地調査する（第3次）。

第2次までに確定した事実:
  - GET /api/episodes/{episodeId} が 200 JSON を返し、その中に
    https://cdncf.stand.fm/audios/XXXX.m4a と playlist.m3u8 が含まれる
    → 音声の取得経路はこれで確定
  - totalDuration の単位はミリ秒（653232 = 約11分）
  - /api/channels/{id} は常に最新10件しか返さない。
    limit / offset / page / cursor / lastEpisodeId / before など9通りを
    試したが、どれもレスポンスが完全に同一（24881B）だった
  - JSバンドルから /api/ パスを素朴な正規表現で抽出できなかった
    （URLを動的に組み立てているため）

残る唯一の課題は「1033件のエピソードIDをどう列挙するか」。
本スクリプトはその経路を特定することだけに集中する。
"""
from __future__ import annotations

import json
import os
import re
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
OBJECT_ID_RE = re.compile(r"\b[0-9a-f]{24}\b")
URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")


def section(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70, flush=True)


def fetch(url: str, method: str = "GET", **kw):
    try:
        res = S.request(method, url, timeout=30, **kw)
    except requests.RequestException as exc:
        print(f"[NG ] {method} {url} -> {type(exc).__name__}")
        return None
    finally:
        time.sleep(0.5)
    ctype = res.headers.get("content-type", "?").split(";")[0]
    print(f"[{res.status_code}] {method} {url}  {len(res.content)}B  {ctype}")
    return res


def as_json(res):
    if res is None or res.status_code != 200 or "json" not in res.headers.get("content-type", ""):
        return None
    try:
        return res.json()
    except ValueError:
        return None


def paths_of(obj, prefix="", depth=0, out=None):
    out = [] if out is None else out
    if depth > 5:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out.append(f"{p}: {type(v).__name__}[{len(v)}]")
                paths_of(v, p, depth + 1, out)
            else:
                out.append(f"{p} = {repr(v)[:90]}")
    elif isinstance(obj, list) and obj:
        out.append(f"{prefix}[0]:")
        paths_of(obj[0], f"{prefix}[0]", depth + 1, out)
    return out


channel = as_json(fetch(BASE)) or {}
eps = (channel.get("response") or {}).get("episodes") or {}
ids = list(eps)
newest, oldest = (ids[0], ids[-1]) if ids else ("", "")
print(f"1ページ目のID: 先頭={newest} 末尾={oldest}")

# ---------------------------------------------------------------- A
section("A. /api/episodes/{id} の全構造（前後エピソードへの導線を探す）")
data = as_json(fetch(f"https://stand.fm/api/episodes/{oldest}"))
if data:
    for line in paths_of(data)[:150]:
        print("  " + line)

# ---------------------------------------------------------------- B
section("B. sitemap / 代替経路")
for url in [
    "https://stand.fm/sitemap.xml",
    "https://stand.fm/sitemap_index.xml",
    "https://stand.fm/robots.txt",
    f"https://stand.fm/channels/{CHANNEL_ID}/episodes",
    f"https://stand.fm/channels/{CHANNEL_ID}?page=2",
]:
    res = fetch(url)
    if res is not None and res.status_code == 200:
        print("    冒頭: " + res.text[:300].replace("\n", " "))

# ---------------------------------------------------------------- C
section("C. HTML に何件のエピソードIDが埋まっているか")
for name, url in [
    ("チャンネルページ", f"https://stand.fm/channels/{CHANNEL_ID}"),
    ("エピソードページ", f"https://stand.fm/episodes/{oldest}"),
]:
    res = fetch(url)
    if res is None or res.status_code != 200:
        continue
    found = sorted(set(OBJECT_ID_RE.findall(res.text)))
    audio = sorted({u for u in URL_RE.findall(res.text) if ".m4a" in u})
    print(f"    {name}: 24桁ID {len(found)} 件 / m4a URL {len(audio)} 件")
    print(f"      ID例: {found[:8]}")

# ---------------------------------------------------------------- D
section("D. JSバンドル内の URL 組み立てパターン")
res = fetch(f"https://stand.fm/channels/{CHANNEL_ID}")
if res is not None and res.status_code == 200:
    srcs = [s for s in re.findall(r'<script[^>]+src="([^"]+)"', res.text) if "stand.fm" in s or s.startswith("/")]
    hits: set[str] = set()
    for src in srcs:
        url = src if src.startswith("http") else f"https://stand.fm{src}"
        js = fetch(url)
        if js is None or js.status_code != 200:
            continue
        # "api/..." を含む文字列リテラルと、その周辺のテンプレート結合を拾う
        for m in re.findall(r'["\'`][^"\'`\n]{0,50}api/[^"\'`\n]{0,70}["\'`]', js.text):
            hits.add(m)
        for m in re.findall(r'["\'`]/?(?:channels|episodes)/[^"\'`\n]{0,60}["\'`]', js.text):
            hits.add(m)
    print(f"--- 検出 {len(hits)} 件 ---")
    for h in sorted(hits)[:120]:
        print("  " + h)

# ---------------------------------------------------------------- E
section("E. ページング: パラメータ名の総当たり（応答サイズで判定）")
base_len = len(json.dumps(channel, ensure_ascii=False))
print(f"基準の応答長: {base_len}")
params = [
    "limit=100", "count=100", "perPage=100", "size=100", "pageSize=100",
    f"lastEpisodeId={oldest}", f"nextToken={oldest}", f"startAfter={oldest}",
    f"skip=10", f"from=10", f"start=10",
    f"publishedAtBefore={eps.get(oldest, {}).get('publishedAt', 0)}",
    f"lastPublishedAt={eps.get(oldest, {}).get('publishedAt', 0)}",
    f"episodeId={oldest}",
    "includeAllEpisodes=true", "all=true",
]
for param in params:
    res = fetch(f"{BASE}?{param}", headers={"Accept": "application/json"})
    got = as_json(res)
    if got is None:
        continue
    page = (got.get("response") or {}).get("episodes") or {}
    new = set(page) - set(ids)
    marker = "  ★変化あり" if (len(page) != len(ids) or new) else ""
    print(f"    -> {len(page)} 件 / 新規 {len(new)} 件{marker}")
