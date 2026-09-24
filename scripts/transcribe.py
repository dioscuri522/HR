#!/usr/bin/env python3
"""音声ファイルを日本語テキストに変換する。

エンジンは2系統:
  faster-whisper : 無料。CPU で動く。タイムスタンプ付きセグメントが取れる。
  gemini         : GEMINI_API_KEY が必要。高速だがタイムスタンプは得られない。

TRANSCRIBE_ENGINE 環境変数で選択（既定 faster-whisper）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import requests

ENGINE = os.environ.get("TRANSCRIBE_ENGINE", "faster-whisper").lower()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM_SIZE", "1"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE = "https://generativelanguage.googleapis.com"

_model = None


def ffmpeg_bin() -> str:
    """システムの ffmpeg を優先し、無ければ imageio-ffmpeg 同梱バイナリを使う。"""
    from shutil import which

    found = which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav(src: Path, dst: Path) -> Path:
    """Whisper が扱いやすい 16kHz モノラル WAV へ変換する。"""
    cmd = [
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


def audio_duration_sec(path: Path) -> float | None:
    """ffmpeg の出力から尺を拾う。取れなければ None。"""
    proc = subprocess.run(
        [ffmpeg_bin(), "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in reversed(proc.stderr.splitlines()):
        if "time=" in line:
            chunk = line.split("time=")[-1].split(" ")[0]
            try:
                h, m, s = chunk.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# faster-whisper
# --------------------------------------------------------------------------
def _load_whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        print(f"  Whisper モデル読み込み: {WHISPER_MODEL} ({WHISPER_COMPUTE})", flush=True)
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE)
    return _model


def transcribe_whisper(audio: Path) -> dict:
    wav = audio.with_suffix(".16k.wav")
    to_wav(audio, wav)
    try:
        segments, info = _load_whisper().transcribe(
            str(wav), language="ja", vad_filter=True, beam_size=WHISPER_BEAM,
        )
        rows = [
            {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
            for s in segments
        ]
    finally:
        wav.unlink(missing_ok=True)
    return {
        "engine": "faster-whisper",
        "model": WHISPER_MODEL,
        "duration_sec": round(getattr(info, "duration", 0.0), 2),
        "segments": rows,
        "text": "".join(r["text"] for r in rows),
    }


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
def _gemini_upload(audio: Path) -> str:
    """Files API のレジューム可能アップロードでファイル URI を得る。"""
    size = audio.stat().st_size
    mime = {
        ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    }.get(audio.suffix.lower(), "audio/mpeg")

    start = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files?key={GEMINI_KEY}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": audio.name}},
        timeout=60,
    )
    start.raise_for_status()
    upload_url = start.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Gemini: アップロードURLが返らなかった")

    with open(audio, "rb") as fh:
        up = requests.post(
            upload_url,
            headers={
                "Content-Length": str(size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=fh,
            timeout=600,
        )
    up.raise_for_status()
    info = up.json()["file"]

    # ACTIVE になるまで待つ
    for _ in range(60):
        if info.get("state") == "ACTIVE":
            return info["uri"]
        time.sleep(5)
        res = requests.get(f"{GEMINI_BASE}/v1beta/{info['name']}?key={GEMINI_KEY}", timeout=60)
        res.raise_for_status()
        info = res.json()
    raise RuntimeError(f"Gemini: ファイルが ACTIVE にならない (state={info.get('state')})")


PROMPT = (
    "この音声は日本語の個人ラジオ配信です。話されている内容を、省略・要約・意訳をせず"
    "逐語で文字起こししてください。フィラー（えー、あの等）は適度に整理して構いませんが、"
    "主張・理由・具体例は一切省かないでください。話者が複数いる場合は話者を区別してください。"
    "出力は文字起こし本文のみとし、前置きや解説は付けないでください。"
)


def transcribe_gemini(audio: Path) -> dict:
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定")
    uri = _gemini_upload(audio)
    mime = "audio/mp4" if audio.suffix.lower() in (".m4a", ".mp4") else "audio/mpeg"
    res = requests.post(
        f"{GEMINI_BASE}/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
        json={
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {"file_data": {"mime_type": mime, "file_uri": uri}},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 65536},
        },
        timeout=900,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Gemini API {res.status_code}: {res.text[:500]}")
    payload = res.json()
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini: 想定外のレスポンス {json.dumps(payload)[:500]}")
    return {
        "engine": "gemini",
        "model": GEMINI_MODEL,
        "duration_sec": audio_duration_sec(audio),
        "segments": [],
        "text": text,
    }


def transcribe(audio: Path) -> dict:
    if ENGINE in ("gemini", "gemini-api"):
        return transcribe_gemini(audio)
    if ENGINE in ("faster-whisper", "whisper"):
        return transcribe_whisper(audio)
    raise ValueError(f"未知のエンジン: {ENGINE}")
