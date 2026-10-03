# 固定公開投稿の再観測 (fixed re-observation) — 候補

既存の x-collect（新規発見→フィルタ→スプシ保存）とは独立した、
**固定の公開投稿URLだけを再観測する**候補実装。
`collector.py` / `growth_tracker.py` / GAS / 認証まわりは一切変更しない。

## 現状の位置づけ（重要）

- **manual-only 候補**: `.github/workflows/reobserve.yml` は
  `workflow_dispatch` のみ。schedule なし、push 起動なし、main への
  本稼働組み込みは未受入。
- **FanScope importer は未接続**: 成果物envelopeのスキーマは決め打ちだが、
  それを読み取る側（FanScopeの取込parser）はまだ存在しない。
  source row の `timeBasis` は本候補では常に `unknown` を維持し、
  FanScope が「信頼済みworkflow / artifact / 時計証拠」を別途受入れた
  ときだけ、専用parserが `clockEvidence` を使う想定。
- **時計精度は未証明**: 取得時計としてUTC区間と単調時間を計測・保持するが、
  誤差上限をこの候補では証明できないため `uncertaintyMs` は常に `null`。
  GitHub run metadata だけで `acquired` へ自己昇格しない。
  既存の保存ログ（スプシ行）を再解釈して時刻精度を主張することもしない。
- **固定追跡bridge・本稼働は未受入**: シートへの書込み、同一行上書き、
  保存済みIDによる除外、定期実行は行わない・実装していない。

## manifest (version 1)

```json
{
  "version": 1,
  "panelId": "public-slug-2026-10",
  "startAt": "2026-10-03T00:00:00Z",
  "expiresAt": "2026-10-05T00:00:00Z",
  "posts": ["https://x.com/<owner>/status/<id>", "..."]
}
```

- `posts` は公開canonical URL の配列。**最大100件**。
  101件以上は拒否（黙って切り捨てない）。
- `expiresAt - startAt` は **最大72時間**。start以前・期限後の実行は拒否。
- 上記5キー以外（未知/privateキー・caller申告のprecision等）は拒否。
- URL は `https://x.com|twitter.com/<owner>/status/<数字>` のみ。
  任意host、credential(`user@`/`user:pass@`)、query、fragment、
  非https、非canonical path、不正IDは拒否。
- 同一postIdの重複（同値/異値どちらも）は拒否。
  同じIDを複数の独立観測として数えない。

## 実行

```bash
python fixed_reobserve.py \
  --manifest reobserve_manifest.json \
  --output observations.json \
  --read-approved
```

- `--read-approved` `--manifest` `--output` は必須。
- `runId` / `runAttempt` は `GITHUB_RUN_ID` / `GITHUB_RUN_ATTEMPT`
  （既存の非秘密メタデータ）から固定される。環境がなければ `local`/`1`。
- 認証情報（state.json）はブラウザへ渡すだけで、検査・表示・複製しない。

## workflow の早期 gate

`reobserve.yml` は checkout / setup-python の直後、依存installと
session 復元の**前**に read approval と manifest を検証する
（`load_manifest` の pure loader + `check_window` の window check。
playwright は import しない）。gate 失敗時は以後の step を起動せず、
`X_STATE_JSON` secret は復元されない。入力は shell 補間せず
env → Python で渡す。

## ブランチ専用 dispatch shim (`collect.yml`) — 別ブランチのみ

GitHub Actions の `workflow_dispatch` は default branch に存在する
workflow しか起動できないが、ブランチ上に登録済みの workflow file は
`gh workflow run fixed-reobservation-branch-pilot --ref <branch>` で
手動起動できる。手動 pilot はこの制約のため、実装本体とは**別ブランチ**
`codex/fixed-x-reobservation-20261003` 上でだけ
`.github/workflows/collect.yml` を `reobserve.yml` と同一内容・name のみ
`fixed-reobservation-branch-pilot` に置き換えた **dispatch shim** に
差し替えて実行した。

- **その pilot ブランチは main へ merge 禁止。本PRにも含めない。**
- **本PRの diff は4ファイル追加のみ**（`.github/workflows/reobserve.yml`、
  `fixed_reobserve.py`、`tests/test_fixed_reobserve.py`、本doc）。
  通常の `.github/workflows/collect.yml` は一切変更しない。
- shim には schedule・GAS・通常収集を一切含めない。通常の collect
  （schedule / GAS webhook / 新規発見収集）は default branch (main)
  側の元ファイルで維持される。

## 手動 pilot 実行結果（2026-10-03）

上記 pilot ブランチの shim を `workflow_dispatch` で2回手動実行した
（GitHub Actions run `37107210372` / `37107329279`、間隔 約2分）。

- 同一の公開post `2105901234863698400` を再観測し、
  impressions `194277 → 194360`、likes `865 → 866` を観測。
- もう1件の対象は `unavailable`（対象IDがレスポンスに無い。
  削除確定とは呼ばない）。
- run 全体の `state` は `partial` であり終了コードは非0。
  `partial` を成功とはみなさない。
- これは約2分間隔の再観測であり、**24h比較・7日比較・
  アプリ（FanScope）取込・時計精度の受入はいずれも未達**。
  `clockEvidence` が存在しても取得時計精度の受入とは呼ばない。

## 取得の約束

- 各URLについてブラウザで閲覧し、`TweetDetail` /
  `TweetResultByRestId` の公開read responseだけを捕捉する。
  XのAPI endpointを直接呼ばない。閲覧以外の操作（クリック・投稿等）もしない。
- レスポンス内から**要求したpostIdだけ**を選ぶ。隣の返信を取り違えない。
  `normalize_tweet` の missing→0 転用はせず、生データから
  NULL（欠損）と実測0を厳密に区別する。
- 出力するのは author安定ID / 表示ハンドル / 公開日時 / URL /
  capturedAt / captureStartedAt / captureFinishedAt /
  monotonicElapsedMs / status / reason /
  impressions / likes / replies / reposts / quotes / bookmarks のみ。
  本文・メディア・実response・session・response URLのqueryは
  ログにも成果物にも出さない。

## 失敗の扱い

- `401/403/429`、login/challenge/age gate を検出したら `blocked` として
  **以後のURL取得を止める**。未試行の対象は omitted にせず
  `blocked` / `not_attempted` として残す。
- 対象IDがレスポンスに無い場合は削除確定とは呼ばず `unavailable`。
- 破損JSON・指標不正は `error`。`partial`（一部成功）を success としない。
- 例外本文や秘密は stdout/stderr に出さない（固定コードのみ）。
- ブラウザ起動・login state 読取・close の失敗は `run_reobservation` で
  捕捉し、完了行の無い全対象を `error` / 固定理由 `browser_error` と
  して envelope に記録する。取得済みの行は保持する
  （ok があれば `partial`、`blocked` も維持）。例外オブジェクトは
  文字列化しないため、例外本文・認証情報・ローカルpathは
  CLI にも成果物にも出ない。失敗時の終了コードは非0。
- response listener は `finally` で除去し、browser/context を閉じる。
  close 自体の失敗も `browser_error` として記録される。
- 時計の逆転・区間不整合は固定エラー `clock_invalid`。
- ブラウザ launch args は既存実行基盤の `--no-sandbox` のみ。
  `AutomationControlled` 等のstealth/ゲート回避flagは追加しない。

## 時計証拠 (clockEvidence)

取得の前後に、ホストに `chronyc` が**既にあれば** read-only の
`chronyc -n tracking` を読む（インストール・サービス起動・時計変更はしない）。
公式doc/FAQに基づき
`abs(System time) + Root dispersion + 0.5*abs(Root delay)`
で誤差を推定し、msへ保守的に切り上げる。run期間中の skew ppm も加味する。
根拠: https://chrony-project.org/doc/4.8/chronyc.html

以下はすべて `null`（数字の既定補完なし）:
chronyc 未存在、非同期（stratum 16 / Leap status 非Normal）、
local mode（参照がLOCAL/127.127.x）、実行エラー。
Ref の hostname/IP は成果物へ出さない。
校正時刻(Ref time)と sample 間の monotonic 区間も検証する。

## 成果物 envelope (version 1)

```json
{
  "version": 1,
  "source": "taku3jp/x-collect-fixed",
  "method": "x-collect-fixed-browser",
  "parserVersion": "x-collect-fixed-browser-1",
  "runId": "...", "runAttempt": "...",
  "panelId": "...",
  "startedAt": "...", "finishedAt": "...",
  "state": "ok|partial|blocked|error",
  "clockEvidence": null,
  "rows": [ ...最大100行... ]
}
```

- 原子的書込み（一時ファイル→`os.replace`）、mode 0600、UTF-8、最大2MiB。
- 各行の `resendKey` は `runId:runAttempt:postId`。
  同一snapshotの再取込はこのキーで冪等にできる想定。
  別runの新規取得は履歴として追加する（同一行上書き・既存ID除外なし）。
- artifact はメタデータJSONのみ（`x-fixed-observations`、7日保持）。
  debug画像・raw response・sessionはアップロードしない。
