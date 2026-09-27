"""毎日の物件アラート処理をまとめて実行する。

収集 → 判定 → 配信メール作成 → 古いデータの削除。
メールの送信はこのスクリプトではなく、実行した Claude が Gmail で行う。
"""
import json
import sys
from pathlib import Path

import collect_suumo
import make_digest
import screen

DATA = Path(__file__).parent / "data"
KEEP_DAYS = 3  # suumo_*.json / result_*.json / digest_* を残す日数（差分には前回分があれば足りる）


def prune():
    for pattern in ("suumo_*.json", "result_*.json", "digest_*.html", "digest_*.txt"):
        files = sorted(DATA.glob(pattern))
        for f in files[:-KEEP_DAYS]:
            f.unlink()
    # 掲載が終わった物件の詳細キャッシュを削除する
    latest = sorted(DATA.glob("suumo_*.json"))[-1]
    alive = {r["id"] for r in json.loads(latest.read_text(encoding="utf-8"))["records"]}
    for f in (DATA / "details").glob("*"):
        if f.stem not in alive:
            f.unlink()


def main():
    collect_suumo.main()
    screen.main()
    make_digest.main()
    prune()


if __name__ == "__main__":
    sys.exit(main())
