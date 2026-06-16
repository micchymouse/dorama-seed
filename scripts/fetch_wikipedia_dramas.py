#!/usr/bin/env python3
"""
ドラマじかん(仮) — Wikipedia から「今期の連続ドラマ一覧」を取得する試作スクリプト。

データ源: 日本語版 Wikipedia の MediaWiki API(公式・無料・規約クリア)
  1. Category:{年}年のテレビドラマ のメンバー(記事)を列挙
  2. 各記事の {{基礎情報 テレビ番組}} infobox を取得・解析
  3. 放送開始が指定クール(例: 4〜6月)の作品だけに絞り込む

出力: 標準出力にサマリ表 + JSON ファイル(放送局/曜日/時刻/開始日/話数)

使い方:
  python3 fetch_wikipedia_dramas.py 2026 spring
  (cool: winter=1-3 / spring=4-6 / summer=7-9 / autumn=10-12)
"""
import sys, json, re, time, urllib.parse, urllib.request

API = "https://ja.wikipedia.org/w/api.php"
UA = "DoramaJikanSeed/0.1 (personal hobby project; contact: +https://github.com/micchymouse/dorama-seed)"

COOLS = {"winter": (1, 3), "spring": (4, 6), "summer": (7, 9), "autumn": (10, 12)}


def api_get(params):
    params = {**params, "format": "json", "utf8": 1, "formatversion": 2}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def category_members(year):
    """Category:{year}年のテレビドラマ の記事(ns=0)を全件取得。"""
    titles, cont = [], None
    cat = f"Category:{year}年のテレビドラマ"
    while True:
        p = {"action": "query", "list": "categorymembers", "cmtitle": cat,
             "cmtype": "page", "cmlimit": 500}
        if cont:
            p["cmcontinue"] = cont
        d = api_get(p)
        titles += [m["title"] for m in d["query"]["categorymembers"]]
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
    return titles


def fetch_wikitext(titles):
    """タイトル群の wikitext を 50 件ずつ取得。{title: text} を返す。"""
    out = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        d = api_get({"action": "query", "prop": "revisions", "rvprop": "content",
                     "rvslots": "main", "titles": "|".join(chunk)})
        for pg in d["query"]["pages"]:
            if "revisions" in pg:
                out[pg["title"]] = pg["revisions"][0]["slots"]["main"]["content"]
        time.sleep(0.2)
    return out


def parse_infobox(text):
    """{{基礎情報 テレビ番組}} の `| key = value` を行単位で素朴に抽出。"""
    if "基礎情報 テレビ番組" not in text:
        return {}
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^\|\s*([^=|]+?)\s*=\s*(.*)$", line)
        if m:
            fields[m.group(1).strip()] = m.group(2).strip()
    return fields


def clean(s):
    if not s:
        return ""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<ref.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"\{\{(?:JPN|日本)\}\}", "日本", s)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", s)   # [[a|b]] -> b
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)             # [[a]]   -> a
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"'''?", "", s)
    s = re.sub(r"（予定）|\(予定\)|（[^）]*放送[^）]*）", "", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_title(s):
    """番組名に紛れ込む放送期間・注記(（…年…）〈予定〉【…】)を除去。"""
    s = re.sub(r"（[^）]*\d+年[^）]*）|\([^)]*\d+年[^)]*\)", "", s)
    s = re.sub(r"〈[^〉]*〉|【[^】]*】", "", s)
    return s.strip()


def parse_start_date(field):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", field)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_airtime(field):
    f = clean(field)
    wd = re.search(r"([月火水木金土日])曜", f)
    tm = re.search(r"(\d{1,2}):(\d{2})", f)
    return (wd.group(1) + "曜" if wd else None,
            f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else None)


def parse_episodes(fields):
    for k in ("放送回数", "回数", "話数", "全話数"):
        v = clean(fields.get(k, ""))
        m = re.search(r"(\d+)", v)
        if m:
            return int(m.group(1))
    return None


def main():
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    cool = sys.argv[2] if len(sys.argv) > 2 else "spring"
    lo, hi = COOLS[cool]

    print(f"[1/3] Category:{year}年のテレビドラマ を列挙中 ...", file=sys.stderr)
    titles = category_members(year)
    print(f"      {len(titles)} 件", file=sys.stderr)

    print("[2/3] 各記事の infobox を取得中 ...", file=sys.stderr)
    texts = fetch_wikitext(titles)

    print("[3/3] 解析・絞り込み中 ...", file=sys.stderr)
    rows = []
    for title, text in texts.items():
        fb = parse_infobox(text)
        if not fb:
            continue
        start = parse_start_date(fb.get("放送期間", "") or fb.get("放送開始", ""))
        if not (start and start[0] == year and lo <= start[1] <= hi):
            continue
        wd, tm = parse_airtime(fb.get("放送時間", ""))
        network = clean(fb.get("放送局", "")) or clean(fb.get("製作", "")) or clean(fb.get("制作", ""))
        rows.append({
            "title": strip_title(clean(fb.get("番組名", ""))) or re.sub(r"\s*\(.*\)$", "", title),
            "network": network,
            "weekday": wd,
            "time": tm,
            "start": f"{start[0]:04d}-{start[1]:02d}-{start[2]:02d}",
            "episodes": parse_episodes(fb),
            "slot": clean(fb.get("放送枠", "")) or None,
            "wikipedia": title,
        })

    rows.sort(key=lambda r: (r["weekday"] or "～", r["time"] or "99:99"))
    out = f"dramas_{year}_{cool}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"\n=== {year}年{cool}クール 連続ドラマ {len(rows)}件 ===")
    print(f"{'曜日':<3}{'時刻':<7}{'話数':>4}  {'放送局':<14}{'タイトル'}")
    print("-" * 70)
    for r in rows:
        print(f"{r['weekday'] or '?':<4}{r['time'] or '--:--':<7}"
              f"{(str(r['episodes']) if r['episodes'] else '?'):>3}話  "
              f"{(r['network'] or '?')[:13]:<14}{r['title']}")
    miss = sum(1 for r in rows if not (r["weekday"] and r["time"]))
    print("-" * 70)
    print(f"曜日/時刻が取れた: {len(rows)-miss}/{len(rows)}   話数が取れた: "
          f"{sum(1 for r in rows if r['episodes'])}/{len(rows)}")
    print(f"-> {out} に書き出しました")


if __name__ == "__main__":
    main()
