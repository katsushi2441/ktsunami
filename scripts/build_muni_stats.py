#!/usr/bin/env python3
"""市区町村ごとの津波浸水想定の統計を作る（地域ページの中身）。

A40（津波浸水想定）は**市区町村コードを持たない**。都道府県と浸水深ランクだけ。
そこで市区町村単位で言える事実として、krefuge（国土地理院 指定緊急避難場所）の
実在する施設の座標を使い、**その施設が津波浸水想定区域の中にあるか**を数える。

ここで測っているのは「市域の何%が浸水するか」ではない（面積の標本にはなっていない）。
「その市区町村の指定緊急避難場所のうち何件が浸水想定区域内にあるか」であり、
避難先そのものが浸水する想定かどうかという、確かめられる事実。ページにもそう書く。

  cd /home/kojima/work/ktsunami && /usr/bin/python3 scripts/build_muni_stats.py
"""
import os, json, sqlite3, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ktsunami.db")
KREFUGE = "/home/kojima/work/krefuge/data/krefuge.db"

DDL = """
CREATE TABLE IF NOT EXISTS muni_stats (
  muni_code TEXT PRIMARY KEY, pref_code TEXT, pref TEXT, muni TEXT,
  shelters INTEGER, inundated INTEGER, max_rank INTEGER, max_label TEXT,
  tsunami_shelters INTEGER, tsunami_inundated INTEGER, samples TEXT
);
CREATE INDEX IF NOT EXISTS muni_stats_pref ON muni_stats(pref_code);
"""
Q = ("SELECT d.rank, d.label FROM cells_rtree r JOIN cells c ON c.id = r.id"
     " JOIN depths d ON d.id = c.depth_id"
     " WHERE ? BETWEEN r.min_lat AND r.max_lat AND ? BETWEEN r.min_lon AND r.max_lon"
     " ORDER BY d.rank DESC LIMIT 1")


def main():
    t = sqlite3.connect(DB)
    t.executescript(DDL)
    prefs = {c: n for c, n in t.execute("SELECT pref_code, pref_name FROM datasets")}
    k = sqlite3.connect(KREFUGE)
    k.row_factory = sqlite3.Row
    try:
        rows = k.execute("SELECT muni_code, pref_code, pref, muni FROM muni_stats").fetchall()
    except sqlite3.OperationalError:
        print("krefuge の muni_stats がありません。先に krefuge 側を作ってください。")
        return
    # krefuge 側の市区町村割り当て（住所から判定済み）を再利用する
    by_code = {r["muni_code"]: dict(r) for r in rows}

    agg = {}
    for s in k.execute("SELECT pref, address, name, lat, lon, tsunami FROM shelters"):
        pass  # 施設→市区町村の対応は krefuge 側の判定に合わせるため下で引き直す
    k.close()

    # krefuge の判定ロジックをそのまま使う（重複実装しない）
    import sys
    sys.path.insert(0, "/home/kojima/work/krefuge/scripts")
    from muni import load_canon, extract, PREFS
    code_by_pref = {p: f"{i+1:02d}" for i, p in enumerate(PREFS)}
    _, canon = load_canon(KREFUGE)

    k = sqlite3.connect(KREFUGE)
    for pref, address, name, lat, lon, tsu in k.execute(
            "SELECT pref, address, name, lat, lon, tsunami FROM shelters"):
        pc = code_by_pref.get(pref)
        if not pc or pc not in prefs or lat is None or lon is None:
            continue          # 津波浸水想定が公表されていない県は対象外
        hit = extract(pc, address, canon)
        if not hit:
            continue
        mname, code = hit
        a = agg.get(code)
        if a is None:
            a = agg[code] = dict(pref_code=pc, pref=pref, muni=mname, shelters=0, inundated=0,
                                 max_rank=-1, max_label="", tsunami_shelters=0,
                                 tsunami_inundated=0, samples=[])
        a["shelters"] += 1
        if tsu:
            a["tsunami_shelters"] += 1
        r = t.execute(Q, (lat, lon)).fetchone()
        if r:
            a["inundated"] += 1
            if tsu:
                a["tsunami_inundated"] += 1
            if r[0] > a["max_rank"]:
                a["max_rank"], a["max_label"] = r[0], r[1]
            if len(a["samples"]) < 5 and name:
                a["samples"].append({"name": name, "address": address, "depth": r[1],
                                     "tsunami_ok": bool(tsu)})
    k.close()

    t.execute("DELETE FROM muni_stats")
    t.executemany(
        "INSERT INTO muni_stats (muni_code,pref_code,pref,muni,shelters,inundated,max_rank,"
        "max_label,tsunami_shelters,tsunami_inundated,samples) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(c, a["pref_code"], a["pref"], a["muni"], a["shelters"], a["inundated"],
          a["max_rank"], a["max_label"], a["tsunami_shelters"], a["tsunami_inundated"],
          json.dumps(a["samples"], ensure_ascii=False)) for c, a in agg.items()])
    t.commit()
    n_in = sum(1 for a in agg.values() if a["inundated"])
    print(f"市区町村 {len(agg):,}（うち浸水想定区域内に避難所がある {n_in:,}）")
    for r in t.execute("SELECT pref,muni,shelters,inundated,max_label FROM muni_stats "
                       "ORDER BY inundated DESC LIMIT 5"):
        print(f"   {r[0]}{r[1]}: 避難所{r[2]:,} うち浸水想定内{r[3]:,} 最大{r[4]}")
    print("  内陸の確認（0であるべき）:")
    for r in t.execute("SELECT pref,muni,shelters,inundated FROM muni_stats WHERE muni IN "
                       "('豊田市','長野市','甲府市','前橋市')"):
        print(f"   {r[0]}{r[1]}: 避難所{r[2]:,} うち浸水想定内{r[3]:,}")
    t.close()


if __name__ == "__main__":
    main()
