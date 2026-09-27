"""SUUMO の検索条件 RSS から中古マンション・中古戸建を収集する。

- 検索条件ごとの RSS（SUUMO が購読用に提供しているもの）を使う
- RSS は 1 フィード最大 30 件なので、30 件に達したら価格帯を分割して取り直す
- 出力: data/suumo_YYYYMMDD.json
"""
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
BASE = "https://suumo.jp/jj/bukken/ichiran/JJ012FC001/"
RSS_CAP = 30
SLEEP_SEC = 3
PRICE_STEPS = [1, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000,
               5500, 6000, 6500, 7000, 7500, 8000, 9000]

# (地域名, 都道府県コード ta, 市区町村コード sc, 対象駅の限定 or None)
AREAS = [(f"tokyo-{sc}", 13, sc, None) for sc in range(13101, 13124)] + [
    ("mitaka", 13, 13204, None),
    ("tachikawa", 13, 13202, None),
    ("ichikawa", 12, 12203, ["妙典"]),
    ("wako", 11, 11229, ["和光市"]),
    ("takatsu", 14, 14134, ["溝の口", "武蔵溝ノ口"]),
]

# bs=011 中古マンション / bs=021 中古一戸建て。
# 価格上限 kt は「未満」なので 9000 で取得し、8000 万円以下は後段で判定する。
TYPES = {
    "mansion": {"bs": "011", "md": ["3", "4", "5"], "mb": "60", "mt": "9999999",
                "et": "10", "cn": "15"},
    "kodate": {"bs": "021", "md": ["3", "4", "5"], "tb": "0", "tt": "9999999",
               "hb": "80", "ht": "9999999", "et": "10", "cn": "15"},
}

LINE_SUFFIX = re.compile(
    r"^(?P<line>.*?(?:線|ライン|ライナー|モノレール|ゆりかもめ|エクスプレス)(?:快速|各停)?）?)"
    r"(?P<station>.+)$")


def build_url(ptype, ta, sc, kb, kt):
    p = TYPES[ptype]
    q = [("ar", "030"), ("bs", p["bs"]), ("ta", str(ta)), ("sc", str(sc)),
         ("kb", str(kb)), ("kt", str(kt))]
    for k, v in p.items():
        if k == "bs":
            continue
        if isinstance(v, list):
            q += [(k, x) for x in v]
        else:
            q.append((k, v))
    q += [("po", "1"), ("pj", "2"), ("rssFlg", "1")]
    return BASE + "?" + urllib.parse.urlencode(q)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_station(line_station):
    m = LINE_SUFFIX.match(line_station)
    if not m:
        return None, line_station
    return m.group("line"), m.group("station")


def parse_item(ptype, title, desc, link):
    d = html.unescape(desc).replace("m&sup2;", "m²").replace("m<sup>2</sup>", "m²")
    d = re.sub(r"\s+", "", d)
    rec = {"type": ptype, "source": "suumo", "title": re.sub(r"^物件名：", "", title.strip()),
           "url": link, "raw": d}
    rec["id"] = "suumo-" + (re.search(r"nc=(\d+)", link) or re.search(r"(\d+)", link)).group(1)
    m = re.match(r"^(?P<addr>.+?)(?P<price>\d{4})万円(?P<ls>.+?)徒歩(?P<walk>\d+)分(?P<rest>.*)$", d)
    if not m:
        rec["parse_error"] = True
        return rec
    rec["address"] = m.group("addr")
    rec["price"] = int(m.group("price"))
    rec["line"], rec["station"] = parse_station(m.group("ls"))
    rec["walk"] = int(m.group("walk"))
    rest = m.group("rest")
    areas = [float(x) for x in re.findall(r"([\d.]+)m²", rest)]
    if ptype == "mansion":
        rec["area"] = areas[0] if areas else None
        rec["balcony"] = areas[1] if len(areas) > 1 else None
    else:
        rec["land"] = areas[0] if areas else None
        rec["building"] = areas[1] if len(areas) > 1 else None
        rec["land_note"] = "実測" if "（実測）" in rest else ("公簿" if "（公簿）" in rest else "")
    mm = re.search(r"(\d(?:S?L?D?K|R)(?:\+\d?S(?:（納戸）)?)?)(\d{4})年(\d{1,2})月$", rest)
    if mm:
        rec["madori"] = mm.group(1)
        rec["built"] = f"{mm.group(2)}-{int(mm.group(3)):02d}"
    return rec


def fetch_feed(ptype, ta, sc, kb, kt, log):
    url = build_url(ptype, ta, sc, kb, kt)
    body = fetch(url)
    time.sleep(SLEEP_SEC)
    items = re.findall(r"<item>(.*?)</item>", body, re.S)
    log.append(f"{ptype} sc={sc} {kb}-{kt}: {len(items)}")
    if len(items) >= RSS_CAP:
        # 上限に達した → 価格帯を二分して取り直す
        lo, hi = PRICE_STEPS.index(kb), PRICE_STEPS.index(kt)
        if hi - lo >= 2:
            mid = PRICE_STEPS[(lo + hi) // 2]
            return (fetch_feed(ptype, ta, sc, kb, mid, log)
                    + fetch_feed(ptype, ta, sc, mid, kt, log))
        log.append(f"  WARNING: cap reached and cannot split further ({sc} {kb}-{kt})")
    out = []
    for it in items:
        t = re.search(r"<title>(.*?)</title>", it, re.S).group(1)
        de = re.search(r"<description>(.*?)</description>", it, re.S).group(1)
        ln = html.unescape(re.search(r"<link>(.*?)</link>", it, re.S).group(1)).strip()
        out.append(parse_item(ptype, re.sub(r"\s+", " ", t), de, ln))
    return out


def main():
    outdir = Path(__file__).parent / "data"
    outdir.mkdir(exist_ok=True)
    log, records = [], []
    for ptype in TYPES:
        for name, ta, sc, stations in AREAS:
            try:
                recs = fetch_feed(ptype, ta, sc, 1, 9000, log)
            except Exception as e:  # 1 フィードの失敗で全体を止めない
                log.append(f"ERROR {ptype} {name}: {e}")
                continue
            for r in recs:
                r["area_key"] = name
                r["station_filter"] = stations
            records += recs
            print(log[-1], flush=True)
    today = dt.date.today().strftime("%Y%m%d")
    out = outdir / f"suumo_{today}.json"
    out.write_text(json.dumps({"fetched_at": dt.datetime.now().isoformat(), "log": log,
                               "records": records}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"saved {len(records)} records -> {out}")


if __name__ == "__main__":
    sys.exit(main())
