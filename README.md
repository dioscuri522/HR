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

## 動作

1. `fetch_episodes.py` が3つの戦略を順に試し、最初に成功したものを採用する。
   RSS → チャンネルページ埋め込みJSON → 内部API の順。
   全滅した場合は生レスポンスを `diagnostics/` に落とし、Actions のアーティファクトに残す。
2. `run.py` が未処理エピソードを古い順にダウンロードして文字起こしし、`transcripts/` に書き出す。
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

- **push トリガー**: `scripts/` か workflow を変更して push すると、疎通確認として3件だけ処理する。
- **定期実行 / 手動実行**: `schedule` と `workflow_dispatch` は、**ワークフローファイルが
  デフォルトブランチ (`master`) に存在して初めて有効になる**（GitHub の仕様）。
  本格運用するには、このワークフローを `master` にマージする必要がある。

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

## 未検証の箇所

stand.fm の RSS・内部API のエンドポイント構造は**実機で未確認**（開発環境から到達できないため）。
`fetch_episodes.py` は複数候補を総当たりする作りにしてあり、Actions の実行ログと
`diagnostics/` の生レスポンスを見て調整する前提になっている。
また、ログイン必須・限定公開のエピソードは取得対象外。

収集はチャンネルに負荷をかけないよう、リクエスト間に待機（既定1秒）を入れている。
