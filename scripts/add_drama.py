#!/usr/bin/env python3
"""台帳 registry/dramas.json への手動追加・Wikipedia 紐付けヘルパー。

採番ミス・重複を避けるため、台帳の手編集より本スクリプトを推奨する。

## 追加(Wikipedia 未掲載の新作を手動登録)
  python3 scripts/add_drama.py \
    --title "GTO" --start 2026-07-20 \
    --network "関西テレビ フジテレビ系" --weekday 月曜 --time 22:00 \
    --slot "関西テレビ制作・月曜夜10時枠" --source https://www.ktv.jp/gto2026/
  → 次のIDを自動採番し wikipediaPageId=null で追加する。

## 紐付け(手動登録作品に Wikipedia 記事ができたとき)
  python3 scripts/add_drama.py --link d_0051 --pageid 123456
  → 既存レコードに pageid / 記事名を紐付ける(ID は不変・削除はしない)。
    週次 fetch が報告する「紐付け候補」を人が確認してから実行する。

ID契約(不変・削除禁止・振り直し禁止)は registry.py を参照。
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as R  # noqa: E402

API = "https://ja.wikipedia.org/w/api.php"
UA = ("DoramaJikanSeed/0.1 (personal hobby project; "
      "+https://github.com/micchymouse/dorama-seed)")


def wikipedia_title(pageid):
    """pageid から現在の記事名を引く。"""
    params = {"action": "query", "pageids": pageid, "format": "json",
              "formatversion": 2, "maxlag": 5}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        sys.exit(f"pageid={pageid} の記事が見つかりません")
    return pages[0]["title"]


def cmd_add(args):
    records = R.load_registry()
    year, cool = R.cool_of(args.start)
    if cool is None:
        sys.exit(f"--start は YYYY-MM-DD 形式で指定してください: {args.start!r}")

    weekday, time = args.weekday, args.time
    if weekday and weekday not in R.WEEKDAY_ORDER:
        sys.exit(f"--weekday は {' / '.join(R.WEEKDAY_ORDER)} のいずれか: "
                 f"{weekday!r}")
    if time and not re.fullmatch(r"\d{1,2}:\d{2}", time):
        sys.exit(f"--time は HH:MM で指定してください: {time!r}")
    weekday, time = R.broadcast_night(weekday, time)   # 深夜枠は前夜へ正規化

    norm = R.normalize_title(args.title)
    for r in records:
        if (R.resolve_cool(r) == (year, cool)
                and R.normalize_title(r.get("title", "")) == norm):
            print(f"警告: 同一クールに類似作品 `{r['id']}`「{r['title']}」が"
                  "既にあります。重複でなければ続行して構いません。",
                  file=sys.stderr)
            break

    new_id = R.next_id(records)
    record = {
        "id": new_id,
        "title": args.title,
        "wikipediaPageId": None,
        "wikipediaTitle": None,
        "network": args.network,
        "weekday": weekday,
        "time": time,
        "start": args.start,
        "episodes": args.episodes,
        "slot": args.slot,
        "year": year,
        "cool": cool,
        "source": args.source,
        "note": args.note or "Wikipedia未掲載。記事化されたら wikipediaPageId "
                             "を紐付ける(削除はしない)",
    }
    records.append(record)
    R.save_registry(records)
    print(f"追加しました: {new_id}「{args.title}」({year} {cool})")


def cmd_link(args):
    records = R.load_registry()
    rec = next((r for r in records if r.get("id") == args.link), None)
    if rec is None:
        sys.exit(f"ID {args.link} は台帳にありません")
    dup = next((r for r in records if r.get("wikipediaPageId") == args.pageid
                and r is not rec), None)
    if dup:
        sys.exit(f"pageid={args.pageid} は既に `{dup['id']}`「{dup['title']}」"
                 "に紐付いています")
    title = wikipedia_title(args.pageid)
    rec["wikipediaPageId"] = args.pageid
    rec["wikipediaTitle"] = title
    if args.title:
        rec["title"] = args.title
    R.save_registry(records)
    print(f"紐付けました: `{rec['id']}`「{rec['title']}」 ⇄ "
          f"記事「{title}」(pageid={args.pageid})")
    print("次回 fetch 実行時に放送情報が Wikipedia 側で更新されます。")


def main():
    p = argparse.ArgumentParser(
        description="台帳への手動追加・Wikipedia 紐付けヘルパー")
    p.add_argument("--title", help="番組名(追加時は必須)")
    p.add_argument("--start", help="放送開始日 YYYY-MM-DD(追加時は必須)")
    p.add_argument("--network", help="放送局")
    p.add_argument("--weekday", help="曜日(例: 月曜)")
    p.add_argument("--time", help="時刻(例: 22:00)")
    p.add_argument("--episodes", type=int, help="話数")
    p.add_argument("--slot", help="放送枠")
    p.add_argument("--source", help="出典URL(運用メモ)")
    p.add_argument("--note", help="メモ(運用メモ)")
    p.add_argument("--link", metavar="ID",
                   help="既存レコードに pageid を紐付ける(--pageid と併用)")
    p.add_argument("--pageid", type=int, help="紐付ける Wikipedia の pageid")
    args = p.parse_args()

    if args.link:
        if not args.pageid:
            p.error("--link には --pageid が必要です")
        cmd_link(args)
        return
    if not (args.title and args.start):
        p.error("追加には --title と --start が必要です"
                "(紐付けは --link ID --pageid N)")
    cmd_add(args)


if __name__ == "__main__":
    main()
