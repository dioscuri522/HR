#!/usr/bin/env python3
"""stand.fm のエンドポイント構造を実地調査し、結果をログに出す。

開発環境から stand.fm に到達できないため、構造の確認は Actions 上で行うしかない。
このスクリプトは取得したレスポンスの中身をログに直接出力し、
そこから本実装の取得ロジックを確定させるためのもの。
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

URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+")
API_PATH_RE = re.compile(r"[\"'](/api/[A-Za-z0-9/_.\-{}$\[\]]+)[\"']")


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def fetch(url: str, **kw):
    try:
        res = S.get(url, timeout=30, **kw)
    except requests.RequestException as exc:
        print(f"[NG] {url} -> {type(exc).__name__}: {exc}")
        return None
    finally:
        time.sleep(0.8)
    print(f"[{res.status_code}] {url}  {len(res.content)}B  {res.headers.get('content-type','?')}")
    return res


def key_tree(obj, prefix: str = "", depth: int = 0, out: list | None = None) -> list:
    """JSON の構造を「パス: 型（サンプル）」の一覧にする。"""
    out = [] if out is None else out
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                size = len(value)
                out.append(f"{path}: {type(value).__name__}[{size}]")
                key_tree(value, path, depth + 1, out)
            else:
                sample = repr(value)
                out.append(f"{path}: {type(value).__name__} = {sample[:110]}")
    elif isinstance(obj, list):
        out.append(f"{prefix}[]: {len(obj)} 要素")
        if obj:
            key_tree(obj[0], f"{prefix}[0]", depth + 1, out)
    return out


def report_json(name: str, data) -> None:
    print(f"--- {name} の構造 ---")
    for line in key_tree(data)[:400]:
        print("  " + line)
    blob = json.dumps(data, ensure_ascii=False)
    urls = sorted(set(URL_RE.findall(blob)))
    audio = [u for u in urls if re.search(r"\.(m4a|mp3|aac|wav|m3u8)|/audio", u, re.I)]
    print(f"--- URL 総数 {len(urls)} / 音声らしきもの {len(audio)} ---")
    for u in audio[:20]:
        print("  AUDIO " + u)
    for u in urls[:40]:
        print("  URL   " + u)


def main() -> int:
    # 1. チャンネルAPI（前回 200 を返した唯一のJSONエンドポイント）
    section("1. /api/channels/{id}")
    res = fetch(f"https://stand.fm/api/channels/{CHANNEL_ID}")
    channel_json = None
    if res is not None and res.status_code == 200:
        try:
            channel_json = res.json()
            report_json("channel", channel_json)
        except ValueError:
            print("  JSON として解釈できず:", res.text[:500])

    # 2. チャンネルページ HTML の中身
    section("2. チャンネルページ HTML")
    res = fetch(f"https://stand.fm/channels/{CHANNEL_ID}")
    html = res.text if res is not None and res.status_code == 200 else ""
    if html:
        for marker in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "window.__", "buildId"):
            print(f"  マーカー {marker}: {'あり' if marker in html else 'なし'}")
        build = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if build:
            print(f"  buildId = {build.group(1)}")
        print("  --- script src 一覧 ---")
        for src in sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))[:30]:
            print("    " + src)
        print("  --- HTML 内の /api/ パス ---")
        for path in sorted(set(API_PATH_RE.findall(html)))[:40]:
            print("    " + path)
        audio_urls = sorted(set(u for u in URL_RE.findall(html)
                                if re.search(r"\.(m4a|mp3|aac|m3u8)|/audio", u, re.I)))
        print(f"  --- HTML 内の音声らしきURL {len(audio_urls)} 件 ---")
        for u in audio_urls[:20]:
            print("    " + u)
        # OGP や JSON-LD にエピソード情報が載ることがある
        for m in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)[:3]:
            print("  --- JSON-LD ---")
            print("    " + m.strip()[:1500])
        print("  --- HTML 冒頭 1500 文字 ---")
        print(html[:1500])

    # 3. JS バンドルから API パスを抽出（SPA なら本当の経路はここに書かれている）
    section("3. JS バンドル内の API パス")
    if html:
        srcs = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))
        for src in srcs[:6]:
            url = src if src.startswith("http") else f"https://stand.fm{src}"
            res = fetch(url)
            if res is None or res.status_code != 200:
                continue
            paths = sorted(set(API_PATH_RE.findall(res.text)))
            print(f"  {len(paths)} 件の /api/ パスを検出:")
            for path in paths[:60]:
                print("    " + path)

    # 4. エピソード取得エンドポイントの候補を総当たり
    section("4. エピソード系エンドポイントの候補")
    candidates = [
        f"https://stand.fm/api/channels/{CHANNEL_ID}/publicEpisodes",
        f"https://stand.fm/api/channels/{CHANNEL_ID}/episode",
        f"https://stand.fm/api/episodes?channelId={CHANNEL_ID}",
        f"https://stand.fm/api/episodes/channel/{CHANNEL_ID}",
        f"https://stand.fm/api/channel/{CHANNEL_ID}/episodes",
        f"https://stand.fm/api/channels/{CHANNEL_ID}?withEpisodes=true",
        f"https://stand.fm/api/channels/{CHANNEL_ID}/episodes?limit=20",
        f"https://stand.fm/api/v2/channels/{CHANNEL_ID}",
        f"https://stand.fm/api/channels/{CHANNEL_ID}/feed",
    ]
    for url in candidates:
        res = fetch(url, headers={"Accept": "application/json"})
        if res is not None and res.status_code == 200 and "json" in res.headers.get("content-type", ""):
            try:
                report_json(url, res.json())
            except ValueError:
                pass

    # 5. Next.js のデータルート
    section("5. Next.js データルート")
    if html:
        build = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if build:
            bid = build.group(1)
            for path in (
                f"https://stand.fm/_next/data/{bid}/channels/{CHANNEL_ID}.json",
                f"https://stand.fm/_next/data/{bid}/ja/channels/{CHANNEL_ID}.json",
            ):
                res = fetch(path)
                if res is not None and res.status_code == 200:
                    try:
                        report_json(path, res.json())
                    except ValueError:
                        pass
        else:
            print("  buildId が取れなかったため省略")

    # 6. エピソード個別ページ（チャンネルAPIからIDが取れた場合）
    section("6. エピソード個別ページ")
    ep_ids = []
    if channel_json is not None:
        blob = json.dumps(channel_json, ensure_ascii=False)
        ep_ids = sorted(set(re.findall(r'"(?:episodeId|episode_id)"\s*:\s*"([^"]+)"', blob)))
        ep_ids += sorted(set(re.findall(r"stand\.fm/episodes/([A-Za-z0-9_-]+)", blob)))
    print(f"  チャンネルJSONから拾えたエピソードID: {ep_ids[:10]}")
    for ep in ep_ids[:2]:
        for url in (
            f"https://stand.fm/api/episodes/{ep}",
            f"https://stand.fm/episodes/{ep}",
        ):
            res = fetch(url)
            if res is None or res.status_code != 200:
                continue
            if "json" in res.headers.get("content-type", ""):
                try:
                    report_json(url, res.json())
                except ValueError:
                    pass
            else:
                found = sorted(set(u for u in URL_RE.findall(res.text)
                                   if re.search(r"\.(m4a|mp3|aac|m3u8)|/audio", u, re.I)))
                print(f"  音声らしきURL {len(found)} 件: {found[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
