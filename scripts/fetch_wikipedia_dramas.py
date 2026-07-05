#!/usr/bin/env python3
"""ドラマのじかん — Wikipedia を情報源に台帳(registry)を突合・更新するスクリプト。

台帳 `registry/dramas.json` がマスター。Wikipedia は情報源の一つで、ここでは
指定した年・クールの記事を取得して台帳と突合し、放送情報を更新する。

処理の流れ:
  1. Category:{年}年のテレビドラマ のメンバー記事を列挙し、pageid 付きで
     wikitext を取得。{{基礎情報 テレビ番組}} infobox を解析。あわせて本文・
     脚注から放送休止日(hiatus)を精度優先で抽出する。
  2. 放送開始が指定クールに入る作品だけに絞り込む。
  3. pageid が台帳に一致 → その台帳レコードの放送情報を更新(Wikipedia 優先)。
  4. pageid 未知の新記事 → 台帳の同一クールに「正規化タイトルが類似する
     pageid=null のレコード(手動登録作品)」があれば**紐付け候補**として報告し、
     自動では紐付けない。類似が無ければ新IDを採番して台帳へ追加。
  5. 台帳から当該クールの配信JSONを再生成し、サマリ表と報告を表示。

冪等: 2回連続実行しても差分は出ず、既存IDが振り直されることもない。

使い方:
  python3 scripts/fetch_wikipedia_dramas.py 2026 spring
  (cool: winter=1-3 / spring=4-6 / summer=7-9 / autumn=10-12)

環境変数:
  SEED_REPORT — 指定するとそのファイルに Markdown 形式の報告(新規採番・
                紐付け候補)を追記する(週次ワークフローの PR 本文用)。
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as R  # noqa: E402

API = "https://ja.wikipedia.org/w/api.php"
UA = ("DoramaJikanSeed/0.1 (personal hobby project; "
      "+https://github.com/micchymouse/dorama-seed)")

COOLS = R.COOLS


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


def fetch_pages(titles):
    """タイトル群の wikitext を 50 件ずつ取得。{pageid, title, text} の配列を返す。

    redirects=1 でリダイレクトは転送先記事へ解決してから取得する。pageid は
    解決後の本体記事のもの(台帳との突合キー)。複数の転送が同一本体に集約
    された場合は pageid で 1 件へまとめる。
    """
    out = {}
    chunks = [titles[i:i + 50] for i in range(0, len(titles), 50)]
    for n, chunk in enumerate(chunks):
        d = api_get({"action": "query", "prop": "revisions", "rvprop": "content",
                     "rvslots": "main", "redirects": 1,
                     "titles": "|".join(chunk)})
        for pg in d.get("query", {}).get("pages", []):
            if pg.get("missing") or "pageid" not in pg:
                continue
            revs = pg.get("revisions")
            if not revs:
                continue
            content = revs[0].get("slots", {}).get("main", {}).get("content")
            if content:
                out[pg["pageid"]] = {"pageid": pg["pageid"],
                                     "title": pg["title"], "text": content}
        if n < len(chunks) - 1:           # 最終チャンク後は待たない
            time.sleep(0.2)
    return list(out.values())


# --- infobox 解析(Wikipedia wikitext → 放送情報) --------------------------

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
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", field.strip())  # ISO(手動)
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


# --- 放送休止日の抽出(記事本文・脚注 → hiatus) ---------------------------
#
# infobox には休止日の標準フィールドが無いため、本文・脚注(<ref>)・特記事項
# などの自由記述から「日付 × 休止」が明示的に結びつく記述だけを拾う。
# 方針は「再現率より精度」。誤って実放送回を休止扱いにするとユーザーがその回を
# 見逃す実害が出るため、曖昧・未確定・否定(「休止せず」等)は拾わない。
# 拾い漏れは無害(ユーザーが手動登録するだけ)。迷ったら拾わない。
#
# 「休止」語をアンカーに同一節内(句点や別の数字を跨がない)で隣接する日付だけを
# 対象にし、初回放送日・各話日付・視聴率の日付・再開日など無関係な日付を弾く。

# 抽出は「休止」語をアンカーにして、その語と同じ節内に隣接する日付だけを拾う。
# 日付が休止語の手前でも後ろでも対応する。分離要因は「。」「改行」「別の◯月◯日
# 日付」のみで、時刻(19時等)や視聴率の数字が間に入っても結合してよい
# (「◯月◯日は『特番…』(19時-…)放送のため休止」= 特番差し替え休止の典型表記)。
# 初回放送日・各話日付・視聴率の日付は「。」または「別の日付」で分離される。
_HIATUS_KW = re.compile(r"放送休止|休止")
_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_CLAUSE_BREAK = ("。", "\n")               # これを跨いだら別の節=結びつけない
# 休止の言及そのものを打ち消す語(否定・未確定)。休止語の近傍にあれば丸ごと不採用。
# 例:「休止せず」「休止しない」「休止の予定はない」「休止となる可能性がある」。
_HIATUS_VOID = re.compile(r"可能性|見込み|見通し|検討|かもしれ|未定|"
                          r"せず|しな|しませ|なけれ|ない|なし|無し")
# 休止語の後ろにある日付が「再開日・振替放送日・次回放送日・その日以降の通常放送」
# など実際に放送される日を指す手掛かり。該当すれば後ろ側の日付だけ捨てる。
# 実放送回を休止と誤検出しないための最重要ガード。左側の日付判定(=「◯月◯日は
# …休止」「◯月◯日から放送休止」)には作用しないため、休止開始日は取りこぼさない。
_HIATUS_RESUME = re.compile(r"再開|再放送|振替|振り替|繰り下げ|繰り上げ|繰下げ|繰上げ|"
                            r"次回|翌|ずれ込|順延|延期|以降|より|から")


def _date_before(left):
    """休止語の直前側で、節(。・改行)を跨がず隣接する最も近い日付 match。"""
    m = None
    for m in _DATE.finditer(left):
        pass                               # 最後 = 最も休止語寄りの日付
    if m is None or any(b in left[m.end():] for b in _CLAUSE_BREAK):
        return None
    return m


def _date_after(right):
    """休止語の直後側で、節(。・改行)を跨がず隣接する最も近い日付 match。"""
    m = _DATE.search(right)                # 最初 = 最も休止語寄りの日付
    if m is None or any(b in right[:m.start()] for b in _CLAUSE_BREAK):
        return None
    return m


def _hiatus_scan_text(text):
    """休止抽出用に wikitext の装飾を落として日付と語を近づける。

    コメント・リンク [[a|b]]→b / [[a]]→a・強調 '''・<ref> の囲みタグ・
    テンプレート {{…}} を外し、地の文だけを残す(休止は脚注内に書かれる
    ことが多い)。テンプレート引数の数字が日付判定へ紛れ込むのを防ぐ。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"</?ref[^>]*>", "", text)             # <ref…> </ref> の囲みだけ外す
    prev = None
    while prev != text and "{{" in text:                 # ネストは内側から除去
        prev = text
        text = re.sub(r"\{\{[^{}]*?\}\}", "", text)
    text = re.sub(r"\[\[[^\]|]*\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"<br\s*/?>", " ", text)
    return text.replace("'''", "").replace("''", "")


def _resolve_hiatus_year(month, day, base):
    """(月, 日) に年を補完して date を返す。start の年→翌年の順で、放送開始
    以降かつ約1年以内(大河・朝ドラの通年放送を許容)に収まる最初の候補を採る。"""
    for y in (base.year, base.year + 1):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if d >= base and (d - base).days <= 366:
            return d
    return None


def extract_hiatus(text, start):
    """記事 wikitext から放送休止日を抽出し、週次グリッド整列済みで返す。

    「休止/放送休止」語ごとに、同じ節内で隣接する日付(手前・後ろ)だけを
    候補にする。近傍に否定・未確定語があれば丸ごと捨て、後ろ側の日付が
    「再開日」を示す場合はその日付だけ捨てる(精度優先・迷ったら拾わない)。
    年は start から一意に補完し、`R.align_hiatus` で放送予定日へ整列する。
    """
    base = R.parse_iso(start)
    if base is None:
        return []
    scan = _hiatus_scan_text(text)
    found = set()
    for km in _HIATUS_KW.finditer(scan):
        left = scan[max(0, km.start() - 80):km.start()]
        right = scan[km.end():km.end() + 80]
        # 否定・未確定の判定は休止語と同じ節に限る(隣接文の「ない」等で
        # 正当な休止を打ち消さないよう「。」「改行」で頭打ちにする)。
        right_clause = re.split(r"[。\n]", right, maxsplit=1)[0]
        left_clause = re.split(r"[。\n]", left)[-1]
        if _HIATUS_VOID.search(right_clause[:24]) or _HIATUS_VOID.search(left_clause[-8:]):
            continue                       # 否定・未確定 → この休止言及は捨てる
        mb = _date_before(left)
        if mb:
            d = _resolve_hiatus_year(int(mb.group(1)), int(mb.group(2)), base)
            if d:
                found.add(d.isoformat())
        ma = _date_after(right)
        # 後ろ側の日付が再開日・振替日・次回放送日を指すなら実放送日なので除外。
        if ma and not _HIATUS_RESUME.search(right[:ma.end() + 10]):
            d = _resolve_hiatus_year(int(ma.group(1)), int(ma.group(2)), base)
            if d:
                found.add(d.isoformat())
    return R.align_hiatus(start, found)


def wiki_fields(page):
    """1 記事の wikitext から放送情報 dict を作る。対象外(infobox 無し・
    開始日不明)なら None。放送休止日(hiatus)は本文・脚注から抽出する。"""
    fb = parse_infobox(page["text"])
    if not fb:
        return None
    start = first_start_date(fb.get("放送期間", ""), fb.get("放送開始", ""),
                             fb.get("放送日", ""))
    if not start:
        return None
    wd, tm = parse_airtime(fb.get("放送時間", ""))
    wd, tm = R.broadcast_night(wd, tm)     # 深夜枠は前夜の曜日・24時超表記へ
    network = (clean(fb.get("放送局", "")) or clean(fb.get("製作", ""))
               or clean(fb.get("制作", "")))
    name = (strip_title(clean(fb.get("番組名", "")))
            or re.sub(r"\s*\([^()]*\)$", "", page["title"]))
    start_iso = f"{start[0]:04d}-{start[1]:02d}-{start[2]:02d}"
    return {
        "title": name,
        "network": network,
        "weekday": wd,
        "time": tm,
        "start": start_iso,
        "episodes": parse_episodes(fb),
        "slot": clean(fb.get("放送枠", "")) or None,
        "hiatus": extract_hiatus(page["text"], start_iso),
    }


# --- 台帳との突合 -----------------------------------------------------------

WIKI_INFO_KEYS = ("title", "network", "weekday", "time",
                  "start", "episodes", "slot")


def apply_wiki_fields(record, fields, page):
    """台帳レコードに Wikipedia の放送情報を反映(Wikipedia 優先)。

    放送情報 7 キー・wikipediaTitle・(start から導出した)year/cool を更新する。
    休止日(hiatus)だけは Wikipedia 検出分と既存分をマージする(抽出が空でも
    手動登録した休止を消さないため)。実際に変化があったら True。
    """
    changed = False
    for k in WIKI_INFO_KEYS:
        if record.get(k) != fields[k]:
            record[k] = fields[k]
            changed = True
    if record.get("wikipediaTitle") != page["title"]:
        record["wikipediaTitle"] = page["title"]
        changed = True
    year, cool = R.cool_of(fields["start"])
    if record.get("year") != year or record.get("cool") != cool:
        record["year"], record["cool"] = year, cool
        changed = True
    merged = R.align_hiatus(fields["start"],
                            (record.get("hiatus") or []) + fields["hiatus"])
    if (record.get("hiatus") or []) != merged:
        record["hiatus"] = merged
        changed = True
    return changed


def find_link_candidate(records, year, cool, title):
    """同一クールで pageid=null(手動登録)かつ正規化タイトルが一致するレコード。"""
    target = R.normalize_title(title)
    for r in records:
        if r.get("wikipediaPageId") is not None:
            continue
        if r.get("year") != year or r.get("cool") != cool:
            continue
        if R.normalize_title(r.get("title", "")) == target:
            return r
    return None


def reconcile(year, cool):
    """Wikipedia を取得して台帳を突合・更新し、(更新数, 新規, 候補) を返す。"""
    lo, hi = COOLS[cool]
    print(f"[1/4] Category:{year}年のテレビドラマ を列挙中 ...", file=sys.stderr)
    titles = category_members(year)
    print(f"      {len(titles)} 件", file=sys.stderr)

    print("[2/4] 各記事の infobox を取得中 ...", file=sys.stderr)
    pages = fetch_pages(titles)

    print("[3/4] 台帳と突合中 ...", file=sys.stderr)
    records = R.load_registry()
    by_pageid = {r["wikipediaPageId"]: r for r in records
                 if r.get("wikipediaPageId") is not None}

    updated, added, candidates = 0, [], []
    for page in pages:
        fields = wiki_fields(page)
        if not fields:
            continue
        y, m, _ = map(int, fields["start"].split("-"))
        if not (y == year and lo <= m <= hi):
            continue
        rec = by_pageid.get(page["pageid"])
        if rec:                            # 既知記事 → 放送情報を更新
            if apply_wiki_fields(rec, fields, page):
                updated += 1
            continue
        cand = find_link_candidate(records, year, cool, fields["title"])
        if cand:                           # 手動作品に記事ができた可能性 → 報告のみ
            candidates.append((page, fields["title"], cand))
            continue
        rec = {                            # 新規作品 → 採番して追加
            "id": R.next_id(records),
            "title": fields["title"],
            "wikipediaPageId": page["pageid"],
            "wikipediaTitle": page["title"],
            "network": fields["network"],
            "weekday": fields["weekday"],
            "time": fields["time"],
            "start": fields["start"],
            "episodes": fields["episodes"],
            "slot": fields["slot"],
            "hiatus": fields["hiatus"],
            "year": year,
            "cool": cool,
            "source": None,
            "note": None,
        }
        records.append(rec)
        by_pageid[page["pageid"]] = rec
        added.append(rec)

    R.save_registry(records)
    return records, updated, added, candidates


def _w(s):
    """表示幅(全角=2, 半角=1)を返す。サマリ表の桁揃え用。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, width):
    return s + " " * max(0, width - _w(s))


def print_summary(rows, year, cool):
    print(f"\n=== {year}年{cool}クール 連続ドラマ {len(rows)}件 ===")
    print(_pad("ID", 8) + _pad("曜日", 6) + _pad("時刻", 7) + _pad("話数", 6)
          + _pad("放送局", 16) + "タイトル")
    print("-" * 78)
    for r in rows:
        ep = f"{r['episodes']}話" if r["episodes"] else "?"
        print(_pad(r["id"], 8) + _pad(r["weekday"] or "?", 6)
              + _pad(r["time"] or "--:--", 7) + _pad(ep, 6)
              + _pad((r["network"] or "?")[:14], 16) + r["title"])
    miss = sum(1 for r in rows if not (r["weekday"] and r["time"]))
    manual_n = sum(1 for r in rows if r["wikipedia"] is None)
    with_hiatus = [r for r in rows if r["hiatus"]]
    print("-" * 78)
    print(f"曜日/時刻が取れた: {len(rows) - miss}/{len(rows)}   話数が取れた: "
          f"{sum(1 for r in rows if r['episodes'])}/{len(rows)}")
    if manual_n:
        print(f"うち手動補完(wikipediaTitle=null): {manual_n}件")
    if with_hiatus:
        print(f"放送休止日を検出: {len(with_hiatus)}件(要確認・誤検出は台帳で修正)")
        for r in with_hiatus:
            print(f"  {r['id']} {r['title']}: {', '.join(r['hiatus'])}")


def write_report(added, candidates, rows, year, cool):
    """新規採番・紐付け候補・検出した休止日を Markdown で報告。SEED_REPORT があれば追記。"""
    lines = []
    if added:
        lines.append(f"### {year} {cool}: 新規採番 {len(added)}件")
        for r in added:
            lines.append(f"- `{r['id']}` {r['title']}(pageid={r['wikipediaPageId']})")
    with_hiatus = [r for r in rows if r["hiatus"]]
    if with_hiatus:
        lines.append(f"### {year} {cool}: 放送休止日を検出 {len(with_hiatus)}件"
                     "(精度優先の自動抽出・誤検出がないか要確認)")
        for r in with_hiatus:
            lines.append(f"- `{r['id']}` {r['title']}: {', '.join(r['hiatus'])}")
    if candidates:
        lines.append(f"### {year} {cool}: 紐付け候補 {len(candidates)}件"
                     "(手動作品に記事ができた可能性・要確認)")
        for page, title, cand in candidates:
            lines.append(
                f"- 記事「{page['title']}」(pageid={page['pageid']}) ⇄ "
                f"台帳 `{cand['id']}`「{cand['title']}」"
                f" → `python3 scripts/add_drama.py --link {cand['id']} "
                f"--pageid {page['pageid']}` で紐付け")
    if not lines:
        return
    report = "\n".join(lines)
    print("\n" + report, file=sys.stderr)
    path = os.environ.get("SEED_REPORT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(report + "\n\n")


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
    year = int(year_s)

    records, updated, added, candidates = reconcile(year, cool)

    print("[4/4] 配信JSONを再生成中 ...", file=sys.stderr)
    cool_records = [r for r in records if R.resolve_cool(r) == (year, cool)]
    out, rows = R.write_seed(cool_records, year, cool)

    print_summary(rows, year, cool)
    print(f"更新: {updated}件 / 新規採番: {len(added)}件 / "
          f"紐付け候補: {len(candidates)}件")
    write_report(added, candidates, rows, year, cool)
    print(f"-> 台帳: {R.REGISTRY_PATH}")
    print(f"-> 配信: {out}")


if __name__ == "__main__":
    main()
