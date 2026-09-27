"""国土地理院「重ねるハザードマップ」のタイル画像から、地点の浸水深などを判定する。

- 洪水（想定最大規模）と高潮（想定最大規模）の浸水深、土砂災害警戒区域を調べる
- 物件の緯度経度は SUUMO 詳細ページの地図から取る（丁目レベルの位置のことがある）
- 位置の誤差を考え、地点の値に加えて半径約 100m 内の最大値も返す
"""
import math
import struct
import time
import urllib.error
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
ZOOM = 17
BASE = "https://disaportal.gsi.go.jp/data/raster/{layer}/{z}/{x}/{y}.png"
LAYERS = {
    "洪水": "01_flood_l2_shinsuishin_data",
    "高潮": "03_hightide_l2_shinsuishin_data",
}
DOSHA_LAYERS = ["05_dosekiryukeikaikuiki", "05_kyukeishakeikaikuiki", "05_jisuberikeikaikuiki"]

# 浸水深の凡例（色 → (区分, 深さの下限 m)）
DEPTH_COLORS = {
    (247, 245, 169): ("0.5m未満", 0.0),
    (255, 216, 192): ("0.5〜3m", 0.5),
    (255, 183, 183): ("3〜5m", 3.0),
    (255, 145, 145): ("5〜10m", 5.0),
    (242, 133, 201): ("10〜20m", 10.0),
    (220, 122, 220): ("20m以上", 20.0),
}

_tile_cache = {}


def decode_png(data):
    """8bit の RGBA / RGB / パレット PNG を [[(r,g,b,a), ...], ...] に展開する。"""
    pos, chunks = 8, {}
    idat = b""
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IDAT":
            idat += body
        else:
            chunks[ctype] = body
        pos += 12 + length
    w, h, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    if depth != 8:
        raise ValueError(f"unsupported bit depth {depth}")
    bpp = {6: 4, 2: 3, 3: 1, 4: 2, 0: 1}[color]
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev = [], bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 0xFF
            elif f == 2:
                line[x] = (line[x] + b) & 0xFF
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        rows.append(bytes(line))
        prev = line
    palette = chunks.get(b"PLTE")
    trns = chunks.get(b"tRNS", b"")
    out = []
    for row in rows:
        px = []
        for x in range(w):
            if color == 6:
                px.append(tuple(row[x * 4:x * 4 + 4]))
            elif color == 2:
                px.append(tuple(row[x * 3:x * 3 + 3]) + (255,))
            elif color == 3:
                k = row[x]
                alpha = trns[k] if k < len(trns) else 255
                px.append(tuple(palette[k * 3:k * 3 + 3]) + (alpha,))
            elif color == 4:
                px.append((row[x * 2],) * 3 + (row[x * 2 + 1],))
            else:
                px.append((row[x],) * 3 + (255,))
        out.append(px)
    return out


def _tile(layer, x, y):
    key = (layer, x, y)
    if key in _tile_cache:
        return _tile_cache[key]
    url = BASE.format(layer=layer, z=ZOOM, x=x, y=y)
    img = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                img = decode_png(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:  # タイルなし＝その範囲に該当区域なし
                break
            time.sleep(2)
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError(f"tile fetch failed: {url}")
    _tile_cache[key] = img
    time.sleep(0.3)
    return img


def _pixel_pos(lat, lon):
    n = 2 ** ZOOM
    fx = (lon + 180) / 360 * n
    lat_r = math.radians(lat)
    fy = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
    return fx * 256, fy * 256  # 全体ピクセル座標


def _sample(layer, lat, lon, radius_m):
    """地点のピクセルと、半径 radius_m 内のピクセルを返す（アルファ 0 は除く）。"""
    px, py = _pixel_pos(lat, lon)
    m_per_px = 156543.03392 * math.cos(math.radians(lat)) / (2 ** ZOOM)
    r = int(radius_m / m_per_px)
    center, around = None, []
    for dy in range(-r, r + 1, 4):
        for dx in range(-r, r + 1, 4):
            if dx * dx + dy * dy > r * r:
                continue
            gx, gy = int(px) + dx, int(py) + dy
            img = _tile(layer, gx // 256, gy // 256)
            if img is None:
                continue
            p = img[gy % 256][gx % 256]
            if p[3] == 0:
                continue
            around.append(p[:3])
    img = _tile(layer, int(px) // 256, int(py) // 256)
    if img is not None:
        p = img[int(py) % 256][int(px) % 256]
        if p[3] != 0:
            center = p[:3]
    return center, around


def _depth(rgb):
    if rgb is None:
        return None
    best = min(DEPTH_COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, rgb)))
    if sum((a - b) ** 2 for a, b in zip(best, rgb)) > 300:
        return ("不明色", 0.0)
    return DEPTH_COLORS[best]


def check(lat, lon, radius_m=100):
    """{'洪水': {'point': 区分 or None, 'max': 区分 or None, 'max_m': 下限m}, ..., '土砂': bool}"""
    out = {}
    for name, layer in LAYERS.items():
        center, around = _sample(layer, lat, lon, radius_m)
        c = _depth(center)
        depths = [_depth(p) for p in around]
        mx = max(depths, key=lambda d: d[1]) if depths else None
        out[name] = {"point": c[0] if c else None, "point_m": c[1] if c else None,
                     "max": mx[0] if mx else None, "max_m": mx[1] if mx else None}
    dosha = False
    for layer in DOSHA_LAYERS:
        center, _ = _sample(layer, lat, lon, 0)
        if center is not None:
            dosha = True
    out["土砂"] = dosha
    return out
