#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kurage 津波浸水想定マップ — 住所から津波の浸水深を答える。

khazard / krefuge の設計原則を引き継ぐ:
  - 判定に使ったデータの時点を必ず一緒に出す（都道府県ごとに違うので県単位で）
  - 時点が分からないデータでは判定しない
  - 断定できないところは断定しない

この製品固有の芯:
  「浸水するかどうか」ではなく「何メートル浸かる想定か」を返す。
  0.3mと5mでは取るべき行動が違う（垂直避難で足りるか、水平避難が要るか）。
  あわせて海抜も出す。避難先が今いる場所より高いかが判断の基準になるため。

出典: 国土数値情報「津波浸水想定データ」（国土交通省）
      ＜オープンデータとして利用可（商用利用可・再配信可）＞
"""
import os
import sqlite3
import time
from collections import defaultdict

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ktsunami.db")
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GSI_ELEV = "https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php"
KREFUGE = "https://kurage.exbridge.jp/krefuge.php/"
STALE_YEARS = 5

app = FastAPI(title="Kurage 津波浸水想定マップ")
_hits = defaultdict(list)


def limited(ip, per_min=20):
    now = time.time()
    _hits[ip] = [t for t in _hits[ip] if now - t < 60]
    if len(_hits[ip]) >= per_min:
        return True
    _hits[ip].append(now)
    return False


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def geocode(q):
    r = requests.get(GSI, params={"q": q}, timeout=10,
                     headers={"User-Agent": "ktsunami/1.0 (kurage.exbridge.jp)"})
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    def score(it):
        t = it.get("properties", {}).get("title", "")
        return (q in t, t.startswith(q), -len(t))
    it = max(items, key=score)
    lon, lat = it["geometry"]["coordinates"]
    return {"lat": lat, "lon": lon, "label": it.get("properties", {}).get("title", q)}


def elevation(lat, lon):
    """海抜。津波では避難先が今より高いかが判断の基準になるので必ず添える。"""
    try:
        r = requests.get(GSI_ELEV, params={"lon": lon, "lat": lat, "outtype": "JSON"},
                         timeout=8, headers={"User-Agent": "ktsunami/1.0 (kurage.exbridge.jp)"})
        r.raise_for_status()
        v = r.json().get("elevation")
        if v in (None, "-----"):
            return None
        return {"m": round(float(v), 1), "source": r.json().get("hsrc") or ""}
    except Exception:
        return None


PREF_NAMES = ("北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
              "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
              "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
              "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
              "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
              "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
              "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県")


# 海に面していない8県。ここは津波浸水想定そのものが不要で、
# 「未収録」と言うと誤解を招く（2026-09-06）。
INLAND = {"栃木県", "群馬県", "埼玉県", "山梨県", "長野県", "岐阜県", "滋賀県", "奈良県"}


def pref_of(address):
    for n in PREF_NAMES:
        if address.startswith(n):
            return n
    return None


def staleness(vintage):
    try:
        y, mo, d = (int(x) for x in str(vintage).split("-")[:3])
    except Exception:
        return None, False
    days = (time.time() - time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))) / 86400
    years = round(days / 365.25, 1)
    return years, years >= STALE_YEARS


def advice(rank):
    """浸水深で取るべき行動が変わる。数字だけ出して終わりにしない。"""
    if rank >= 500:
        return "建物の2階では足りない可能性があります。高台や津波避難ビルへの水平避難を前提にしてください。"
    if rank >= 200:
        return "1階は水没する想定です。3階以上、または高台への避難が必要です。"
    if rank >= 100:
        return "1階の床上まで浸水する想定です。2階以上への垂直避難が最低限必要です。"
    if rank >= 30:
        return "床上浸水に達する想定です。徒歩での移動は困難になります。"
    return "浅い浸水の想定ですが、流れがあると歩行は危険です。"


@app.get("/api/check")
def check(request: Request, q: str):
    ip = request.client.host if request.client else "?"
    if limited(ip):
        raise HTTPException(429, "しばらく待ってからお試しください")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "住所を入力してください")
    try:
        g = geocode(q)
    except Exception:
        raise HTTPException(502, "住所検索に接続できませんでした")
    if not g:
        raise HTTPException(404, "住所が見つかりませんでした")

    cur = conn().cursor()
    row = cur.execute(
        "SELECT d.label, d.rank, c.pref FROM cells_rtree r"
        " JOIN cells c ON c.id = r.id JOIN depths d ON d.id = c.depth_id"
        " WHERE ? BETWEEN r.min_lat AND r.max_lat AND ? BETWEEN r.min_lon AND r.max_lon"
        " ORDER BY d.rank DESC LIMIT 1", (g["lat"], g["lon"])).fetchone()

    # 県の収録有無で「区域外」と「そもそも未収録」を区別する。
    # 内陸県には津波浸水想定そのものが無い。黙って「区域外」と答えない。
    pref = row["pref"] if row else None
    ds = None
    if pref:
        ds = cur.execute("SELECT * FROM datasets WHERE pref_code=?", (pref,)).fetchone()
    covered = cur.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]

    elev = elevation(g["lat"], g["lon"])
    vintage = ds["data_vintage"] if ds else None
    years, stale = staleness(vintage) if vintage else (None, False)

    out = {
        "query": q, "resolved": g["label"], "lat": g["lat"], "lon": g["lon"],
        "inundated": bool(row),
        "depth_label": row["label"] if row else None,
        "advice": advice(row["rank"]) if row else None,
        "elevation": elev,
        "pref_name": ds["pref_name"] if ds else None,
        "data_vintage": vintage, "data_age_years": years, "stale": stale,
        "covered_prefs": covered,
        "source": "国土数値情報「津波浸水想定データ」国土交通省",
        "license": "オープンデータ（商用利用可・再配信可）",
        "refuge_url": KREFUGE + "?q=" + requests.utils.quote(q) + "&hazard=tsunami",
        "notes": [
            "本サービスの判定は参考情報です。公的な証明ではありません。",
            "津波浸水想定は都道府県が公表するもので、想定を超える津波が起きないことを意味しません。",
            "住所から求めた座標は町丁目のおおよその位置のため、実際の敷地では結果が変わることがあります。",
            "最終的な確認は、必ず当該自治体が公表する最新の津波ハザードマップで行ってください。",
        ],
    }
    if not row:
        # 「区域外」と「その県が未収録」を必ず区別する。
        # 東京都のように沿岸があるのに本データに無い県があり、
        # 黙って「区域外」と返すと安全だと誤解させる（2026-09-06 実測で発覚）。
        pn = pref_of(g["label"])
        has = bool(pn and cur.execute(
            "SELECT 1 FROM datasets WHERE pref_name=? LIMIT 1", (pn,)).fetchone())
        out["pref_covered"] = has
        out["pref_guess"] = pn
        if pn in INLAND:
            out["note"] = ("%s は海に面していないため、津波浸水想定は作成されていません。"
                           "津波の心配がある地域ではありません。" % pn)
        elif pn and not has:
            out["note"] = ("%s は本データに収録されていません。"
                           "津波浸水想定は都道府県が個別に公表するもので、"
                           "国土数値情報にまだ登録されていない県があります。"
                           "「区域外」という意味ではありません。"
                           "%s の公表情報を直接ご確認ください。" % (pn, pn))
        else:
            out["note"] = ("この地点は、収録している津波浸水想定区域には含まれていません。"
                           "海から離れた場所では、区域外であることに特別な意味はありません。")
    return JSONResponse(out)


@app.get("/healthz")
def healthz():
    try:
        cur = conn().cursor()
        return {"status": "ok",
                "cells": cur.execute("SELECT COUNT(*) FROM cells").fetchone()[0],
                "prefs": cur.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]}
    except Exception as e:
        return JSONResponse({"status": "ng", "error": str(e)}, status_code=500)


PAGE = """<!doctype html><html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurage 津波浸水想定マップ | 住所から津波で何メートル浸かるかを調べる</title>
<meta name="description" content="住所を入れると、その地点の津波浸水想定の深さ（何メートル浸かる想定か）を表示します。海抜も一緒に出るので、避難先が今いる場所より高いかを判断できます。都道府県が公表する津波浸水想定データを収録し、判定に使ったデータの時点も県ごとに表示します。">
<link rel="canonical" href="https://kurage.exbridge.jp/ktsunami.php/">
<meta property="og:type" content="website">
<meta property="og:title" content="Kurage 津波浸水想定マップ｜住所から浸水の深さを調べる">
<meta property="og:description" content="津波で何メートル浸かる想定かを住所から表示。海抜も併記。データの時点は県ごとに明示します。">
<meta property="og:url" content="https://kurage.exbridge.jp/ktsunami.php/">
<meta name="twitter:card" content="summary_large_image">
<style>
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#12202f;line-height:1.75;
 font-family:system-ui,-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif}
.wrap{max-width:840px;margin:0 auto;padding:26px 16px 60px}
h1{font-size:25px;margin:0 0 10px;line-height:1.4}
h1 a{color:inherit;text-decoration:none}
.lead{font-size:15px;color:#37485a;margin:0 0 20px}
.card{border:1px solid #e5ebf1;border-radius:14px;padding:18px;background:#fff}
form{display:flex;gap:8px;flex-wrap:wrap}
input{flex:1 1 240px;min-width:0;padding:12px 13px;font-size:16px;border:1px solid #cdd8e3;border-radius:10px}
button{padding:12px 22px;font-size:15.5px;font-weight:800;color:#fff;background:#0a9a8f;border:0;border-radius:10px;cursor:pointer}
button:disabled{opacity:.5}
.res{margin-top:16px}
.hit{border:2px solid #b3261e;border-radius:11px;padding:14px;background:#fdf3f2}
.hit .d{font-size:26px;font-weight:900;color:#b3261e;line-height:1.3}
.safe{border:1px solid #e5ebf1;border-radius:11px;padding:14px;background:#f4f8fb}
.safe .d{font-size:19px;font-weight:800;color:#37485a}
.adv{margin-top:8px;font-size:15px;font-weight:700}
.meta{font-size:13px;color:#5b6b7a;margin-top:12px;background:#f4f8fb;border-radius:9px;padding:10px 12px}
.notes{font-size:13px;color:#5b6b7a;margin:12px 0 0;padding-left:20px}
.notes li{margin:5px 0}
.warn{color:#b3261e;font-weight:700}
.err{color:#b3261e;font-weight:700}
.cta{display:inline-block;margin-top:10px;padding:11px 20px;font-size:15px;font-weight:800;color:#fff;background:#0a9a8f;border-radius:10px;text-decoration:none}
.src{font-size:12px;color:#7d8a97;margin-top:16px;border-top:1px solid #e5ebf1;padding-top:12px}
.src a{color:#0a9a8f}
.doc{margin-top:34px}
.doc h2{font-size:19px;margin:30px 0 10px;padding-left:12px;border-left:5px solid #0a9a8f}
.doc p,.doc li{font-size:14.5px}
.doc ul{padding-left:22px}.doc li{margin:6px 0}
.t2{width:100%;border-collapse:collapse;margin:10px 0;font-size:14px}
.t2 th,.t2 td{border:1px solid #e5ebf1;padding:9px 11px;text-align:left;vertical-align:top}
.t2 th{background:#f4f8fb;width:34%;font-weight:700}
.tw{overflow-x:auto}
.faq dt{font-weight:800;margin-top:14px;font-size:15px}
.faq dd{margin:5px 0 0;padding-left:16px;border-left:3px solid #e5ebf1;color:#37485a}
</style></head><body><div class="wrap">
<h1><a href="./">Kurage 津波浸水想定マップ</a></h1>
<p class="lead">住所を入れると、その地点が<strong>津波で何メートル浸かる想定か</strong>を表示します。
<strong>海抜（標高）</strong>も一緒に出るので、避難先が今いる場所より高いかを判断できます。
判定に使ったデータの時点も、都道府県ごとに表示します。</p>
<div class="card">
  <form id="f">
    <input id="q" placeholder="例: 愛知県名古屋市港区港明" autocomplete="off">
    <button id="b">調べる</button>
  </form>
  <div class="res" id="r"></div>
</div>
<section class="doc">
<h2>「浸水するか」ではなく「何メートルか」を返します</h2>
<p>0.3mと5mでは、取るべき行動がまったく違います。前者なら2階への垂直避難で足りることがありますが、
後者は建物ごと水没する想定です。だから本サービスは○×ではなく<strong>深さの区分</strong>を返し、
それぞれで何をすべきかを添えます。</p>
<div class="tw"><table class="t2">
<tr><th>0.01m〜0.3m未満</th><td>浅い浸水の想定ですが、流れがあると歩行は危険です。</td></tr>
<tr><th>0.3m〜1m未満</th><td>床上浸水に達する想定です。徒歩での移動は困難になります。</td></tr>
<tr><th>1m〜2m未満</th><td>1階の床上まで浸水します。2階以上への垂直避難が最低限必要です。</td></tr>
<tr><th>2m〜5m未満</th><td>1階は水没する想定です。3階以上、または高台への避難が必要です。</td></tr>
<tr><th>5m以上</th><td>建物の2階では足りない可能性があります。高台や津波避難ビルへの水平避難が前提です。</td></tr>
</table></div>
<h2>「区域外」に意味がない場合があります</h2>
<p>津波浸水想定は<strong>沿岸のある都道府県だけ</strong>が作成します。内陸の県には、そもそも想定が存在しません。
そのため本サービスは、区域外と判定したときに<strong>その県が収録されているかどうかを区別して伝えます</strong>。
「区域外です」とだけ返して安心させることはしません。</p>
<h2>データの時点は都道府県ごとに違います</h2>
<p>津波浸水想定は都道府県が個別に公表するため、<strong>整備年度が県によって大きく異なります</strong>。
実測したところ、愛知県は令和4年度、静岡県と高知県は平成28年度でした。
本サービスは判定結果に、その県のデータがいつのものかを併記します。
時点が確認できないデータでは判定しません。</p>
<h2>あわせて確認したい方へ</h2>
<p>津波で使える避難所がどこにあり、そこまで徒歩何分かは
<a href="/krefuge.php/">Kurage 避難所マップ</a>で調べられます。
土砂災害の警戒区域は<a href="/khazard.php/">Kurage 土砂災害ハザードマップ</a>です。</p>
<h2>よくある質問</h2>
<dl class="faq">
<dt>無料で使えますか。</dt><dd>はい。登録もログインも不要です。</dd>
<dt>この結果は公的な証明になりますか。</dt><dd>なりません。参考情報です。最終的な確認は必ず当該自治体の最新の津波ハザードマップで行ってください。</dd>
<dt>想定を超える津波は来ませんか。</dt><dd>来ないとは言えません。津波浸水想定は一定の条件で計算されたもので、それを超える事象を否定するものではありません。</dd>
<dt>自社のサーバーで動かせますか。</dt><dd>はい。買い切り版を用意する予定です。住所を外部に送りたくない場合にご利用ください。</dd>
</dl>
</section>
<p class="src">出典: 国土数値情報「津波浸水想定データ」（国土交通省）を加工して作成
＜オープンデータとして利用可（商用利用可・再配信可）＞ ／
住所検索・標高: 国土地理院 地名検索API／標高API</p>
</div>
<script>
var f=document.getElementById('f'),q=document.getElementById('q'),b=document.getElementById('b'),r=document.getElementById('r');
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function run(){
  var v=q.value.trim(); if(!v)return;
  b.disabled=true; r.innerHTML='<p>調べています…</p>';
  fetch('api/check?q='+encodeURIComponent(v))
   .then(function(x){return x.json().then(function(j){if(!x.ok)throw new Error(j.detail||'エラー');return j;});})
   .then(function(d){
     var o='<p><strong>'+esc(d.resolved)+'</strong></p>';
     if(d.inundated){
       o+='<div class="hit"><div class="d">浸水想定 '+esc(d.depth_label)+'</div>'
         +'<div class="adv">'+esc(d.advice)+'</div></div>';
     }else{
       o+='<div class="safe"><div class="d">津波浸水想定区域には含まれていません</div>'
         +'<div class="adv" style="font-weight:400;font-size:14px">'+esc(d.note||'')+'</div></div>';
     }
     if(d.elevation){
       o+='<div class="meta"><strong>この地点の海抜（標高）: '+d.elevation.m+'m</strong>'
         +(d.elevation.source?'　<span style="font-weight:400">測定: '+esc(d.elevation.source)+'（国土地理院）</span>':'')+'</div>';
     }
     o+='<div class="meta">'
       +(d.data_vintage?'この判定に使ったデータの時点: <strong>'+esc(d.data_vintage)+'</strong>'
          +(d.pref_name?'（'+esc(d.pref_name)+'）':'')
          +(d.data_age_years!=null?'　約'+d.data_age_years+'年前':'')
        :'収録している都道府県は'+d.covered_prefs+'件です')
       +'</div>';
     if(d.stale)o+='<p class="warn">このデータは公表から'+d.data_age_years+'年が経過しています。自治体の最新情報を必ずご確認ください。</p>';
     o+='<a class="cta" href="'+esc(d.refuge_url)+'">津波で使える避難所を探す →</a>';
     o+='<ul class="notes">';
     d.notes.forEach(function(n){o+='<li>'+esc(n)+'</li>';});
     o+='</ul>';
     r.innerHTML=o;
   })
   .catch(function(e){r.innerHTML='<p class="err">'+esc(e.message)+'</p>';})
   .then(function(){b.disabled=false;});
}
f.addEventListener('submit',function(e){e.preventDefault();run();});
(function(){var p=new URLSearchParams(location.search); if(p.get('q')){q.value=p.get('q'); run();}})();
</script>
<!-- 計測タグ(simpletrack)は ktsunami.php プロキシ側が </head> 直前に注入する -->
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE)
