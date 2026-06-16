# dorama-seed

[ドラマのじかん](https://github.com/micchymouse/dorama_no_jikan)(private)アプリ用の**シードJSON配信**リポジトリ。

日本語版 Wikipedia の MediaWiki API から「今期の連続ドラマ一覧」を生成し、**GitHub Pages** で静的配信する。
アプリはここから取得し、失敗時は同梱アセットへフォールバックする(**ローカル完結・アカウント不要・サーバー費ゼロ**方針)。

- データ源: 公開 Wikipedia(規約クリア・無料)。秘匿情報は含まない。
- 配信URL: `https://micchymouse.github.io/dorama-seed/seed/dramas_{year}_{cool}.json`
  - `cool` = `winter | spring | summer | autumn`
- 生成: `.github/workflows/seed.yml`(毎週月曜 + 手動)が当年の全クールを生成し Pages へデプロイ。
- ローカル実行: `python3 scripts/fetch_wikipedia_dramas.py 2026 spring`

> アプリ本体のコードは private リポジトリにあり、本リポジトリは生成スクリプトと公開JSONのみを持つ。
