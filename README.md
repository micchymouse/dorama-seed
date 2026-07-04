# dorama-seed

ドラマのじかん アプリ用のシードJSON配信リポジトリ。
日本語版 Wikipedia を情報源に「各クールの新ドラマ一覧」を生成し、GitHub Pages で静的配信する。

## 配信URL

```
https://micchymouse.github.io/dorama-seed/seed/dramas_{year}_{cool}.json
```

- `cool` = `winter`(1-3月)/ `spring`(4-6月)/ `summer`(7-9月)/ `autumn`(10-12月)
- 各作品には永続ID `id`(`d_0001` 形式)が付く。
  ドラマを全ユーザー間で一意に特定するためのキーで、一度採番したら不変。

```jsonc
{
  "id": "d_0041",          // 永続ID(不変)
  "title": "GTO",
  "network": "関西テレビ フジテレビ系",
  "weekday": "月曜",
  "time": "22:00",
  "start": "2026-07-20",
  "episodes": null,
  "slot": "関西テレビ制作・月曜夜10時枠",
  "wikipedia": null         // Wikipedia 記事名。未掲載作品は null
}
```

## 台帳(registry)

`registry/dramas.json` が全作品のマスター。永続IDはここで採番する。
配信JSONは台帳を年・クールで絞って生成した投影物にすぎない。

- **ID契約**: `d_` + 4桁連番。採番後は不変。削除・再利用・振り直しは禁止。
- Wikipedia は情報源の一つ。記事名の改名や記事化に左右されない安定IDを台帳で持つ。

## スクリプト

```bash
# 台帳を Wikipedia と突合・更新し、配信JSONを再生成(冪等)
python3 scripts/fetch_wikipedia_dramas.py 2026 summer

# 台帳から全クールの配信JSONを生成(Pages デプロイ用・Wikipedia 不使用)
python3 scripts/build_seed.py

# Wikipedia 未掲載の新作を手動登録(次のIDを自動採番)
python3 scripts/add_drama.py --title "GTO" --start 2026-07-20 \
  --network "関西テレビ フジテレビ系" --weekday 月曜 --time 22:00

# 手動登録作品に記事ができたら pageid を紐付け(IDは不変)
python3 scripts/add_drama.py --link d_0041 --pageid 123456
```

標準ライブラリのみで動作(追加依存なし、Python 3.12 を想定)。

## 自動化

- `.github/workflows/seed.yml` — 毎週月曜 18:00 JST に台帳を突合し、変更があれば **PR を作成**。
  新規採番・紐付け候補を人がレビューしてマージする。
- `.github/workflows/deploy.yml` — 台帳が main に入ると配信JSONを生成し Pages へデプロイ。

データ生成と自動化の全体像は [`docs/data-flow.md`](docs/data-flow.md) を参照。
