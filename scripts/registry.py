#!/usr/bin/env python3
"""ドラマ台帳(registry)の共有ユーティリティ。

台帳 `registry/dramas.json` は全ユーザー間で作品を一意に特定する永続IDの
マスターである。Wikipedia は情報源の一つに過ぎず、ID は Wikipedia とは無関係な
無意味な連番。

## ID契約(厳守)
- 形式: `d_0001`(`d_` + 4桁ゼロ埋め連番)。
- 一度採番したIDは**永久不変**。レコード削除・IDの再利用・振り直しは禁止。
- 次のIDは既存レコードの連番の最大値 + 1。

## 台帳レコードの主なキー
- `id`          : `d_xxxx`(不変)
- `title`       : 番組名
- `wikipediaPageId` / `wikipediaTitle` : Wikipedia の pageid / 記事名(nullable)
- `network` / `weekday` / `time` / `start` / `episodes` / `slot` : 放送情報
- `hiatus`      : あらかじめ判明している放送休止日の配列(ISO日付・nullable)。
                  配信時は `start` からの週次グリッドへ整列して出す(`align_hiatus`)。
- `year` / `cool` : 配信先クール(`start` から導出、保存もする)
- `source` / `note` : 手動登録時の運用メモ(配信JSONには出さない)

配信JSON(`public/seed/dramas_{year}_{cool}.json`)は台帳を年・クールで絞り、
`PUBLIC_KEYS` だけを射影して生成する(台帳 = マスター、配信 = その投影)。
"""
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "registry" / "dramas.json"
SEED_DIR = ROOT / "public" / "seed"

COOLS = {"winter": (1, 3), "spring": (4, 6), "summer": (7, 9), "autumn": (10, 12)}

ID_PREFIX = "d_"
ID_WIDTH = 4

# 配信JSONに出す公開キー(この順序・この末尾の hiatus まで。既存アプリとの互換維持)。
# hiatus は末尾に足しただけ(現行アプリは未知キーを無視、非対応でも前方互換)。
PUBLIC_KEYS = ("id", "title", "network", "weekday", "time",
               "start", "episodes", "slot", "wikipedia", "hiatus")

# 曜日の並び順(月曜が先頭、日曜が末尾)。番組表式ソートに使う。
WEEKDAY_ORDER = {"月曜": 0, "火曜": 1, "水曜": 2, "木曜": 3,
                 "金曜": 4, "土曜": 5, "日曜": 6}
WEEKDAYS = list(WEEKDAY_ORDER)


# --- 台帳の入出力 -----------------------------------------------------------

def load_registry(path=REGISTRY_PATH):
    """台帳(レコードの配列)を読む。無ければ空リスト。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path} は作品レコードの配列である必要があります")
    return data


def save_registry(records, path=REGISTRY_PATH):
    """台帳を id 昇順で書き出す(差分を安定させるため常に整列)。"""
    records = sorted(records, key=lambda r: parse_id(r.get("id", "")) or 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --- ID 採番 ----------------------------------------------------------------

def parse_id(s):
    """`d_0042` -> 42。形式不正なら None。"""
    m = re.fullmatch(rf"{re.escape(ID_PREFIX)}(\d+)", s or "")
    return int(m.group(1)) if m else None


def max_id_num(records):
    """既存レコードの連番の最大値(無ければ 0)。"""
    nums = [parse_id(r.get("id", "")) for r in records]
    return max([n for n in nums if n is not None], default=0)


def format_id(num):
    return f"{ID_PREFIX}{num:0{ID_WIDTH}d}"


def next_id(records):
    """次に採番すべきID(連番の最大値 + 1)。"""
    return format_id(max_id_num(records) + 1)


# --- クール判定・タイトル正規化 --------------------------------------------

def parse_iso(s):
    """`YYYY-MM-DD` を datetime.date に。形式不正・実在しない日付なら None。"""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", (s or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def align_hiatus(start, dates):
    """休止日を「放送開始日 start から7日おきの週次グリッド」に整列して返す。

    消費側アプリは休止日を `start + 7*n日` の日付としか一致判定しないため、
    グリッドから1日でもズレた日付は無視される。ここで各日付をその日が属する
    「週」の放送予定日(= その作品の曜日)へスナップし、重複除去・昇順ソートした
    ISO日付文字列の配列を返す。整列先が一意に決まる(週の中点は整数日では
    生じない)ので冪等。start 不明・start 以前の日付は捨てる。
    """
    base = parse_iso(start)
    if base is None:
        return []
    out = set()
    for d in dates or []:
        dt = d if isinstance(d, date) else parse_iso(d)
        if dt is None:
            continue
        delta = (dt - base).days
        if delta < 0:                      # 放送開始より前 = 無効
            continue
        n = round(delta / 7)               # 最も近い放送予定週へ寄せる
        out.add((base + timedelta(days=7 * n)).isoformat())
    return sorted(out)


def cool_of(start):
    """開始日 `YYYY-MM-DD` から (年, クール名) を返す。判定不能なら (None, None)。"""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", start or "")
    if not m:
        return (None, None)
    year, month = int(m.group(1)), int(m.group(2))
    for name, (lo, hi) in COOLS.items():
        if lo <= month <= hi:
            return (year, name)
    return (year, None)


# 正規化で取り除く区切り・記号(長音符 ー は語を変えうるので残す)。
_SEP_RE = re.compile(
    r"[\s　\-‐‑‒–—―~・:：!！?？、。,\.\"'’“”"
    r"「」『』【】〈〉《》()（）\[\]［］#*]"
)


def normalize_title(s):
    """比較用にタイトルを正規化する。

    NFKC・小文字化・波ダッシュ(〜～)統一・末尾の曖昧さ回避括弧
    (「(テレビドラマ)」「(漫画)」等)の除去・空白/記号の除去を行う。
    手動登録作品と Wikipedia 記事の「類似タイトル」判定に使う。
    """
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("〜", "~").replace("～", "~")
    s = re.sub(r"[\(（][^）\)]*[\)）]\s*$", "", s)   # 末尾の (…) を除去
    return _SEP_RE.sub("", s)


# --- 番組表式の曜日・時刻 ---------------------------------------------------

def _minutes(tm):
    """"HH:MM" を分に変換。取れなければ None。"""
    if not tm:
        return None
    h, m = tm.split(":")
    return int(h) * 60 + int(m)


def broadcast_night(weekday, time):
    """放送時刻を「番組表式」に正規化して (曜日, 時刻) を返す。

    深夜枠(0〜4 時台)はその放送が属する前夜の曜日へ寄せ、24 時超表記にする。
      例: ("火曜", "01:29") -> ("月曜", "25:29")  # 火曜未明 = 月曜深夜
    すでに 24 時超("24:50" 等)の表記はその曜日の深夜としてそのまま整形する。
    曜日か時刻が取れない場合はそのまま返す。
    """
    idx = WEEKDAY_ORDER.get(weekday)
    if idx is None or not time:
        return weekday, time
    h, m = time.split(":")
    h, m = int(h), int(m)
    if h < 5:                              # 0〜4 時台 = 前夜の深夜 → 前日へ +24時間表記
        idx = (idx - 1) % 7
        h += 24
    return WEEKDAYS[idx], f"{h:02d}:{m:02d}"


def sort_key(r):
    """月曜先頭・各曜日内は放送時刻の昇順で並べるためのソートキー。

    weekday/time は broadcast_night() で番組表式に正規化済みの前提。
    曜日・時刻が取れない作品は末尾へ送る。
    """
    idx = WEEKDAY_ORDER.get(r.get("weekday"), 99)
    mins = _minutes(r.get("time"))
    return (idx, mins if mins is not None else float("inf"))


# --- network の正規化(系列局はキー局へ集約) -------------------------------
# 配信JSONの network はここでキー局名へ寄せる。台帳の生 network は Wikipedia 由来で
# 週次 fetch に上書きされ続けるが、公開値は射影のたびに再導出するため安定する
# (手で台帳を直す運用と違い、定期実行で元へ戻らない)。

# 局名トークン(部分一致)→ キー局。系列局・制作会社表記・「系」揺れを吸収する。
# 「○○」製作委員会 毎日放送 のように委員会名の末尾へ局名が入る例も部分一致で拾える。
# 上から順に判定するため、同一局は先に具体的な表記を置く必要はない(全て同じ値へ寄る)。
_NETWORK_TOKENS = (
    # フジテレビ系(共同テレビはフジ系の制作会社で一意)
    ("フジテレビ", "フジテレビ"), ("関西テレビ", "フジテレビ"), ("カンテレ", "フジテレビ"),
    ("東海テレビ", "フジテレビ"), ("仙台放送", "フジテレビ"), ("共同テレビ", "フジテレビ"),
    # 日本テレビ系
    ("日本テレビ", "日本テレビ"), ("日テレ", "日本テレビ"), ("読売テレビ", "日本テレビ"),
    ("中京テレビ", "日本テレビ"), ("札幌テレビ", "日本テレビ"),
    # テレビ朝日系
    ("テレビ朝日", "テレビ朝日"), ("朝日放送", "テレビ朝日"), ("名古屋テレビ", "テレビ朝日"),
    # TBS系
    ("TBS", "TBS"), ("毎日放送", "TBS"), ("中部日本放送", "TBS"),
    # テレビ東京系
    ("テレビ東京", "テレビ東京"), ("テレビ大阪", "テレビ東京"), ("テレビ愛知", "テレビ東京"),
    ("テレビ北海道", "テレビ東京"), ("テレビせとうち", "テレビ東京"),
    # キー局を持たない局はそのまま
    ("NHK", "NHK総合"), ("WOWOW", "WOWOW"), ("TOKYO MX", "TOKYO MX"),
    ("BS11", "BS11"),
)

# 局名が取れないときの放送枠 → キー局。単一局に一意に紐づくブランド枠のみ載せる
# (フジ火9/テレ朝火9 のようにどの局か一意でない枠は載せない=生値のまま)。
_SLOT_NETWORK = {
    "月9": "フジテレビ", "木曜劇場": "フジテレビ",
    "ドラマ9": "テレビ東京", "ドラマ24": "テレビ東京", "ドラマ25": "テレビ東京",
    "木ドラ24": "テレビ東京", "ドラマ8": "テレビ東京",
    "ドラマ10": "NHK総合", "夜ドラ": "NHK総合", "よるドラ": "NHK総合",
    "特集ドラマ": "NHK総合", "プレミアムドラマ": "NHK総合",
    "連続ドラマW": "WOWOW",
    "ドラマストリーム": "テレビ朝日",
    "ドラマDiVE+": "日本テレビ", "ドラマDiVE": "日本テレビ",  # 読売テレビの深夜ドラマ枠
}


# catalogId → 配信用の局名(手動確定)。局名トークンも一意な slot も無く自動判定
# できない少数例だけをここで名指しで固定する。台帳フィールドではなくコード側に置く
# ことで、週次 fetch が台帳の生 network を上書きしても配信値は戻らない。
# 追加時は放送局を確認した根拠(公式サイト等)を持っておくこと。
_NETWORK_OVERRIDE = {
    "d_0050": "フジテレビ",   # スピナーベイト(関東ローカル): fujitv 系 / FOD 独占配信
    "d_0060": "テレビ東京",   # 一緒にごはんをたべるだけ: tv-tokyo.co.jp
    "d_0061": "テレビ東京",   # 夫婦と16歳〜狂気の隣人〜: tv-tokyo.co.jp
}


def canonical_network(network, slot=None, catalog_id=None):
    """配信用に network をキー局名へ正規化する。
    0) catalogId が手動確定マップにあればそれを最優先で使う。
    1) 局名トークンを含めばそのキー局へ寄せる(系列局・「系」揺れ・委員会名末尾も拾う)。
    2) 取れなければ放送枠(slot)から一意に決まる局を当てる。
    3) どちらも不可なら生値のまま返す(不明局・独立系の配信枠など)。
    """
    if catalog_id and catalog_id in _NETWORK_OVERRIDE:
        return _NETWORK_OVERRIDE[catalog_id]
    raw = (network or "").strip()
    for token, canonical in _NETWORK_TOKENS:
        if token in raw:
            return canonical
    if slot:
        hit = _SLOT_NETWORK.get(slot.strip())
        if hit:
            return hit
    return raw or None


# --- 配信JSONの生成(台帳 → 公開JSON) --------------------------------------

def to_public(record):
    """台帳レコードを配信JSONの1エントリ(PUBLIC_KEYS のみ)へ射影する。"""
    return {
        "id": record["id"],
        "title": record.get("title"),
        "network": canonical_network(record.get("network"), record.get("slot"),
                                     record.get("id")),
        "weekday": record.get("weekday"),
        "time": record.get("time"),
        "start": record.get("start"),
        "episodes": record.get("episodes"),
        "slot": record.get("slot"),
        "wikipedia": record.get("wikipediaTitle"),
        # 休止日は必ず週次グリッドへ整列した配列で出す(不明・休止なしは [])。
        # キー自体は常に出力する(消費側のパース単純化・前方互換のため)。
        "hiatus": align_hiatus(record.get("start"), record.get("hiatus")),
    }


def resolve_cool(record):
    """レコードの (year, cool)。未設定なら start から導出する。"""
    year, cool = record.get("year"), record.get("cool")
    if year is None or cool is None:
        return cool_of(record.get("start", ""))
    return year, cool


def group_by_cool(records):
    """レコードを (year, cool) ごとにまとめる。クール判定不能なものは除外。"""
    groups = {}
    for r in records:
        year, cool = resolve_cool(r)
        if cool is None:
            continue
        groups.setdefault((year, cool), []).append(r)
    return groups


def seed_rows(records):
    """あるクールのレコード群を配信JSONの行(番組表式ソート済み)にする。"""
    rows = [to_public(r) for r in records]
    rows.sort(key=sort_key)
    return rows


def write_seed(records, year, cool, seed_dir=SEED_DIR):
    """指定クールの配信JSONを public/seed に書き出し、書いた行を返す。"""
    rows = seed_rows(records)
    seed_dir.mkdir(parents=True, exist_ok=True)
    out = seed_dir / f"dramas_{year}_{cool}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return out, rows
