# GOAL: 同人アダアフィ投稿収集システム

## 目的

XのFor Youタイムラインから「**使い捨てアカウントによる同人系アダルトアフィリエイト投稿**」のみを
高精度（目標95%以上）で判別・収集し、スプレッドシートに蓄積する。

商業AV（FANZA/DMM/MGS/SOKMIL等）・公式垢・エロ漫画・無関係なバズ投稿は一切収集しない。

## 収集対象の定義（参考例19件から学習した構造）

ポジティブ例の共通構造:

1. **日本語の短文釣り文**（10〜40字）＋メディア（主に動画）
   - 例:「探してたやつ見つけた」「このビジュでマチアプやってる理由はコレ一択でしょ」
   - 本文には外部URLなし（t.coメディアリンクのみ）
2. **使い捨て垢っぽいアカウント**（認証バッジなし・ランダム英数字名が多い）
3. **収益化は返信にリンク**:
   - 本人返信 → myfans.jp / bit.ly / mfco.link 等
   - 他人返信 → mfco.link / loknote77.com / videy.yt / videi.in / 他垢x.comリング
4. **リング型**: 返信のx.comリンク先ポストにアフィリエイトリンクがある

## 判定モデル（collector.py is_target）

収集 = 以下すべてを満たす:

- `lang == "ja"`
- 認証バッジなし（`is_blue_verified` / `verification.verified` が False）
- 本文が商業宣伝パターンに非一致（発売中/配信開始/【長タイトル】/予約受付/セール中）
- 本文が漫画系パターンに非一致（漫画/コミック/単行本/試し読み/DLsite/成年向け）
- 以下のいずれかのアフィリンク構造を持つ:
  a. スレッド内の返信にキーワードドメイン一致のURL（URL文字列のみで判定・本文ワードは見ない）
  b. 本人返信にキーワード一致の外部URL
  c. 他人返信のx.comポストリンク先にアフィリンク構造（1段掘って検証）
  d. 本文のリンクがキーワードドメイン一致

## 対象ドメイン（設定タブD5〜で管理・随時追加）

- ファンクラブ: myfans(.jp), xfans, onlyfans, fansly, fantia(.jp), fanvue, candfans, fc2, stripchat, chaturbate
- リンク集: lit.link, linktr.ee, potofu.me, instabio.me, bio.site, campsite.bio
- アフィ短縮/誘導: mfco.link, loknote77.com, videy.yt, videi.in, ho-zuki.com, omg10.com
- 汎用短縮: bit.ly, cutt.ly, tinyurl, t.ly, reurl.cc, is.gd, x.gd

## 除外（ネガティブ）ルール

- 商業AVドメイン（fanza, dmm.co.jp, dmm.com, mgs, sokmil, duga）はキーワードに入れない
- 認証バッジ付きアカウント（公式・企業・本人確認済み）
- 商業宣伝文パターン・漫画系ワード
- リンク構造を持たない単なるバズエロ投稿

## 運用ループ

1. 3時間おきにGitHub Actionsで収集（For You TL → フィルタ → スクショ → スプシ）
2. ユーザーが混入/取りこぼしを報告 → 具体URLを解析して判定ロジック・ドメインリストを更新
3. 混入分は `action=delete&ids=...` で行削除
4. 新しいアフィドメインを発見したらキーワードに追加
