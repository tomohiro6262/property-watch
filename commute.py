"""最寄駅 → 勤務地駅の平日朝の所要時間を Yahoo!路線情報で調べ、キャッシュする。

- 平日（木曜）8:50 着の到着時刻指定で検索し、最短ルートの所要時間と乗換回数を使う
- 結果は data/station_times.json に保存し、次回以降は再検索しない
"""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
DESTS = {"日本橋": "日本橋(東京都)", "溜池山王": "溜池山王"}
ARRIVAL = ("2026", "09", "24", "08", "5", "0")  # 平日 08:50 着
SLEEP_SEC = 2
CACHE = Path(__file__).parent / "data" / "station_times.json"
PREF_SUFFIX = {"東京都": "(東京都)", "千葉県": "(千葉県)", "埼玉県": "(埼玉県)", "神奈川県": "(神奈川県)"}


def _query(frm, to):
    y, m, d, hh, m1, m2 = ARRIVAL
    q = {"from": frm, "to": to, "y": y, "m": m, "d": d, "hh": hh, "m1": m1, "m2": m2,
         "type": "4", "s": "0", "ws": "3", "expkind": "1", "ticket": "ic"}
    url = "https://transit.yahoo.co.jp/search/result?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
    time.sleep(SLEEP_SEC)
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    nsp = data.get("props", {}).get("pageProps", {}).get("naviSearchParam") or {}
    routes = nsp.get("featureInfoList") or []
    best = None
    for rt in routes:
        s = rt.get("summaryInfo") or {}
        mins = _to_min(s.get("totalTime", ""))
        if mins is None or mins > 180:  # 翌日便などの異常値は使わない
            continue
        if best is None or mins < best["minutes"]:
            best = {"minutes": mins, "transfers": int(s.get("transferCount") or 0),
                    "dep": s.get("departureTime"), "arr": s.get("arrivalTime")}
    return best


def _to_min(t):
    h = re.search(r"(\d+)時間", t)
    m = re.search(r"(\d+)分", t)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def station_times(station, pref, cache):
    """駅名と都県から、各勤務地駅までの所要時間 dict を返す（キャッシュ優先）。"""
    key = f"{station}|{pref}"
    if key in cache:
        return cache[key]
    result = {}
    for label, dest in DESTS.items():
        if station == label:
            result[label] = {"minutes": 0, "transfers": 0}
            continue
        # 同名駅の取り違え（例: 東京の「高松」→ 香川の高松）を防ぐため都県名付きで先に検索し、
        # 都県名付きでは見つからない駅（例: 溝の口）は駅名だけで再検索する
        r = _query(station + PREF_SUFFIX.get(pref, ""), dest)
        if r is None:
            r = _query(station, dest)
        result[label] = r
    cache[key] = result
    save_cache(cache)
    return result
