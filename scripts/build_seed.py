#!/usr/bin/env python3
"""台帳 registry/dramas.json から配信JSONを生成する。

台帳を (year, cool) ごとに絞り込み、公開キーだけを射影して
`public/seed/dramas_{year}_{cool}.json` を書き出す。Wikipedia へはアクセスせず、
レビュー済みの台帳をそのまま配信物へ変換する(Pages デプロイ用)。

使い方:
  python3 scripts/build_seed.py           # 台帳の全年・全クールを生成
  python3 scripts/build_seed.py 2026      # 指定年だけ生成
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as R  # noqa: E402


def main():
    args = sys.argv[1:]
    only_year = int(args[0]) if args and args[0].isdigit() else None

    records = R.load_registry()
    if not records:
        sys.exit(f"台帳が空です: {R.REGISTRY_PATH}")

    groups = R.group_by_cool(records)
    written = 0
    for (year, cool), recs in sorted(groups.items()):
        if only_year is not None and year != only_year:
            continue
        out, rows = R.write_seed(recs, year, cool)
        print(f"{out.relative_to(R.ROOT)}: {len(rows)}件")
        written += 1
    if written == 0:
        sys.exit("該当するクールがありませんでした")
    print(f"-> {written} クール分を {R.SEED_DIR.relative_to(R.ROOT)} に生成しました")


if __name__ == "__main__":
    main()
