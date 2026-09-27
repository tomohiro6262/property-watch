"""判定結果（data/result_YYYYMMDD.json）から配信メールの HTML を作る。

出力: data/digest_YYYYMMDD.html
"""
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

from screen import property_key

DATA = Path(__file__).parent / "data"
TOP_MAIN = 10     # 本命枠（6,500 万円以下）の表示件数
TOP_OVER = 5      # 別枠（6,500 万円超〜8,000 万円）の表示件数
TEST = "--test" in sys.argv

CSS = """
body{font-family:'Hiragino Sans','Yu Gothic',Meiryo,sans-serif;color:#1f2328;background:#f6f7f9;margin:0;padding:16px}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:18px;margin:0 0 4px}
h2{font-size:15px;margin:24px 0 8px;padding-left:8px;border-left:4px solid #2f6feb}
.sub{color:#59636e;font-size:12px;margin:0 0 12px}
.card{background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:12px 14px;margin:0 0 10px}
.name{font-weight:700;font-size:15px;margin:0 0 2px}
.name a{color:#0b57d0;text-decoration:none}
.price{font-size:16px;font-weight:700}
.score{float:right;background:#eef4ff;color:#1b4fb8;border-radius:12px;padding:2px 10px;font-size:12px;font-weight:700}
.meta{font-size:13px;color:#3d444d;margin:4px 0}
.plus{font-size:12px;color:#1a7f37;margin:4px 0 0}
.chk{font-size:12px;color:#9a6700;margin:2px 0 0}
.note{font-size:12px;color:#59636e;line-height:1.6}
.badge{display:inline-block;background:#d1242f;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;margin-right:6px;vertical-align:2px}
.stats{font-size:13px;background:#fff;border:1px solid #d8dee4;border-radius:8px;padding:10px 14px}
"""


def esc(x):
    return html.escape(str(x))


def card(r):
    kind = "中古マンション" if r["type"] == "mansion" else "中古戸建"
    if r["type"] == "mansion":
        size = f'{r["madori"]}・{r["area"]}㎡'
        extra = []
        if r.get("floor"):
            extra.append(r["floor"])
        if r.get("direction"):
            extra.append(f'{r["direction"]}向き')
        if r.get("monthly_fee"):
            extra.append(f'管理費等 月{r["monthly_fee"]:.1f}万円')
    else:
        size = f'{r["madori"]}・土地{r["land"]}㎡・建物{r["building"]}㎡'
        ov = r.get("overview", {})
        extra = [x for x in (ov.get("構造・工法"), ov.get("私道負担・道路", "")[:40]) if x]
    c = r["commute"]
    commute = " / ".join(f"{k} {v}分" for k, v in c.items())
    y = r["built"].split("-")[0]
    plus = "・".join(r.get("notes", []))
    checks = "・".join(r.get("checks", []))
    if r.get("other_prices"):
        extra.append("他社掲載価格 " + "・".join(f"{p:,}万円" for p in sorted(set(r["other_prices"]))))
    badge = ""
    if r.get("status") == "new":
        badge = '<span class="badge">新着</span>'
    elif r.get("status") == "down":
        badge = f'<span class="badge">値下げ {r["prev_price"]:,}→{r["price"]:,}万円</span>'
    return f"""
<div class="card">
  <span class="score">{r['score']}点</span>
  <p class="name">{badge}<a href="{esc(r['url'])}">{esc(r['title'])}</a></p>
  <div class="meta">{kind}｜<span class="price">{r['price']:,}万円</span>｜{esc(size)}｜{y}年築</div>
  <div class="meta">{esc(r['address'])}｜{esc(r.get('line') or '')}「{esc(r['commute_station'])}」徒歩{r['walk']}分</div>
  <div class="meta">通勤（ドアtoドア）: {esc(commute)}</div>
  {f'<div class="meta">{esc("｜".join(extra))}</div>' if extra else ''}
  {f'<div class="plus">加点・減点: {esc(plus)}</div>' if plus else ''}
  {f'<div class="chk">要確認: {esc(checks)}</div>' if checks else ''}
</div>"""


def mark_changes(res, prev):
    """前回の結果と比べて、新着（status=new）と値下げ（status=down）に印を付ける。"""
    prev_price = {}
    for r in prev:
        for key in [tuple(property_key(r))] + [("id", u) for u in [r["url"]] + r.get("dup_urls", [])]:
            prev_price[key] = min(prev_price.get(key, r["price"]), r["price"])
    for r in res:
        keys = [tuple(property_key(r))] + [("id", u) for u in [r["url"]] + r.get("dup_urls", [])]
        known = [prev_price[k] for k in keys if k in prev_price]
        if not known:
            r["status"] = "new"
        elif r["price"] < min(known):
            r["status"], r["prev_price"] = "down", min(known)


def to_text(html_body):
    t = re.sub(r"<style.*?</style>", "", html_body, flags=re.S)
    t = re.sub(r"<br>|</div>|</p>|</h2>|</h1>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def main():
    files = sorted(DATA.glob("result_*.json"))
    src = files[-1]
    d = json.loads(src.read_text(encoding="utf-8"))
    res, st = d["results"], d["stats"]
    prev_date = None
    if len(files) >= 2 and not TEST:
        prev = json.loads(files[-2].read_text(encoding="utf-8"))["results"]
        mark_changes(res, prev)
        prev_date = files[-2].stem.split("_")[1]
    changed = [r for r in res if r.get("status")]
    main_list = [r for r in res if r["price"] <= 6500][:TOP_MAIN]
    over_list = [r for r in res if r["price"] > 6500][:TOP_OVER]
    n_main = sum(1 for r in res if r["price"] <= 6500)
    n_over = len(res) - n_main
    n_new = sum(1 for r in changed if r["status"] == "new")
    n_down = len(changed) - n_new
    today = dt.date.today()
    if TEST:
        title = f"【テスト】物件アラート {today:%Y/%m/%d}：候補{len(res)}件"
    else:
        title = f"物件アラート {today:%Y/%m/%d}：新着{n_new}件・値下げ{n_down}件（候補{len(res)}件）"
    since = (f"{prev_date[4:6]}/{prev_date[6:]}" if prev_date else "")
    changes_html = (f"<h2>{since}以降の新着・値下げ {len(changed)}件</h2>"
                    + ("".join(card(r) for r in changed) or '<p class="note">該当なし</p>')
                    if prev_date else "")
    ex = st.get("詳細で除外", {})
    ex_txt = "、".join(f"{k}{v}件" for k, v in sorted(ex.items(), key=lambda x: -x[1]))
    body = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{esc(title)}</h1>
<p class="sub">{'初回テストのため、新着だけでなく現在掲載中の全物件を対象にしています。' if TEST else '前回配信以降の新着・値下げ物件と、現在の候補ランキングです。'}</p>
<div class="stats">
SUUMOで条件に近い物件 {st['収集']}件（重複除去後 {st['重複除去後']}件）<br>
→ 価格・間取り・築年数・面積で絞り込み {st['一覧通過']}件<br>
→ 通勤60分以内・詳細条件を満たすもの <b>{len(res)}件</b>（6,500万円以下 {n_main}件／6,500万円超 {n_over}件）<br>
<span class="note">詳細ページで除外：{esc(ex_txt) or 'なし'}</span>
</div>
{changes_html}
<h2>本命枠（6,500万円以下）上位{len(main_list)}件</h2>
{''.join(card(r) for r in main_list) or '<p class="note">該当なし</p>'}
<h2>別枠（6,500万円超〜8,000万円）上位{len(over_list)}件</h2>
{''.join(card(r) for r in over_list) or '<p class="note">該当なし</p>'}
<h2>判定の前提</h2>
<p class="note">
・通勤時間 ＝ 物件から駅までの徒歩 ＋ 平日8:50着の最短経路（Yahoo!路線情報）＋ 勤務地の駅から徒歩5分（仮定）<br>
・点数は50点を基準に、予算内 +20、通勤が両方50分以内 +15、2階建て +10、駅徒歩5分以内 +5 などを加点し、私道・擁壁・1階・北向き・管理費等の高さ・浸水想定などを減点しています。<br>
・借地権、再建築不可、告知事項あり、賃貸中、前面道路4m未満、駐車場なし（戸建）、土砂災害警戒区域は除外しています。<br>
・浸水想定は国土地理院「重ねるハザードマップ」の洪水・高潮（想定最大規模）の浸水深を、SUUMOの地図位置で判定しています。地図位置は丁目単位の概略のことがあるため、購入前には正確な住所でご確認ください（disaportal.gsi.go.jp）。<br>
・情報源：SUUMO（LIFULL HOME'Sは自動取得が遮断されるため対象外）
</p>
</div></body></html>"""
    out = DATA / f"digest_{today:%Y%m%d}.html"
    out.write_text(body, encoding="utf-8")
    out.with_suffix(".txt").write_text(to_text(body), encoding="utf-8")
    print(title)
    print(f"saved -> {out}")


if __name__ == "__main__":
    sys.exit(main())
