# X収集

指定したXアカウントの投稿を巡回し、**インプ閾値以上のポストだけ**を
Googleスプレッドシートに自動収集する。GitHub Actionsで3時間おきに実行。

## 仕組み

```
GitHub Actions (3時間おき / 無料)
  └─ Cloudflare WARP で出口IPを住宅相当に（データセンターIPはXがブロックするため）
  └─ collector.py
       1. Apps Script から設定をGET（対象アカウント・閾値・収集済みID）
       2. state.json のXセッションで「おすすめTL(For You)」を限界まで巡回
          ※設定タブにアカウントを入れるとそのプロフィールTLも追加で巡回
          ※数値はDOMではなくX内部API(HomeTimeline/UserTweets/TweetDetail)のJSONから取得
       3. 閾値以上&未収集のポストの詳細ページを開いてスクリーンショット
       4. Apps Script にPOST → シート先頭に行挿入 + 画像をセルに埋め込み
```

設定はスプレッドシートの「設定」タブで変更できる（これがUI）。

## セットアップ（初回のみ・約15分）

### 1. Apps Scriptのデプロイ

**自動（実施済みの経路）:** `clasp`でプロジェクト作成・push・デプロイ済み。
初回 `GET .../exec?action=bootstrap` でTOKEN自動生成 + 「設定」「X収集」
タブの自動作成が行われる。`gas/` 配下がデプロイ済みコード（`apps_script/Code.gs`
と同じ内容）。

**手動の場合（代替手順）:**

1. スプレッドシートで **拡張機能 → Apps Script** → `apps_script/Code.gs` をコピペ
2. **デプロイ → 新しいデプロイ → ウェブアプリ**（自分/全員）
3. 一度ブラウザで `/exec` URLを開いて権限を承認
4. `/exec?action=bootstrap` を開く → 返ってきた `token` をGitHub Secretへ

### 2. 「設定」タブで対象を指定

setup実行で自動生成される「設定」タブに入力:

| セル | 内容 |
|---|---|
| B1 | `ON`（止めたいときは `OFF`） |
| B2 | インプ閾値（例 `300000`） |
| A5〜 | 対象アカウントID（@なし・1行1件） |

### 3. GitHubリポジトリを作る

```bash
cd "/Users/takumi/Documents/ChatGPT/X収集"
git add -A && git commit -m "init"
# GitHubで public リポジトリを作成して push
gh repo create x-collect --public --source=. --push
```

publicにするとGitHub Actionsの実行時間が**無制限**になります。

### 4. state.json（Xログインセッション）を作る

**方法A（推奨）: ログイン済みChromeからクッキーを取り出す**

```bash
pip install browser_cookie3 playwright
python export_state_from_chrome.py
# macOSのキーチェーン許可ダイアログが出たら「許可」
```

**方法B: Playwrightブラウザでログイン**（Xのログイン制限に注意）

```bash
python export_state.py
# ブラウザが開く → Xにログイン → ターミナルでEnter → state.jsonが生成
```

### 5. GitHub Secretsを登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**:

| Secret名 | 値 |
|---|---|
| `APPS_SCRIPT_URL` | 手順1のウェブアプリURL |
| `APPS_SCRIPT_TOKEN` | スクリプトプロパティに登録したTOKENと同じ文字列 |
| `X_STATE_JSON` | `state.json` の中身を全部コピペ |

### 6. 動作確認

GitHubリポジトリの **Actions → collect-x-posts → Run workflow** で手動実行。
成功すればシートに行が追加される。あとは3時間おきに自動実行。

## 自アカ投稿の伸び検出 → 「実際伸びた投稿」タブ + Slack

`growth_tracker.py` が「設定」タブH5以降に登録した自アカウントの
プロフィールTLを毎回巡回し、**インプが閾値を新たに超えた自分の投稿**を
「実際伸びた投稿」タブに追記し、Slackへ通知する。
投稿のインプは時間とともに増えるため、毎回直近ポストを再スキャンして
「閾値を新たに超えたものだけ」を記録する方式（重複はstatus IDで排除）。

「設定」タブの自アカ監視欄:

| セル | 内容 |
|---|---|
| D2 | 自アカ監視 `ON`/`OFF` |
| D3 | 自アカインプ閾値（デフォルト `100000`） |
| H5〜 | 監視するアカウントID（@なし・1行1件） |

Slack通知には GitHub Secret `SLACK_WEBHOOK_URL` が必要
（Slack App の Incoming Webhooks で発行したURL）。
未設定でもシートへの追記は動く（Slack通知だけスキップ）。

ローカル検証: `DRY_RUN=1 python growth_tracker.py` で保存・通知をせず
候補だけ表示。

## スプレッドシートの列

`No. | 日付 | 参考リンク | ポスト文 | 素材リンク | 動画内容 | 参考画像 | インプ | いいね | リポスト | 保存数`

- 新しい収集が**2行目（最上段）**に挿入される
- `動画内容` にはポストのスクリーンショットがセル内画像として入る
  （画像ファイルはDriveの「X収集画像」フォルダに保存される）
- `素材リンク` はポスト内の画像/動画の直リンク（改行区切り）
- `参考画像` は手動用に空欄のまま

## 運用メモ

- **収集対象**: デフォルトは収集アカウントのおすすめTL（For You）。
  「設定」タブA5以降にアカウントIDを入れるとそのプロフィールTLも追加で巡回
- **セッション切れ**: Xがセッションを無効化するとログに
  `state.jsonが無効です` と出る。手順4をやり直して `X_STATE_JSON` を更新
- **閾値・アカウント変更**: スプシ「設定」タブを書き換えるだけで即反映
- **一時停止**: 「設定」タブのB1を `OFF` にする
- **収集数の上限**: 1回の実行で最大25件（`MAX_SHOTS`）までスプシへ送信。
  超過分は次回以降に回収される（収集済みIDで重複判定するため取りこぼしなし）
- **ローカルで手動実行**: `APPS_SCRIPT_URL` `APPS_SCRIPT_TOKEN` を環境変数に
  設定して `python collector.py`

## FanScope向けメタデータ出力

`gas/Code.js` と `gas/FanScopeExport.js` を既存のApps Scriptへ反映し、既存Webアプリの新バージョンとしてデプロイする。GitHubへの反映だけではデプロイは更新されない。既存のTOKENとURLを使用し、X APIトークンは不要。

- `GET ?action=fanscope_export_capability`: 認証・シートへのアクセスなしで対応バージョンを返す。取り込み側はこの応答のversion/source/capabilityが一致してからPOSTする。旧デプロイにはPOSTしない。
- `POST {"action":"fanscope_export","token":"<既存TOKEN>"}`: 「X収集」のURL・収集日時・反応数だけを返す。本文・画像・動画・Drive URLは含まない。
- 読み取り専用。認証後最大3000行を一括取得し、超過・不正な数値は明示的に失敗する。上流で未取得と区別できない0はnull。収集日時はJSTで、投稿公開日時とは異なる。
- 出力形式: `{version:1, source:"taku3jp/x-collect", rows:[{url,date,impressions,likes,reposts,bookmarks}]}`。
- TOKENをログ・PR・チャットへ貼らない。シートが通常持つのは投稿ごとの単発記録であり、増加や他SNSとの因果をこの出力だけで示すものではない。

検証: `node --test tests/fanscope-export.test.mjs`。GAS実デプロイと実データ取得は別途必要。
