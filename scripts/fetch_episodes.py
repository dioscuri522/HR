#!/usr/bin/env python3
"""stand.fm のチャンネルからエピソード一覧と音声URLを取得する。

stand.fm は公開APIを文書化していない。そのため取得戦略を複数用意し、
順に試して最初に成功したものを採用する。どの戦略が通ったかは
data/fetch_report.json に残し、全滅した場合は生レスポンスを
diagnostics/ に落として Actions のアーティファクトから原因を追えるようにする。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIAG = ROOT / "diagnostics"

CHANNEL_ID = os.environ.get("STANDFM_CHANNEL_ID", "5f5edcc0f04555115dfb0845")
SLEEP_SEC = float(os.environ.get("FETCH_SLEEP_SEC", "1.0"))
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT_SEC", "30"))

UA = os.environ.get(
    "FETCH_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}

CHANNEL_URL = f"https://stand.fm/channels/{CHANNEL_ID}"

RSS_CANDIDATES = [
    f"https://stand.fm/rss/{CHANNEL_ID}",
    f"https://stand.fm/channels/{CHANNEL_ID}/rss",
    f"https://stand.fm/rss/channels/{CHANNEL_ID}",
    f"https://rss.stand.fm/{CHANNEL_ID}",
]

API_CANDIDATES = [
    f"https://stand.fm/api/channels/{CHANNEL_ID}/episodes",
    f"https://stand.fm/api/v2/channels/{CHANNEL_ID}/episodes",
    f"https://stand.fm/api/v1/channels/{CHANNEL_ID}/episodes",
    f"https://stand.fm/api/channels/{CHANNEL_ID}",
]

AUDIO_EXT = (".m4a", ".mp3", ".aac", ".mp4", ".wav", ".ogg")
AUDIO_KEY_HINTS = ("audio", "media", "sound", "file", "url", "src")

session = requests.Session()
session.headers.update(HEADERS)

_notes: list[str] = []


def note(msg: str) -> None:
    _notes.append(msg)
    print(msg, flush=True)


def dump(name: str, content: bytes | str) -> None:
    DIAG.mkdir(parents=True, exist_ok=True)
    path = DIAG / name
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, encoding=None if isinstance(content, bytes) else "utf-8") as fh:
        fh.write(content)


def get(url: str, **kw) -> requests.Response | None:
    try:
        res = session.get(url, timeout=TIMEOUT, **kw)
    except requests.RequestException as exc:
        note(f"  NG {url} -> {type(exc).__name__}: {exc}")
        return None
    finally:
        time.sleep(SLEEP_SEC)
    note(f"  {res.status_code} {url} ({len(res.content)} bytes, {res.headers.get('content-type','?')})")
    return res


def looks_like_audio(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("http"):
        return False
    low = value.split("?")[0].lower()
    return low.endswith(AUDIO_EXT) or "/audio" in low or "playlist.m3u8" in low


def iso(value) -> str | None:
    """様々な形式の日時を ISO8601(UTC) に寄せる。判定できなければ None。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # ミリ秒エポックの可能性
        ts = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        for parse in (datetime.fromisoformat,):
            try:
                dt = parse(text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
    return None


def episode_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/episodes/([0-9a-zA-Z_-]+)", url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# 戦略1: RSS
# --------------------------------------------------------------------------
def strategy_rss() -> list[dict]:
    for url in RSS_CANDIDATES:
        res = get(url)
        if res is None or res.status_code != 200 or not res.content:
            continue
        head = res.content[:200].lstrip()
        if not (head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head):
            note(f"  skip {url}: XML ではない")
            continue
        dump("rss.xml", res.content)
        parsed = feedparser.parse(res.content)
        episodes = []
        for entry in parsed.entries:
            audio = None
            for enc in list(getattr(entry, "enclosures", [])) + list(getattr(entry, "links", [])):
                href = enc.get("href") or enc.get("url")
                if href and (enc.get("rel") == "enclosure" or looks_like_audio(href)):
                    audio = href
                    break
            page = getattr(entry, "link", None)
            ep_id = episode_id_from_url(page) or getattr(entry, "id", None) or audio
            if not audio:
                note(f"  警告: 音声URLなし entry={getattr(entry,'title','?')}")
                continue
            episodes.append(
                {
                    "id": str(ep_id),
                    "title": getattr(entry, "title", "") or "",
                    "description": getattr(entry, "summary", "") or "",
                    "published_at": iso(getattr(entry, "published", None))
                    or iso(getattr(entry, "updated", None)),
                    "audio_url": audio,
                    "page_url": page,
                    "duration": getattr(entry, "itunes_duration", None),
                    "source": "rss",
                    "source_url": url,
                }
            )
        if episodes:
            note(f"  RSS 成功: {len(episodes)} 件 ({url})")
            return episodes
        note(f"  RSS からエピソードを抽出できず: {url}")
    return []


# --------------------------------------------------------------------------
# 戦略2: チャンネルページの埋め込み JSON
# --------------------------------------------------------------------------
def walk(obj, found: list[dict]) -> None:
    """音声URLらしき値を持つ dict を再帰的に集める。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and looks_like_audio(value):
                if any(hint in key.lower() for hint in AUDIO_KEY_HINTS):
                    found.append(obj)
                    break
        for value in obj.values():
            walk(value, found)
    elif isinstance(obj, list):
        for item in obj:
            walk(item, found)


def normalize_blob(blob: dict, source: str, source_url: str) -> dict | None:
    audio = None
    for key, value in blob.items():
        if isinstance(value, str) and looks_like_audio(value) and any(
            hint in key.lower() for hint in AUDIO_KEY_HINTS
        ):
            audio = value
            break
    if not audio:
        return None
    ep_id = (
        blob.get("id")
        or blob.get("episodeId")
        or blob.get("_id")
        or episode_id_from_url(audio)
        or audio.rsplit("/", 1)[-1].split(".")[0]
    )
    published = None
    for key in ("publishedAt", "published_at", "createdAt", "created_at", "updatedAt", "date"):
        published = iso(blob.get(key))
        if published:
            break
    page = blob.get("url") or blob.get("shareUrl")
    if not (isinstance(page, str) and "/episodes/" in page):
        page = f"https://stand.fm/episodes/{ep_id}"
    return {
        "id": str(ep_id),
        "title": blob.get("title") or blob.get("name") or "",
        "description": blob.get("description") or blob.get("body") or blob.get("comment") or "",
        "published_at": published,
        "audio_url": audio,
        "page_url": page,
        "duration": blob.get("duration") or blob.get("audioDuration"),
        "source": source,
        "source_url": source_url,
    }


def strategy_embedded_json() -> list[dict]:
    res = get(CHANNEL_URL)
    if res is None or res.status_code != 200:
        return []
    dump("channel.html", res.content)
    soup = BeautifulSoup(res.text, "lxml")
    blobs: list[dict] = []
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        candidates = []
        if script.get("id") == "__NEXT_DATA__" or script.get("type") == "application/json":
            candidates.append(raw)
        else:
            for m in re.finditer(r"(?:__NEXT_DATA__|__NUXT__|INITIAL_STATE)\s*=\s*(\{.*?\})\s*[;<]", raw, re.S):
                candidates.append(m.group(1))
        for text in candidates:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            dump("embedded.json", json.dumps(data, ensure_ascii=False, indent=2))
            walk(data, blobs)
    episodes = []
    seen = set()
    for blob in blobs:
        ep = normalize_blob(blob, "embedded_json", CHANNEL_URL)
        if ep and ep["id"] not in seen:
            seen.add(ep["id"])
            episodes.append(ep)
    if episodes:
        note(f"  埋め込みJSON 成功: {len(episodes)} 件")
    else:
        note("  埋め込みJSONから音声URLを見つけられず")
    return episodes


# --------------------------------------------------------------------------
# 戦略3: 内部 API
# --------------------------------------------------------------------------
def strategy_api() -> list[dict]:
    episodes: list[dict] = []
    seen = set()
    for base in API_CANDIDATES:
        page_found = False
        for offset in range(0, 2000, 100):
            url = base if offset == 0 else f"{base}?limit=100&offset={offset}"
            res = get(url, headers={"Accept": "application/json"})
            if res is None or res.status_code != 200:
                break
            try:
                data = res.json()
            except ValueError:
                note(f"  skip {url}: JSON ではない")
                break
            dump(f"api_{abs(hash(url)) % 10**8}.json", json.dumps(data, ensure_ascii=False, indent=2))
            blobs: list[dict] = []
            walk(data, blobs)
            new = 0
            for blob in blobs:
                ep = normalize_blob(blob, "api", url)
                if ep and ep["id"] not in seen:
                    seen.add(ep["id"])
                    episodes.append(ep)
                    new += 1
            if new == 0:
                break
            page_found = True
        if page_found and episodes:
            note(f"  API 成功: {len(episodes)} 件 ({base})")
            return episodes
    return []


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    note(f"チャンネル: {CHANNEL_URL}")

    strategies = [
        ("rss", strategy_rss),
        ("embedded_json", strategy_embedded_json),
        ("api", strategy_api),
    ]
    episodes: list[dict] = []
    used = None
    for name, fn in strategies:
        note(f"戦略 [{name}] を試行")
        try:
            episodes = fn()
        except Exception as exc:  # 戦略単位で失敗を隔離し、次を試す
            note(f"  例外: {type(exc).__name__}: {exc}")
            episodes = []
        if episodes:
            used = name
            break

    report = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "channel_id": CHANNEL_ID,
        "channel_url": CHANNEL_URL,
        "strategy_used": used,
        "episode_count": len(episodes),
        "log": _notes,
    }
    (DATA / "fetch_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if not episodes:
        note("すべての戦略が失敗した。diagnostics/ の生レスポンスを確認すること。")
        return 1

    # 既存メタデータとマージ（過去に取得したものを消さない）
    path = DATA / "episodes.json"
    merged: dict[str, dict] = {}
    if path.exists():
        for ep in json.loads(path.read_text(encoding="utf-8")):
            merged[ep["id"]] = ep
    for ep in episodes:
        merged[ep["id"]] = {**merged.get(ep["id"], {}), **ep}

    ordered = sorted(merged.values(), key=lambda e: (e.get("published_at") or "", e["id"]))
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    note(f"合計 {len(ordered)} 件を data/episodes.json に保存（戦略: {used}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
