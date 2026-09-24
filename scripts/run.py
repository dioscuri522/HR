#!/usr/bin/env python3
"""未処理のエピソードをダウンロードして文字起こしし、transcripts/ に書き出す。

GitHub Actions のジョブ上限（6時間）に収まるよう時間予算で打ち切り、
残りは次回の実行に持ち越す。途中で落ちても完了済みは失われない。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transcribe import ENGINE, transcribe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TRANSCRIPTS = ROOT / "transcripts"
CACHE = ROOT / "audio_cache"

MAX_EPISODES = int(os.environ.get("MAX_EPISODES", "0"))  # 0 = 無制限
TIME_BUDGET_MIN = float(os.environ.get("TIME_BUDGET_MIN", "300"))
ORDER = os.environ.get("ORDER", "old")  # old = 古い順（思考の変遷を追う）／new = 新しい順
SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SLEEP_SEC = float(os.environ.get("DOWNLOAD_SLEEP_SEC", "1.0"))

UA = os.environ.get(
    "FETCH_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)


def slugify(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\s/\\:*?\"<>|]+", "_", (text or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:limit] or "untitled"


def transcript_path(ep: dict) -> Path:
    date = (ep.get("published_at") or "0000-00-00")[:10]
    return TRANSCRIPTS / f"{date}_{slugify(ep['id'], 40)}.md"


def resolve_audio_url(ep_id: str) -> str:
    """エピソード詳細APIから音声URLを取り出す。

    一覧APIの応答には音声URLが含まれないため、ダウンロード直前に解決する。
    応答には nextEpisodes や recommendedEpisodes など他エピソードの情報も
    混ざるので、必ず当該エピソードのオブジェクトの中だけを見る。
    """
    url = f"https://stand.fm/api/episodes/{ep_id}"
    res = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60)
    res.raise_for_status()
    body = res.json().get("response") or {}
    own = (body.get("episodes") or {}).get(ep_id)
    if own is None:
        raise RuntimeError(f"エピソード {ep_id} が応答に含まれない")

    found: list[str] = []

    def walk(obj) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, str) and obj.startswith("http") and ".m4a" in obj:
            found.append(obj)

    walk(own)
    if not found:
        raise RuntimeError(f"エピソード {ep_id} の音声URLが見つからない")
    return found[0]


def download(url: str, dest: Path) -> Path:
    headers = {"User-Agent": UA, "Referer": "https://stand.fm/"}
    with requests.get(url, headers=headers, stream=True, timeout=120) as res:
        res.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in res.iter_content(1 << 16):
                fh.write(chunk)
    return dest


def write_markdown(ep: dict, result: dict, path: Path) -> None:
    def esc(value) -> str:
        return json.dumps(value if value is not None else "", ensure_ascii=False)

    lines = [
        "---",
        f"episode_id: {esc(ep['id'])}",
        f"title: {esc(ep.get('title'))}",
        f"published_at: {esc(ep.get('published_at'))}",
        f"page_url: {esc(ep.get('page_url'))}",
        f"audio_url: {esc(ep.get('audio_url'))}",
        f"engine: {esc(result.get('engine'))}",
        f"model: {esc(result.get('model'))}",
        f"duration_sec: {result.get('duration_sec') or 0}",
        f"transcribed_at: {esc(datetime.now(timezone.utc).isoformat())}",
        "---",
        "",
        f"# {ep.get('title') or ep['id']}",
        "",
    ]
    description = (ep.get("description") or "").strip()
    if description:
        lines += ["## 配信者による説明", "", description, ""]
    lines += ["## 文字起こし", ""]
    if result.get("segments"):
        # タイムスタンプ付き。後の分析で発言の出典を特定できるようにする。
        for seg in result["segments"]:
            mm, ss = divmod(int(seg["start"]), 60)
            hh, mm = divmod(mm, 60)
            stamp = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
            lines.append(f"[{stamp}] {seg['text']}")
    else:
        lines.append(result.get("text", ""))
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def load_state() -> dict:
    path = DATA / "state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done": {}, "failed": {}}


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    (DATA / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    episodes_path = DATA / "episodes.json"
    if not episodes_path.exists():
        print("data/episodes.json がない。先に fetch_episodes.py を実行すること。")
        return 1

    episodes = json.loads(episodes_path.read_text(encoding="utf-8"))
    state = load_state()
    done = state.setdefault("done", {})
    failed = state.setdefault("failed", {})

    pending = []
    for ep in episodes:
        path = transcript_path(ep)
        if ep["id"] in done and path.exists():
            continue
        if path.exists():  # 状態ファイルが欠けているだけなら追認する
            done[ep["id"]] = str(path.relative_to(ROOT))
            continue
        pending.append(ep)

    pending.sort(key=lambda e: (e.get("published_at") or ""), reverse=(ORDER == "new"))
    if SHARD_COUNT > 1:
        # 並列ジョブで分担する。並び順は安定しているのでシャード同士は重複しない。
        pending = [e for i, e in enumerate(pending) if i % SHARD_COUNT == SHARD_INDEX]
    if MAX_EPISODES > 0:
        pending = pending[:MAX_EPISODES]

    print(f"全 {len(episodes)} 件 / 処理済 {len(done)} 件 / 今回対象 {len(pending)} 件")
    shard = f" / シャード {SHARD_INDEX + 1}/{SHARD_COUNT}" if SHARD_COUNT > 1 else ""
    print(f"エンジン: {ENGINE} / 時間予算: {TIME_BUDGET_MIN} 分 / 順序: {ORDER}{shard}")

    started = time.monotonic()
    processed = 0
    for index, ep in enumerate(pending, 1):
        elapsed_min = (time.monotonic() - started) / 60
        if elapsed_min > TIME_BUDGET_MIN:
            print(f"時間予算に到達（{elapsed_min:.1f}分）。残りは次回に持ち越す。")
            break

        print(f"[{index}/{len(pending)}] {ep.get('published_at','?')} {ep.get('title','')[:50]}")
        audio = CACHE / f"{slugify(ep['id'], 40)}.m4a"
        try:
            audio_url = ep.get("audio_url") or resolve_audio_url(ep["id"])
            ep["audio_url"] = audio_url
            download(audio_url, audio)
            print(f"  ダウンロード完了 {audio.stat().st_size / 1e6:.1f} MB")
            result = transcribe(audio)
            path = transcript_path(ep)
            write_markdown(ep, result, path)
            done[ep["id"]] = str(path.relative_to(ROOT))
            failed.pop(ep["id"], None)
            processed += 1
            chars = len(result.get("text", ""))
            print(f"  文字起こし完了 {chars} 文字 -> {path.relative_to(ROOT)}")
        except Exception as exc:
            failed[ep["id"]] = {
                "error": f"{type(exc).__name__}: {exc}",
                "at": datetime.now(timezone.utc).isoformat(),
            }
            print(f"  失敗: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            audio.unlink(missing_ok=True)
            save_state(state)
            time.sleep(SLEEP_SEC)

    state["total_episodes"] = len(episodes)
    save_state(state)
    remaining = len(episodes) - len(done)
    print(f"完了: 今回 {processed} 件処理 / 累計 {len(done)} 件 / 残り {remaining} 件")
    if failed:
        print(f"失敗が {len(failed)} 件ある（data/state.json の failed を参照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
