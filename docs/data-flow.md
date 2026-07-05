# データ生成と自動化の流れ

ドラマのじかん用シードJSONの**作られ方**と、GitHub に push したあとに**定期的に動く処理**をまとめる。
仕様の詳細は [`CLAUDE.md`](../CLAUDE.md) / [`README.md`](../README.md) を参照。

## 登場人物

- **Wikipedia** — 情報源(週1で参照するだけ)。マスターではない。
- **台帳 `registry/dramas.json`** — 全作品のマスター。永続ID `d_0001…` の源泉。**唯一コミットする正データ**。
- **配信JSON `public/seed/*.json`** — 台帳から生成する投影物。コミットしない(`.gitignore` 対象)。
- **GitHub Actions**(`seed.yml` / `deploy.yml`) — 突合PR作成 と Pages デプロイ。
- **アプリ** — 配信URLから取得(サーバー費ゼロ)。

## 全体像

```
┌──────────────┐   情報源(週1で参照)   ┌────────────────────────────┐
│  Wikipedia   │ ────────────────────▶ │  台帳 registry/dramas.json    │◀── マスター(唯一の正)
│ (記事+pageid)│                       │  ・全作品                      │    永続IDの源泉
└──────────────┘                       │  ・d_0001… の連番ID(不変)     │
                                       └───────────┬────────────────┘
                                                   │ 射影(年・クールで絞り+公開キーのみ)
                                                   ▼
                                       ┌────────────────────────────┐
                                       │ 配信JSON public/seed/*.json    │
                                       │ (id付き・クール別)              │
                                       └───────────┬────────────────┘
                                                   │ GitHub Pages で静的配信
                                                   ▼
                                       ┌────────────────────────────┐
                                       │ アプリ「ドラマのじかん」         │
                                       │ URLから取得(サーバー費ゼロ)     │
                                       └────────────────────────────┘
```

**台帳が正**なので、配信JSONはいつでも作り直せる。だから配信JSONはコミットせず、台帳だけをコミットする。

## スクリプトの役割

| スクリプト | 役割 | Wikipedia接続 |
|---|---|---|
| `scripts/fetch_wikipedia_dramas.py` | Wikipedia と台帳を突合・更新(書き込むのは台帳) | 要 |
| `scripts/build_seed.py` | 台帳 → 配信JSON を生成 | 不要 |
| `scripts/add_drama.py` | 手動で台帳に追加・pageid紐付け(採番ミス防止) | 追加は不要 / 紐付けは要 |
| `scripts/registry.py` | 上記の共有ユーティリティ(ID採番・正規化・射影など) | — |

### fetch(突合)の中身

```
1. Wikipedia から当該クールの記事を pageid 付きで取得(本文・脚注から放送休止日も抽出)
2. pageid が台帳にある     → その行の放送情報を更新(Wikipedia優先)/ 休止日はマージ
3. pageid 未知の新記事
     ├ 同クールに似た手動作品(pageid=null)がある → 「紐付け候補」として報告のみ(自動紐付けしない)
     └ 似た作品がない                          → 新IDを採番して台帳に追加
4. 台帳を保存し、配信JSONを再生成
```

**放送休止日(hiatus)**: あらかじめ判明している休止日(年末年始特番・スポーツ中継延長など)を
シードに載せ、ユーザーが作品を追加した瞬間に反映する。抽出は**精度優先**(「◯月◯日×休止」が明示された
記述だけ・否定や曖昧は捨てる)。配信時は放送開始 `start` からの**週次グリッドへ整列**して出す
(消費側は `start + 7*n日` としか一致判定しないため)。休止なし・不明は `[]`。詳細は
[`CLAUDE.md`](../CLAUDE.md) の「休止日」節。

**2回実行しても差分が出ない(冪等)／既存IDは絶対に振り直さない**のが肝。

## push 後に定期的に動く処理(GitHub Actions 2本)

### ① `seed.yml` — 毎週月曜 18:00 JST(＋手動実行)

台帳を最新Wikipediaと突合し、**変更があればPRを作るだけ**(直接公開はしない)。

```
毎週月曜 18:00 JST
   │
   ▼
当年の winter/spring/summer/autumn を順に fetch で突合
   │
   ├─ 台帳に差分なし ─────────────▶ PRを作らない(何もしない週)
   │
   └─ 台帳に差分あり
          │
          ▼
   PRを自動作成(peter-evans/create-pull-request)
   本文に「新規採番した作品」「紐付け候補」を列挙
          │
          ▼
   👤 レビュー → マージ
```

**なぜPR方式か**: 新規採番や「手動作品に記事ができた(紐付け候補)」を人が確認してから取り込むため。
誤ったIDの付与や二重登録を防ぐ。

### ② `deploy.yml` — 台帳が main に入った瞬間(PRマージ or 手動編集の push)

```
main への push(registry/dramas.json が変わった)
   │
   ▼
build_seed.py で台帳 → 配信JSON を生成(Wikipediaにはアクセスしない)
   │
   ▼
GitHub Pages へデプロイ
   │
   ▼
https://micchymouse.github.io/dorama-seed/seed/dramas_2026_summer.json が更新
   │
   ▼
アプリが次回取得時に反映
```

デプロイはレビュー済みの台帳を機械的に配信物へ変換するだけ。Wikipediaの状態には左右されない。

## シナリオ例: 手動登録した「GTO」が記事化された週

```
月曜18:00  seed.yml が fetch 実行
           → GTOのWikipedia記事(pageid付き)を発見
           → 台帳に手動GTO(d_0041, pageid=null)がある → 「紐付け候補」として報告
           → PR作成(本文に「d_0041 に紐付け推奨」と表示)

あなた     PRを見て確認 → コマンドで紐付け:
             python3 scripts/add_drama.py --link d_0041 --pageid <記事のpageid>
           → d_0041 に pageid が入る(IDは d_0041 のまま不変・削除しない)
           → これをコミット/マージ

マージ後   deploy.yml が発火 → 配信JSON更新
           GTOは今後 Wikipedia 側の情報で自動更新されるが、IDは永久に d_0041
```

**記事化・改名が起きてもアプリが握るID `d_0041` は永遠に変わらない** → 「ドラマ単位のチャット」を
全ユーザー間で一意に紐付けられる。これが台帳方式の狙い。

## あなたが普段やること

| やりたいこと | 操作 |
|---|---|
| 何もしない週 | 何もしない(差分がなければPRも来ない) |
| 新ドラマが増えた週 | 来たPRの中身を確認して**マージするだけ** |
| Wikipedia未掲載の新作を早めに載せたい | `add_drama.py --title … --start …` で手動追加 → コミット |
| 手動作品が記事化された | PRの「紐付け候補」を見て `add_drama.py --link d_xxxx --pageid N` |

## ローカルでの確認手順

```bash
# 台帳 → 配信JSON を生成(オフライン。まず試すならこれ)
python3 scripts/build_seed.py

# Wikipedia突合 + 冪等性チェック(要ネット)
python3 scripts/fetch_wikipedia_dramas.py 2026 summer
/bin/cp registry/dramas.json /tmp/reg1.json
python3 scripts/fetch_wikipedia_dramas.py 2026 summer
diff /tmp/reg1.json registry/dramas.json && echo "冪等OK(差分なし)"

# 手動追加(オフライン)。試したら git checkout registry/dramas.json で戻せる
python3 scripts/add_drama.py --title "テスト作品" --start 2026-10-05 --network TBS --weekday 火曜 --time 22:00
```

> ⚠️ `fetch` を winter/autumn など未移行のクールで実行すると、その期の実在ドラマが新規採番されて
> 台帳に追記される(正当な動作)。試して戻すなら `git checkout registry/dramas.json`。

## ID契約(再掲・厳守)

- 形式 `d_0001`(`d_` + 4桁連番)。次のIDは既存連番の最大値 + 1。
- 採番後は**永久不変**。レコード削除・ID再利用・振り直しは**禁止**。
- 手動作品は `wikipediaPageId: null` で始まり、記事化されたら**同じレコードにpageidを紐付ける**(削除しない)。
- IDは無意味な連番で Wikipedia とは無関係。突合キーは記事名ではなく **pageid**(記事名は改名されうるため)。
