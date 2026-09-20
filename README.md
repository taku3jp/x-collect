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

### 1. スプレッドシートに「設定」タブを作る

対象スプシに `設定` というタブを追加し、以下を入力:

| セル | 内容 | 例 |
|---|---|---|
| A1 | `収集ON/OFF` | （ラベル） |
| B1 | `ON` or `OFF` | `ON` |
| A2 | `インプ閾値` | （ラベル） |
| B2 | 数値 | `300000` |
| A4 | `対象アカウント` | （ラベル） |
| A5〜 | アカウントID（@なし、1行1件） | `example_user` |

### 2. Apps Scriptをデプロイ（人間作業）

1. スプレッドシートで **拡張機能 → Apps Script**
2. `apps_script/Code.gs` の内容を全てコピペ
3. 先頭の `TOKEN` を適当なランダム文字列に変更（例: `xk7f29ab` — これを後でGitHub Secretにも入れる）
4. **デプロイ → 新しいデプロイ → 種類「ウェブアプリ」**
   - 実行ユーザー: **自分**
   - アクセスできるユーザー: **全員**
5. デプロイして表示されたURL（`https://script.google.com/macros/s/.../exec`）をコピー

### 3. GitHubリポジトリを作る

```bash
cd "/Users/takumi/Documents/ChatGPT/X収集"
git add -A && git commit -m "init"
# GitHubで public リポジトリを作成して push
gh repo create x-collect --public --source=. --push
```

publicにするとGitHub Actionsの実行時間が**無制限**になります。

### 4. state.json（Xログインセッション）を作る

このMacで一度だけ実行:

```bash
pip install playwright
playwright install chromium
python export_state.py
# ブラウザが開く → Xにログイン → ターミナルでEnter → state.jsonが生成
```

### 5. GitHub Secretsを登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**:

| Secret名 | 値 |
|---|---|
| `APPS_SCRIPT_URL` | 手順2のウェブアプリURL |
| `APPS_SCRIPT_TOKEN` | Code.gsに書いたTOKENと同じ文字列 |
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
