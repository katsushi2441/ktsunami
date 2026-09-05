#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国土数値情報 津波浸水想定(A40)を SQLite に取り込む。

なぜ PostGIS を使わないか（2026-09-06 実測）:
  A40のポリゴンは約10m四方の**軸平行な矩形**グリッドで、愛知県だけで129,834件。
  矩形なので R-tree の矩形判定がそのまま厳密な内外判定になる。
  ポリゴン演算が要らないので、SQLite だけで足りる。
  買い切り版に Docker と PostgreSQL を要求せずに済む（krefuge と同じ方針）。

khazard から引き継ぐ原則:
  データ時点が分からないデータでは判定しない。
  時点は都道府県ごとに違うので、県単位で記録する。

出典: 国土数値情報「津波浸水想定データ」（国土交通省）
      ＜オープンデータとして利用可（商用利用可・再配信可）＞
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
DB = os.path.join(DATA, "ktsunami.db")
BASE = "https://nlftp.mlit.go.jp/ksj/gml/data/A40"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ktsunami/1.0)"}
SOURCE_URL = "https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-A40.html"
ATTRIBUTION = "国土数値情報（津波浸水想定データ）国土交通省 を加工して作成"
LICENSE = "オープンデータ（商用利用可・再配信可）"

# 津波浸水想定は沿岸のある都道府県だけ。内陸県には配布が無い（404）。
COASTAL = [1] + list(range(2, 16)) + [17, 18, 22, 23, 24, 26, 27, 28, 30, 31, 32,
                                      33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,
                                      44, 45, 46, 47]

SCHEMA = """
CREATE TABLE datasets (
  pref_code TEXT PRIMARY KEY,
  pref_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  data_vintage TEXT NOT NULL,   -- 空を許さない。時点不明のデータでは判定しない
  attribution TEXT NOT NULL,
  license TEXT NOT NULL,
  cells INTEGER NOT NULL,
  loaded_at TEXT NOT NULL
);
CREATE TABLE depths (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  rank INTEGER NOT NULL          -- 深いほど大きい。最大値を答えに使う
);
CREATE TABLE cells (
  id INTEGER PRIMARY KEY,
  pref TEXT NOT NULL,
  depth_id INTEGER NOT NULL
);
CREATE VIRTUAL TABLE cells_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE INDEX idx_cells_pref ON cells(pref);
"""

# 浸水深ランクの順序。文字列の先頭の数値で並べると「0.01m以上〜」が正しく最小になる
def depth_rank(label):
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)m以上", label or "")
    return int(float(m.group(1)) * 100) if m else 0


# 整備年度は都道府県ごとに違う（2026-09-06 実測: 愛知=A40-22、静岡・高知=A40-16）。
# 新しい年度から順に探し、見つかった年度をその県のデータ時点として記録する。
YEARS = [22, 21, 20, 19, 18, 17, 16]


def fiscal_vintage(year):
    """A40-YY は年度なので、時点はその年度末(翌年3月31日)とする。"""
    return "%d-03-31" % (2000 + year + 1)


def download(pref, year):
    os.makedirs(RAW, exist_ok=True)
    name = "A40-%02d_%02d_GML.zip" % (year, pref)
    dst = os.path.join(RAW, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return dst
    url = "%s/A40-%02d/%s" % (BASE, year, name)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=900) as r, open(dst, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        return dst
    except Exception:
        if os.path.exists(dst):
            os.remove(dst)
        return None


def shp_of(zpath, tmp):
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    for r, _, fs in os.walk(tmp):
        for x in fs:
            if x.lower().endswith(".shp"):
                return os.path.join(r, x)
    return None


def load_pref(cur, pref, year, vintage):
    """year が None なら新しい年度から順に探す。"""
    zpath = None
    if year:
        zpath = download(pref, year)
        used = year
    else:
        for y in YEARS:
            zpath = download(pref, y)
            if zpath:
                used = y
                break
    if not zpath:
        return 0, None, None
    vintage = vintage or fiscal_vintage(used)
    with tempfile.TemporaryDirectory() as tmp:
        shp = shp_of(zpath, tmp)
        if not shp:
            return 0, None, None
        csv_path = os.path.join(tmp, "cells.csv")
        # 各ポリゴンの外接矩形だけ取り出す。A40は軸平行な矩形なので矩形＝ポリゴン。
        # ENCODING は指定しない（.cpg を尊重する。指定すると二重変換で壊れる）
        subprocess.run(["ogr2ogr", "-f", "CSV", csv_path, shp,
                        "-dialect", "SQLite",
                        "-sql", ("SELECT A40_001 AS pref, A40_003 AS depth, "
                                 "ST_MinX(geometry) AS x0, ST_MaxX(geometry) AS x1, "
                                 "ST_MinY(geometry) AS y0, ST_MaxY(geometry) AS y1 "
                                 "FROM \"%s\"" % os.path.splitext(os.path.basename(shp))[0])],
                       check=True, capture_output=True)
        import csv as _csv
        _csv.field_size_limit(1 << 20)
        n = 0
        pname = None
        with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
            for row in _csv.DictReader(f):
                try:
                    x0, x1 = float(row["x0"]), float(row["x1"])
                    y0, y1 = float(row["y0"]), float(row["y1"])
                except (TypeError, ValueError):
                    continue
                label = (row.get("depth") or "").strip() or "区分なし"
                pname = pname or (row.get("pref") or "").strip()
                cur.execute("INSERT OR IGNORE INTO depths (label, rank) VALUES (?,?)",
                            (label, depth_rank(label)))
                did = cur.execute("SELECT id FROM depths WHERE label=?", (label,)).fetchone()[0]
                cur.execute("INSERT INTO cells (pref, depth_id) VALUES (?,?)",
                            ("%02d" % pref, did))
                cid = cur.lastrowid
                cur.execute("INSERT INTO cells_rtree VALUES (?,?,?,?,?)", (cid, y0, y1, x0, x1))
                n += 1
        if n:
            cur.execute("INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
                        ("%02d" % pref, pname or "", SOURCE_URL, vintage, ATTRIBUTION, LICENSE, n))
        return n, pname, vintage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None, help="A40の整備年度。省略で新しい順に自動探索")
    ap.add_argument("--vintage", default=None, help="データ時点を固定したい場合のみ。既定は見つかった年度の年度末")
    ap.add_argument("--pref", type=int, action="append", help="都道府県コード。省略で沿岸全県")
    ap.add_argument("--fresh", action="store_true", help="DBを作り直す")
    args = ap.parse_args()

    prefs = args.pref or COASTAL
    if args.fresh and os.path.exists(DB):
        os.remove(DB)
    new = not os.path.exists(DB)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    if new:
        cur.executescript(SCHEMA)

    total = 0
    for p in prefs:
        try:
            n, name, vint = load_pref(cur, p, args.year, args.vintage)
        except subprocess.CalledProcessError as e:
            print("  %02d 変換失敗: %s" % (p, e.stderr[:80].decode("utf-8", "replace")))
            continue
        con.commit()
        if n:
            total += n
            print("  %02d %-6s %8d セル  時点 %s" % (p, name or "", n, vint), flush=True)
        else:
            print("  %02d 配布なし" % p, flush=True)
    print("  合計 %d セル / %d 都道府県" % (
        total, cur.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]))
    con.close()


if __name__ == "__main__":
    main()
