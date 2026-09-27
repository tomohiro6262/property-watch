"""収集した物件を条件で判定・採点し、結果を data/result_YYYYMMDD.json に出力する。

1. 一覧データで足切り（価格・間取り・築年数・面積・対象駅）
2. 最寄駅 → 日本橋 / 溜池山王 の通勤時間で足切り（両方 60 分以内）
3. 詳細ページ（物件概要）を取得し、権利・接道・駐車場などで足切り
4. 希望条件で採点
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import commute
import hazard

UA = commute.UA
ROOT = Path(__file__).parent
DATA = ROOT / "data"
DETAIL_DIR = DATA / "details"
SLEEP_SEC = 3
OFFICE_WALK = 5          # 勤務地駅 → 勤務先の徒歩（仮定）
COMMUTE_MAX = 60
COMMUTE_BONUS = 50
PRICE_MAX = 8000
PRICE_TARGET_MAX = 6500
LOW_LYING_WARDS = {"墨田区", "江東区", "足立区", "葛飾区", "江戸川区", "市川市"}


# ---------- 1. 一覧データの足切り ----------

def madori_ok(m):
    mm = re.match(r"(\d)", m or "")
    if not mm:
        return False
    n = int(mm.group(1))
    return n >= 4 or (n == 3 and "LDK" in m)


def age_months(built):
    y, m = map(int, built.split("-"))
    t = dt.date.today()
    return (t.year - y) * 12 + (t.month - m)


def list_filter(r):
    """(通過したか, 理由)"""
    if r.get("parse_error"):
        return False, "解析失敗"
    if r["price"] > PRICE_MAX:
        return False, "価格"
    if not madori_ok(r.get("madori")):
        return False, "間取り"
    if not r.get("built") or age_months(r["built"]) > 15 * 12:
        return False, "築年数"
    if r["type"] == "mansion" and (r.get("area") or 0) < 65:
        return False, "専有面積"
    if r["type"] == "kodate":
        if (r.get("land") or 0) < 50:
            return False, "土地面積"
        if (r.get("building") or 0) < 80:
            return False, "建物面積"
    return True, ""


def dedupe(records):
    seen = {}
    for r in records:
        if r["type"] == "mansion":
            key = (r["type"], r.get("address"), r.get("price"), r.get("area"), r.get("built"))
        else:
            key = (r["type"], r.get("address"), r.get("price"), r.get("land"), r.get("building"))
        if key in seen:
            seen[key].setdefault("dup_urls", []).append(r["url"])
        else:
            seen[key] = r
    return list(seen.values())


# ---------- 2. 通勤時間 ----------

def pref_of(addr):
    for p in ("東京都", "千葉県", "埼玉県", "神奈川県"):
        if addr.startswith(p):
            return p
    return "東京都"


def door_to_door(walk, st_times):
    out = {}
    for dest, v in st_times.items():
        out[dest] = None if not v else walk + v["minutes"] + (0 if v["minutes"] == 0 else OFFICE_WALK)
    return out


# ---------- 3. 詳細ページ ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
    time.sleep(SLEEP_SEC)
    return body


def clean(x):
    x = re.sub(r"<[^>]+>", " ", x)
    x = x.replace("&nbsp;", " ").replace("ヒント", "")
    return re.sub(r"\s+", " ", x).strip()


def get_overview(r):
    """物件概要の {項目: 値} を返す。

    読み取った結果（とハザード判定）を data/details/{id}.json に保存し、次回以降は再取得しない。
    HTML はリポジトリに溜めない。
    """
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    cache = DETAIL_DIR / f"{r['id']}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    legacy = DETAIL_DIR / f"{r['id']}.html"
    body = legacy.read_text(encoding="utf-8") if legacy.exists() else None
    if body is None:
        body = fetch(r["url"])
        m = re.search(r'href="(https://suumo\.jp/[^"]+/bukkengaiyo/)[^"]*"', body)
        if r["type"] == "mansion" and m:
            # マンションは管理費などが物件概要ページにしかない。地図の座標はメインページにある
            body = fetch(m.group(1)) + "\n<!-- main -->\n" + body
    elif r["type"] == "mansion" and "<!-- main -->" not in body:
        body += "\n<!-- main -->\n" + fetch(r["url"])  # 旧形式のキャッシュに座標を補う
    ov = parse_overview(body)
    if ov["_latlon"]:
        try:
            ov["_hazard"] = hazard.check(*ov["_latlon"])
        except Exception as e:
            ov["_hazard_error"] = str(e)
    cache.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
    if legacy.exists():
        legacy.unlink()
    return ov


def parse_overview(body):
    ov = {}
    ll = re.search(r"(3\d\.\d{5,})',\s*lng:'(1[34]\d\.\d{5,})", body)
    ov["_latlon"] = (float(ll.group(1)), float(ll.group(2))) if ll else None
    for k, v in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", body, re.S):
        k, v = clean(k), clean(v)
        if k and k not in ov:
            ov[k] = v
    # 判定用テキストは物件概要と担当者コメントに限る（関連物件欄などの他物件の文言を拾わないため）
    page = clean(re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.S))
    comment = re.search(r"【この物件について】(.*?)(?:お問い合せ先|【得意エリア】)", page)
    ov["_text"] = " ".join(v for k, v in ov.items()
                           if not k.startswith("_") and k not in ("会社概要", "問い合わせ先")) \
        + " " + (comment.group(1) if comment else "")
    return ov


def stations_from(ov):
    return [(line, st, int(w)) for line, st, w in
            re.findall(r"([^\s「」]+)「([^」]+)」歩(\d+)分", ov.get("交通", ""))]


def yen_to_man(s):
    """'1万8300円' → 1.83（万円）"""
    m = re.search(r"(?:(\d+)万)?(\d+)?円", s or "")
    if not m:
        return None
    return int(m.group(1) or 0) + int(m.group(2) or 0) / 10000


# ---------- 4. 判定・採点 ----------

def evaluate(r, ov):
    """(除外理由 or None, 点数, 加点減点のメモ, 要確認メモ)"""
    text = ov.get("_text", "")
    notes, checks = [], []
    right = ov.get("土地の権利形態") or ov.get("敷地の権利形態") or ""
    if re.search(r"借地|賃借権|地上権", right):
        return f"借地権（{right[:30]}）", 0, notes, checks
    if "再建築不可" in text:
        return "再建築不可", 0, notes, checks
    if re.search(r"告知事項", text):
        return "告知事項あり", 0, notes, checks
    if re.search(r"賃貸中|オーナーチェンジ", ov.get("引渡可能時期", "") + ov.get("現況", "")
                 + ov.get("その他概要・特記事項", "")):
        return "賃貸中", 0, notes, checks
    if re.search(r"43条", text):
        return "43条許可が必要な道路", 0, notes, checks
    if re.search(r"土砂災害警戒区域", text):
        return "土砂災害警戒区域", 0, notes, checks

    score = 50
    p = r["price"]
    if p <= PRICE_TARGET_MAX:
        score += 20; notes.append("予算内(+20)")
    elif p <= 7000:
        score += 10; notes.append("予算やや超(+10)")
    elif p <= 7500:
        score += 5; notes.append("予算超(+5)")

    c = r["commute"]
    under = [k for k, v in c.items() if v is not None and v <= COMMUTE_BONUS]
    if len(under) == 2:
        score += 15; notes.append("通勤 両方50分以内(+15)")
    elif len(under) == 1:
        score += 7; notes.append(f"通勤 {under[0]}のみ50分以内(+7)")

    if r["type"] == "kodate":
        road = ov.get("私道負担・道路", "")
        widths = [float(x) for x in re.findall(r"([\d.]+)[ｍm]幅", road)]
        if widths and max(widths) < 4:
            return f"前面道路が4m未満（{road[:40]}）", 0, notes, checks
        # 「無、南4ｍ幅」以外（面積や持分の記載がある）は私道負担あり
        if road and not road.startswith("無") and re.search(r"有|持分|m 2|㎡|私道", road):
            score -= 10; notes.append("私道負担あり(-10)")
            checks.append("私道の通行・掘削承諾")
        frontage = [float(x) for x in re.findall(r"接道幅([\d.]+)", road)]
        if frontage and max(frontage) < 3:
            score -= 5; notes.append("接道幅3m未満・旗竿地の可能性(-5)")
        if "旗竿" in text or "路地状" in text:
            score -= 5; notes.append("旗竿地(-5)")
        struct = ov.get("構造・工法", "")
        if "2階建" in struct:
            score += 10; notes.append("2階建て(+10)")
        elif not struct:
            checks.append("階数不明")
        parking_text = ov.get("その他概要・特記事項", "") + ov.get("駐車場", "")
        if re.search(r"駐車場[：:]\s*(無|なし)", parking_text):
            return "駐車スペースなし", 0, notes, checks
        if not re.search(r"駐車|車庫|カーポート|ガレージ", text):
            score -= 10; notes.append("駐車スペースの記載なし(-10)")
            checks.append("駐車スペースの有無")
        m = re.search(r"擁壁[^。、]{0,20}?([\d.]+)\s*[ｍm]", text)
        if m and float(m.group(1)) >= 2:
            score -= 10; notes.append(f"擁壁{m.group(1)}m(-10)")
        elif "擁壁" in text:
            score -= 5; notes.append("擁壁あり(-5)"); checks.append("擁壁の高さ")
        if "セットバック" in road + ov.get("その他制限事項", "") and "済" not in ov.get("その他制限事項", ""):
            checks.append("セットバック要否")
    else:
        fee = (yen_to_man(ov.get("管理費")) or 0) + (yen_to_man(ov.get("修繕積立金")) or 0)
        r["monthly_fee"] = round(fee, 2) if fee else None
        if fee > 4:
            score -= 10; notes.append(f"管理費+修繕積立金 月{fee:.1f}万円(-10)")
        elif not fee:
            checks.append("管理費・修繕積立金")
        floor = ov.get("所在階", "")
        r["floor"] = floor
        if re.match(r"^1階", floor):
            score -= 5; notes.append("1階(-5)")
        direction = ov.get("向き", "")
        r["direction"] = direction
        if direction.startswith("北"):
            score -= 5; notes.append(f"{direction}向き(-5)")
        units = re.search(r"(\d+)戸", ov.get("総戸数", ""))
        if units and int(units.group(1)) < 20:
            score -= 5; notes.append(f"総戸数{units.group(1)}戸(-5)")
        elif not units:
            checks.append("総戸数")
    if r["walk"] <= 5:
        score += 5; notes.append(f"駅徒歩{r['walk']}分(+5)")
    latlon = ov.get("_latlon")
    if latlon:
        hz = ov.get("_hazard")
        if hz is None:  # 前回失敗していれば再判定する
            try:
                hz = hazard.check(*latlon)
            except Exception as e:
                checks.append(f"ハザード判定失敗（{e}）")
        if hz:
            r["hazard"] = hz
            if hz["土砂"]:
                return "土砂災害警戒区域", 0, notes, checks
            # 洪水・高潮のうち深い方で採点する。地点に値がない（川面など色のない場所に当たった）
            # ときは、位置が概略であることを踏まえ周辺 100m 内の最大値を使う
            def depth_of(h):
                if h["point_m"] is not None:
                    return h["point_m"], h["point"]
                if h["max_m"] is not None:
                    return h["max_m"], f"{h['max']}（周辺）"
                return None, None
            name, h = max(((k, hz[k]) for k in ("洪水", "高潮")),
                          key=lambda kv: -1 if depth_of(kv[1])[0] is None else depth_of(kv[1])[0])
            depth, label = depth_of(h)
            fl = re.match(r"(\d+)階", r.get("floor") or "")
            upper = r["type"] == "mansion" and fl and int(fl.group(1)) >= 3
            if depth is None:
                notes.append("浸水想定なし")
            elif depth >= 3:
                pt = 5 if upper else 15
                score -= pt; notes.append(f"浸水想定{label}（{name}）(-{pt})")
            elif depth >= 0.5 and not upper:
                score -= 5; notes.append(f"浸水想定{label}（{name}）(-5)")
            else:
                notes.append(f"浸水想定{label}（{name}）")
            near = max((hz[k]["max_m"] or 0) for k in ("洪水", "高潮"))
            if depth is not None and near > depth:
                checks.append("周辺100m内に、より深い浸水想定あり")
    else:
        city = re.match(r"^(東京都|千葉県|埼玉県|神奈川県)(.+?[区市])", r["address"])
        if city and city.group(2) in LOW_LYING_WARDS:
            score -= 5; notes.append("低地エリア(-5)")
        checks.append("ハザードマップ（座標が取れず未判定）")
    return None, score, notes, checks


def property_key(r):
    """不動産会社が違っても同じ物件なら同じになるキー。"""
    if r["type"] == "mansion":
        return (r["type"], r["address"], r["built"], r.get("floor"), round(r.get("area") or 0))
    return (r["type"], r["address"], r["built"], round(r.get("building") or 0))


def merge_same_property(results):
    """不動産会社違いの同一物件（価格や面積表記が少し違う）を 1 件にまとめる。

    results は点数の高い順に並んでいる前提で、先頭（高得点・安い方）を残す。
    """
    kept, index = [], {}
    for r in results:
        key = property_key(r)
        if key in index:
            base = index[key]
            base.setdefault("dup_urls", []).append(r["url"])
            if r["price"] != base["price"]:
                base.setdefault("other_prices", []).append(r["price"])
            continue
        index[key] = r
        kept.append(r)
    return kept


def main():
    today = dt.date.today().strftime("%Y%m%d")
    src = sorted(DATA.glob("suumo_*.json"))[-1]
    raw = json.loads(src.read_text(encoding="utf-8"))["records"]
    stats = {"収集": len(raw)}
    recs = dedupe(raw)
    stats["重複除去後"] = len(recs)

    passed, reasons = [], {}
    for r in recs:
        ok, why = list_filter(r)
        if ok:
            passed.append(r)
        else:
            reasons[why] = reasons.get(why, 0) + 1
    stats["一覧で除外"] = reasons
    stats["一覧通過"] = len(passed)

    cache = commute.load_cache()
    stage2 = []
    for r in passed:
        t = commute.station_times(r["station"], pref_of(r["address"]), cache)
        r["commute"] = door_to_door(r["walk"], t)
        r["commute_station"] = r["station"]
        ok = all(v is not None and v <= COMMUTE_MAX for v in r["commute"].values())
        if ok or r["walk"] > 10 or r.get("station_filter"):
            stage2.append(r)  # 徒歩 10 分超・対象駅限定は詳細ページの駅一覧で再判定
        else:
            reasons_c = stats.setdefault("通勤で除外", 0)
            stats["通勤で除外"] = reasons_c + 1
    stats["通勤通過"] = len(stage2)

    results, excluded = [], []
    for i, r in enumerate(stage2):
        print(f"[{i + 1}/{len(stage2)}] {r['title']}", flush=True)
        try:
            ov = get_overview(r)
        except Exception as e:
            r["error"] = str(e)
            excluded.append((r, f"詳細取得失敗: {e}"))
            continue
        # 徒歩 10 分以内の駅のうち通勤時間が最短の駅で判定し直す
        best = None
        for line, st, w in stations_from(ov):
            if w > 10:
                continue
            if r.get("station_filter") and st not in r["station_filter"]:
                continue
            t = door_to_door(w, commute.station_times(st, pref_of(r["address"]), cache))
            if any(v is None for v in t.values()):
                continue
            worst = max(t.values())
            if best is None or worst < best[0]:
                best = (worst, st, w, t, line)
        if best is None:
            excluded.append((r, "徒歩10分以内の対象駅なし"))
            continue
        _, r["commute_station"], r["walk"], r["commute"], r["line"] = best
        if max(r["commute"].values()) > COMMUTE_MAX:
            excluded.append((r, f"通勤{max(r['commute'].values())}分"))
            continue
        why, score, notes, checks = evaluate(r, ov)
        if why:
            excluded.append((r, why))
            continue
        r.update(score=score, notes=notes, checks=checks,
                 overview={k: v for k, v in ov.items() if not k.startswith("_")})
        results.append(r)

    ex_reasons = {}
    for _, why in excluded:
        k = "通勤60分超" if why.startswith("通勤") else re.sub(r"（.*", "", why)
        ex_reasons[k] = ex_reasons.get(k, 0) + 1
    stats["詳細で除外"] = ex_reasons
    results.sort(key=lambda r: (-r["score"], r["price"]))
    results = merge_same_property(results)
    stats["最終候補"] = len(results)
    out = DATA / f"result_{today}.json"
    out.write_text(json.dumps({"stats": stats, "results": results,
                               "excluded": [{"title": r["title"], "url": r["url"], "why": w,
                                             "price": r["price"]} for r, w in excluded]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    print(f"saved -> {out}")


if __name__ == "__main__":
    sys.exit(main())
