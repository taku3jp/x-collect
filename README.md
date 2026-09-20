# X収集

指定したXアカウントの投稿を巡回し、**インプ閾値以上のポストだけ**を
Googleスプレッドシートに自動収集する。GitHub Actionsで3時間おきに実行。

## 仕組み

```
GitHub Actions (3時間おき / 無料)
  └─ collector.py
       1. Apps Script から設定をGET（対象アカウント・閾値・収集済みID）
       2. state.json のXセッションで各アカウントのタイムラインを巡回
          ※数値はDOMではなくX内部API(UserTweets/TweetDetail)のJSONから取得
       3. 閾値以上&未収集のポストの詳細ページを開いてスクリーンショット
       4. Apps Script にPOST → シート先頭に行挿入 + 画像をセルに埋め込み
```

設定はスプレッドシートの「設定」タブで変更できる（これがUI）。

## セットアップ（初回のみ・約15分）

### 1. Apps Scriptのデプロイ

**自動（実施済みの経路）:** `clasp`でプロジェクト作成・push・デプロイ済み。
初回 `GET .../exec?action=bootstrap` でTOKEN自動生成 + 「設定」「X収集テスト」
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

## スプレッドシートの列

`No. | 日付 | 参考リンク | ポスト文 | 素材リンク | 動画内容 | 参考画像 | インプ | いいね | リポスト | 保存数`

- 新しい収集が**2行目（最上段）**に挿入される
- `動画内容` にはポストのスクリーンショットがセル内画像として入る
  （画像ファイルはDriveの「X収集画像」フォルダに保存される）
- `素材リンク` はポスト内の画像/動画の直リンク（改行区切り）
- `参考画像` は手動用に空欄のまま

## 運用メモ

- **セッション切れ**: Xがセッションを無効化するとログに
  `state.jsonが無効です` と出る。手順4をやり直して `X_STATE_JSON` を更新
- **閾値・アカウント変更**: スプシ「設定」タブを書き換えるだけで即反映
- **一時停止**: 「設定」タブのB1を `OFF` にする
- **収集数の上限**: 1回の実行で最大25件（`MAX_SHOTS`）までスプシへ送信。
  超過分は次回以降に回収される（収集済みIDで重複判定するため取りこぼしなし）
- **ローカルで手動実行**: `APPS_SCRIPT_URL` `APPS_SCRIPT_TOKEN` を環境変数に
  設定して `python collector.py`
