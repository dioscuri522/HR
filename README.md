# HR — 人財

stand.fm チャンネルの配信内容を自動収集し、文字起こしを蓄積して分析するためのリポジトリ。

対象チャンネル: https://stand.fm/channels/5f5edcc0f04555115dfb0845

## なぜ GitHub Actions で収集するのか

分析を行う Claude Code のリモート実行環境は、egress ポリシーにより `stand.fm` へ到達できない
（GitHub・パッケージレジストリ・googleapis のみ許可）。一方 GitHub Actions のランナーは
ネットワーク制限がない。そこで **収集は Actions 側で行い、成果物をリポジトリに commit する**
構成にしている。分析側は `transcripts/` を読むだけでよい。

## 構成

```
.github/workflows/collect-standfm.yml  収集ワークフロー
scripts/fetch_episodes.py              エピソード一覧と音声URLの取得
scripts/transcribe.py                  音声 → テキスト（faster-whisper / Gemini）
scripts/run.py                         増分処理のオーケストレーション
data/episodes.json                     エピソードのメタデータ（自動生成）
data/state.json                        処理済み・失敗の記録（自動生成）
data/fetch_report.json                 どの取得戦略が通ったかの記録（自動生成）
transcripts/<日付>_<ID>.md             文字起こし（自動生成）
```

## 確定した取得経路

実地調査（Actions 上で5次にわたって実施）で以下を確定させた。

```
一覧  GET /api/channels/{channelId}/appending?limit=2000
        → response.episodes に全1033件。hasNextEpisode=false。カーソル不要
音声  GET /api/episodes/{episodeId}
        → https://cdncf.stand.fm/audios/{ULID}.m4a
```

判明した注意点:

- 公開RSSは存在しない（`channelRssUrl` は null、`/rss` 系はすべて404）
- `sitemap.xml` / `robots.txt` も404
- `/api/channels/{id}`（`appending` なし）は `limit` を無視して常に最新10件しか返さない。
  パラメータ名を16通り試したが応答が完全に同一だった
- `totalDuration` の単位はミリ秒
- `episodes` は dict で、キーの並びは `publishedAt` の昇順

## 動作

1. `fetch_episodes.py` が上記の一覧APIから全エピソードのメタデータを取得する。
2. `run.py` が未処理エピソードを古い順にダウンロードして文字起こしし、`transcripts/` に書き出す。
   音声URLはダウンロード直前に `/api/episodes/{id}` から解決する。
   処理済みは `data/state.json` で管理され、再実行しても重複処理しない。
3. 時間予算（既定300分）で打ち切り、残りは次回の実行に持ち越す。
   Actions のジョブ上限6時間に収めるための措置。
4. 結果をブランチに commit + push する。

## 文字起こしエンジン

| エンジン | 費用 | 速度 | タイムスタンプ | 必要なもの |
|---|---|---|---|---|
| `faster-whisper`（既定） | 無料 | 遅い（ランナーはCPU 2コア） | あり | なし |
| `gemini` | 従量課金 | 速い | なし | `GEMINI_API_KEY` を repo secret に登録 |

タイムスタンプがあると、分析時に「どの発言が根拠か」を時刻で特定できる。
バックフィルを急ぐ場合のみ Gemini に切り替えるのが妥当。

## 実行方法

- `verify-standfm.yml` は取得経路が機能するかを確認するだけで、何もコミットしない。
- `collect-standfm.yml` は文字起こし全文をリポジトリにコミットするため、**自動起動しない**
  設定にしてある（`workflow_dispatch` のみ）。保存先の扱いが決まるまでは手動実行が必要。
  なお `workflow_dispatch` は、ワークフローファイルがデフォルトブランチ (`master`) に
  存在して初めて有効になる（GitHub の仕様）。

## 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `STANDFM_CHANNEL_ID` | 上記チャンネル | 対象チャンネル |
| `TRANSCRIBE_ENGINE` | `faster-whisper` | `faster-whisper` / `gemini` |
| `WHISPER_MODEL` | `small` | `tiny`〜`large-v3` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini 使用時のモデル |
| `MAX_EPISODES` | `0`（無制限） | 1回の実行で処理する上限 |
| `TIME_BUDGET_MIN` | `300` | 打ち切りまでの分数 |
| `ORDER` | `old` | `old`＝古い順（思考の変遷を追う）／`new`＝新しい順 |

## 規模

| 項目 | 値 |
|---|---|
| エピソード数 | 1033 |
| 1件あたりの尺 | 中央値 約10分（最短16秒／最長約18分） |
| 限定公開・支援者限定 | 確認した範囲では0件 |

## 取り扱い上の注意

エピソードの説明文に、著作権は WGSL に帰属し、第三者による複製および送信可能化を
禁じる旨が明記されている。文字起こし全文をどこに保存するかは、この記載を踏まえて
決める必要がある。`collect-standfm.yml` を自動起動させていないのはこのため。

収集はチャンネルに負荷をかけないよう、リクエスト間に待機（既定1秒）を入れている。
