# orekou.net 練習試合データ収集ツール(Render動作確認用)

## これは何か

俺の甲子園(orekou.net)の練習試合結果を、運営に確認済みの低頻度(1秒1回未満)で
収集するツール一式。会社PC・ネットワークでのSSL証明書検証エラーを避けるため、
Render上のWeb Service(無料プラン)として実行する構成にしてある。

まずは `MAX_GAMES=100` で動作確認することが目的。継続的な本番収集(外部DB連携)は
別途設計する。

無料プランには Background Worker が無いため、Web Service として起動し、
HTTPポートを開きながらバックグラウンドスレッドでクロールを進める構成にしている
(`run_render_web.py`)。ブラウザでサービスのURLを開くと、進捗・結果を
テキストで確認できる。

## ファイル構成

- `orekou_http.py` : 共通HTTPレイヤー(レート制限・キャッシュ・リトライ・サーキットブレーカー・日次予算)
- `orekou_school_list_scraper.py` / `orekou_school_profile_scraper.py` / `orekou_scraper.py` / `orekou_student_scraper.py` : 各ページのスクレイパー
- `orekou_condition.py` : 調子による能力補正の計算モジュール
- `crawl_state.py` : 訪問済みURL管理・進捗の永続化
- `orekou_crawler.py` : 上記を束ねる統合クローラー
- `orekou_validate.py` : 収集データの整合性チェック
- `run_render_web.py` : Render無料Web Service上で動作確認クロールを実行し、
  進捗・結果をHTTP経由で確認できるようにするラッパー(こちらを使う)
- `render.yaml` : Render用のWeb Service構成定義
- `requirements.txt` : 依存ライブラリ

## GitHubへのアップロード手順

1. GitHubで新しいリポジトリを作成する(Public/Privateどちらでも可。Renderの無料プランはPublicでもPrivateでも連携可能)
2. このフォルダの中身(このREADMEを含む全ファイル)をそのリポジトリにアップロードする
   - GitHubのWeb画面から "Add file" → "Upload files" でドラッグ&ドロップするのが簡単
3. コミットする

## Renderでのデプロイ手順

1. Renderのダッシュボードにログイン(sky-ymst.onrender.comと同じアカウント)
2. "New +" → "Web Service" を選択
3. 先ほど作成したGitHubリポジトリを連携する
4. `render.yaml` が自動検出されるはずなので、内容を確認して進める
   - 自動検出されない場合は、手動で以下を設定:
     - Language: Python 3
     - Build Command: `pip install -r requirements.txt`
     - Start Command: `python run_render_web.py`
     - Instance Type: Free
     - 環境変数: `MAX_GAMES=100`, `CRAWL_INTERVAL=5.0`, `CRAWL_JITTER=2.0`
5. デプロイが始まったら、"Logs" タブを開いて進捗を確認する
6. デプロイ完了後に発行されるURL(例: `https://orekou-crawler-test.onrender.com/`)を
   ブラウザで開くと、進捗・結果がテキストで表示される

## 結果の確認方法

- **ブラウザで直接確認**: デプロイ完了後のサービスURLを開くと、その時点の
  ステータス(`running` / `done` / `error`)、整合性チェックのサマリー、
  ログの直近500行が表示される。定期的に再読み込みすれば進捗を追える。
- **Renderのログタブ**: 同じ内容がRenderダッシュボードの"Logs"にも出力される。

無料プランには永続ディスクが無いため、`matches.jsonl` 等のファイルは
サービス再起動時にファイルシステムごと消える。取得したデータそのものを
残したい場合は、ブラウザ表示 or ログから内容をコピーして保存するか、
継続収集フェーズで外部DBへの保存に切り替える。

## 無料プランの注意点

- 一定時間アクセスが無いとスリープする。デプロイ直後はアクセスがあるので
  スリープしないが、念のためデプロイ後はブラウザでURLを開いたままにしておくか、
  数分おきに再読み込みすることをおすすめする。
- 100試合(想定10〜15分)程度であれば、スリープが発生する前に完了する見込み。

## 動作確認後にやること

100試合の動作確認がうまくいったら、次のステップとして:

1. 継続収集用に外部DB(Supabase等)への保存に切り替える設計を検討する
2. `MAX_GAMES` を段階的に引き上げる
3. 有料プランのBackground Worker、またはRender Cron Jobsでの定期実行を検討する

## 注意事項

- `orekou_http.py` のレート制限(`interval=5.0秒 + jitter 0〜2秒`)は、
  orekou.net運営への問い合わせで確認済みの「1秒1回未満」を大きく下回るペースを
  維持するためのものです。`CRAWL_INTERVAL` を安易に下げないでください。
- User-Agentは身元を偽っていません(`orekou-personal-research/1.0`)。
  個人の研究目的であることを示したまま、低頻度アクセスである旨を明記しています。
