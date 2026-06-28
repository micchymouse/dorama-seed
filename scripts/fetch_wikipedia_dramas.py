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
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

API = "https://ja.wikipedia.org/w/api.php"
UA = "DoramaJikanSeed/0.1 (personal hobby project; contact: +https://github.com/micchymouse/dorama-seed)"

COOLS = {"winter": (1, 3), "spring": (4, 6), "summer": (7, 9), "autumn": (10, 12)}

# 曜日の並び順(月曜が先頭、日曜が末尾)
WEEKDAY_ORDER = {"月曜": 0, "火曜": 1, "水曜": 2, "木曜": 3,
                 "金曜": 4, "土曜": 5, "日曜": 6}


def _minutes(tm):
    """"HH:MM" を分に変換。取れなければ None。"""
    if not tm:
        return None
    h, m = tm.split(":")
    return int(h) * 60 + int(m)


def sort_key(r):
    """月曜先頭・時刻降順で並べるためのソートキー。

    日曜深夜枠(放送日付は月曜だが 5:00 未満)は前日=日曜の位置に寄せる。
    Wikipedia が "日曜 24:50" のように 24時超で表す深夜枠も、+24時間扱いで
    同じ位置に揃う。曜日・時刻が取れない作品は各グループの末尾へ送る。
    """
    idx = WEEKDAY_ORDER.get(r["weekday"], 99)
    mins = _minutes(r["time"])
    # 早朝(5:00 未満)は前日の深夜枠とみなし、前日へ +24時間で寄せる
    if idx != 99 and mins is not None and mins < 5 * 60:
        idx = (idx - 1) % 7
        mins += 24 * 60
    # 曜日は昇順(月曜先頭)、時刻は降順。未取得は末尾。
    return (idx, -mins if mins is not None else float("inf"))


def api_get(params, retries=4):
    """MediaWiki API を叩いて JSON を返す。

    瞬断・タイムアウト・429/5xx・maxlag は指数バックオフで再試行する。
    Wikipedia 推奨の maxlag=5 を付与し、API エラー応答は例外化する。
    """
    params = {**params, "format": "json", "utf8": 1,
              "formatversion": 2, "maxlag": 5}
    url = API + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        wait = 2 ** attempt
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or 500 <= e.code < 600:
                ra = e.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, int(ra))
                print(f"      API {e.code}; {wait}秒待機して再試行 "
                      f"({attempt + 1}/{retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as e:
            last_err = e
            print(f"      通信エラー({e}); {wait}秒待機して再試行 "
                  f"({attempt + 1}/{retries})", file=sys.stderr)
            time.sleep(wait)
            continue
        # maxlag 等のエラー応答(HTTP 200 で返ることがある)
        if isinstance(data, dict) and "error" in data:
            if data["error"].get("code") == "maxlag":
                print(f"      maxlag; {wait}秒待機して再試行 "
                      f"({attempt + 1}/{retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"API error: {data['error']}")
        return data
    raise RuntimeError(f"API リクエストが {retries} 回失敗しました: {last_err}")


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
        members = d.get("query", {}).get("categorymembers", [])
        titles += [m["title"] for m in members]
        cont = d.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        time.sleep(0.2)
    return titles


def fetch_wikitext(titles):
    """タイトル群の wikitext を 50 件ずつ取得。{title: text} を返す。

    redirects=1 でリダイレクトは転送先記事へ解決してから取得する。
    (例: カテゴリには転送ページ「刑事、ふりだしに戻る」だけが載り、
     infobox は転送先「初恋リバース〜…」本体にある、という構造を拾うため)
    返すキーは解決後の本体記事タイトル。複数の転送が同一本体に集約された
    場合は自然に1件へまとまる。
    """
    out = {}
    chunks = [titles[i:i + 50] for i in range(0, len(titles), 50)]
    for n, chunk in enumerate(chunks):
        d = api_get({"action": "query", "prop": "revisions", "rvprop": "content",
                     "rvslots": "main", "redirects": 1,
                     "titles": "|".join(chunk)})
        for pg in d.get("query", {}).get("pages", []):
            revs = pg.get("revisions")
            if not revs:
                continue
            content = revs[0].get("slots", {}).get("main", {}).get("content")
            if content:
                out[pg["title"]] = content
        if n < len(chunks) - 1:           # 最終チャンク後は待たない
            time.sleep(0.2)
    return out


def _infobox_block(text):
    """{{基礎情報 テレビ番組 ... }} のブロック文字列を波括弧の対応で切り出す。"""
    m = re.search(r"\{\{\s*基礎情報 テレビ番組", text)
    if not m:
        return ""
    start, depth, i, n = m.start(), 0, m.start(), len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return text[start:]                    # 閉じが無ければ末尾まで


def parse_infobox(text):
    """{{基礎情報 テレビ番組}} ブロックだけを対象に key=value を抽出。

    トップレベル(深さ0)の `|` で分割するため、ネストしたテンプレートや
    [[リンク|表示]] 内の `|`、複数行にまたがる値も正しく扱える。
    """
    block = _infobox_block(text)
    if not block:
        return {}
    inner = block[2:]                      # 先頭 {{ を除去
    if inner.endswith("}}"):
        inner = inner[:-2]                 # 末尾 }} を除去
    parts, buf, depth, i, n = [], [], 0, 0, len(inner)
    while i < n:
        two = inner[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth = max(0, depth - 1)
            buf.append(two)
            i += 2
            continue
        c = inner[i]
        if c == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    fields = {}
    for part in parts[1:]:                 # parts[0] はテンプレート名
        eq = part.find("=")
        if eq == -1:
            continue
        key = part[:eq].strip()
        if key:
            fields[key] = part[eq + 1:].strip()
    return fields


def clean(s):
    if not s:
        return ""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<ref.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"\{\{(?:JPN|日本)\}\}", "日本", s)
    # リスト系テンプレートは囲みだけ外して項目を残す
    s = re.sub(r"\{\{\s*(?:Plainlist|Unbulleted list|Ubl|ublist|flatlist|hlist)"
               r"\s*\|", "", s, flags=re.I)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", s)   # [[a|b]] -> b
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)             # [[a]]   -> a
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"</?(?:small|span|div|ul|li)[^>]*>", " ", s)
    s = re.sub(r"'''?", "", s)
    s = re.sub(r"^[\*#:;]+", " ", s, flags=re.M)          # 箇条書き記号
    # 残存テンプレートを内側から除去(ネスト対応)
    prev = None
    while prev != s and "{{" in s:
        prev = s
        s = re.sub(r"\{\{[^{}]*?\}\}", "", s)
    s = s.replace("{{", "").replace("}}", "")            # 取り残した括弧
    s = re.sub(r"（予定）|\(予定\)|（[^）]*放送[^）]*）", "", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_title(s):
    """番組名に紛れ込む放送期間・注記(（…年…）〈予定〉【…】)を除去。"""
    s = re.sub(r"（[^）]*\d+年[^）]*）|\([^)]*\d+年[^)]*\)", "", s)
    s = re.sub(r"〈[^〉]*〉|【[^】]*】", "", s)
    return s.strip()


def parse_start_date(field):
    """放送開始日を (年, 月, 日) で返す。取れなければ None。

    対応する表記:
      - 2026年4月8日
      - 2026年4月          (日が無ければ 1 日扱い)
      - {{開始終了日と経過時間|2026|4|8|…}} / {{開始日|2026|4|8}} /
        {{Start date|2026|4|8}} など、最初の 3 数値を年月日とみなすテンプレート
    """
    if not field:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", field)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\{\{[^{}]*?\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})",
                  field)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})年(\d{1,2})月", field)          # 年月のみ
    if m:
        return (int(m.group(1)), int(m.group(2)), 1)
    return None


def first_start_date(*fields):
    """複数フィールドを順に試し、最初にパースできた開始日を返す。"""
    for f in fields:
        d = parse_start_date(f)
        if d:
            return d
    return None


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


def _w(s):
    """表示幅(全角=2, 半角=1)を返す。サマリ表の桁揃え用。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, width):
    return s + " " * max(0, width - _w(s))


def build_rows(texts, year, lo, hi):
    """取得済み wikitext から対象クールの作品行を組み立てる(重複除去込み)。"""
    rows, seen = [], set()
    for title, text in texts.items():
        fb = parse_infobox(text)
        if not fb:
            continue
        start = first_start_date(fb.get("放送期間", ""), fb.get("放送開始", ""),
                                 fb.get("放送日", ""))
        if not (start and start[0] == year and lo <= start[1] <= hi):
            continue
        wd, tm = parse_airtime(fb.get("放送時間", ""))
        network = (clean(fb.get("放送局", "")) or clean(fb.get("製作", ""))
                   or clean(fb.get("制作", "")))
        name = (strip_title(clean(fb.get("番組名", "")))
                or re.sub(r"\s*\([^()]*\)$", "", title))
        if name in seen:                  # 同名番組の重複は先勝ち
            continue
        seen.add(name)
        rows.append({
            "title": name,
            "network": network,
            "weekday": wd,
            "time": tm,
            "start": f"{start[0]:04d}-{start[1]:02d}-{start[2]:02d}",
            "episodes": parse_episodes(fb),
            "slot": clean(fb.get("放送枠", "")) or None,
            "wikipedia": title,
        })
    rows.sort(key=sort_key)
    return rows


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return
    year_s = args[0] if len(args) > 0 else "2026"
    cool = args[1] if len(args) > 1 else "spring"
    if not re.fullmatch(r"\d{4}", year_s):
        sys.exit(f"年は4桁の西暦で指定してください: {year_s!r}")
    if cool not in COOLS:
        sys.exit(f"cool は {' / '.join(COOLS)} のいずれかで指定してください: "
                 f"{cool!r}")
    year, (lo, hi) = int(year_s), COOLS[cool]

    print(f"[1/3] Category:{year}年のテレビドラマ を列挙中 ...", file=sys.stderr)
    titles = category_members(year)
    print(f"      {len(titles)} 件", file=sys.stderr)

    print("[2/3] 各記事の infobox を取得中 ...", file=sys.stderr)
    texts = fetch_wikitext(titles)

    print("[3/3] 解析・絞り込み中 ...", file=sys.stderr)
    rows = build_rows(texts, year, lo, hi)

    out = f"dramas_{year}_{cool}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"\n=== {year}年{cool}クール 連続ドラマ {len(rows)}件 ===")
    print(_pad("曜日", 6) + _pad("時刻", 7) + _pad("話数", 6)
          + _pad("放送局", 16) + "タイトル")
    print("-" * 70)
    for r in rows:
        ep = f"{r['episodes']}話" if r["episodes"] else "?"
        print(_pad(r["weekday"] or "?", 6) + _pad(r["time"] or "--:--", 7)
              + _pad(ep, 6) + _pad((r["network"] or "?")[:14], 16) + r["title"])
    miss = sum(1 for r in rows if not (r["weekday"] and r["time"]))
    print("-" * 70)
    print(f"曜日/時刻が取れた: {len(rows) - miss}/{len(rows)}   話数が取れた: "
          f"{sum(1 for r in rows if r['episodes'])}/{len(rows)}")
    print(f"-> {out} に書き出しました")


if __name__ == "__main__":
    main()
