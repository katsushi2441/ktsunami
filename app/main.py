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
import json
import os
import re
import sqlite3
import time
from collections import defaultdict

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

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
<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script><script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>
<title>Kurage 津波浸水想定マップ | 住所から津波で何メートル浸かるかを調べる</title>
<meta name="description" content="住所を入れると、その地点の津波浸水想定の深さ（何メートル浸かる想定か）を表示します。海抜も一緒に出るので、避難先が今いる場所より高いかを判断できます。都道府県が公表する津波浸水想定データを収録し、判定に使ったデータの時点も県ごとに表示します。">
<link rel="canonical" href="https://kurage.exbridge.jp/ktsunami.php/">
<meta property="og:type" content="website">
<meta property="og:title" content="Kurage 津波浸水想定マップ｜住所から浸水の深さを調べる">
<meta property="og:description" content="津波で何メートル浸かる想定かを住所から表示。海抜も併記。データの時点は県ごとに明示します。">
<meta property="og:url" content="https://kurage.exbridge.jp/ktsunami.php/">
<meta name="twitter:card" content="summary_large_image">
<meta property="og:image" content="https://kurage.exbridge.jp/pv/ktsunami-pv-poster.jpg">
<meta property="og:locale" content="ja_JP">
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": "津波で自宅が何メートル浸かるかは、どうやって調べますか？", "acceptedAnswer": {"@type": "Answer", "text": "都道府県が公表している津波浸水想定で確認します。このサイトでは住所を入れると、その地点の想定浸水深と海抜を表示します。国土交通省の国土数値情報（津波浸水想定）を収録しており、判定に使ったデータの時点も県ごとに表示します。"}}, {"@type": "Question", "name": "「区域外」と表示されれば安全ですか？", "acceptedAnswer": {"@type": "Answer", "text": "いいえ。津波浸水想定を公表していない地域は「未収録」であって、津波が来ないという意味ではありません。また想定は前提を置いた計算であり、実際の浸水がこのとおりになると決まっているわけでもありません。"}}, {"@type": "Question", "name": "海抜も一緒に表示されるのはなぜですか？", "acceptedAnswer": {"@type": "Answer", "text": "避難先が今いる場所より高いかどうかを判断するためです。浸水深だけでは、どこへ逃げれば足りるのかが分かりません。"}}, {"@type": "Question", "name": "この判定は公的な証明になりますか？", "acceptedAnswer": {"@type": "Answer", "text": "なりません。住所から求めた代表点による参考情報です。避難計画は自治体の最新のハザードマップと避難所情報で確認してください。"}}]}</script>
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
.pv{margin:16px 0}
.pv video{width:100%;height:auto;border-radius:12px;border:1px solid #e5ebf1;background:#000;display:block}
.note-sm{font-size:12.5px;color:#7d8a97;margin:5px 0 0}
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
<div class="pv">
<video src="https://kurage.exbridge.jp/pv/ktsunami-pv-30s.mp4"
       poster="https://kurage.exbridge.jp/pv/ktsunami-pv-poster.jpg"
       controls playsinline preload="none" width="1920" height="1080"></video>
<p class="note-sm">冒頭8秒の実写映像は MiniMax H3（セルフホスト）で生成しています。</p>
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
<h2>地図で見る</h2>
<p>浸水想定区域を地図に重ねて表示します。クリックするとその地点の想定浸水深と海抜が出ます。 <a href="map/"><b>地図を開く</b></a></p>

<h2>主要都市から探す</h2>
<p><a href="area/aichi-nagoya">名古屋市</a>・<a href="area/osaka-osaka">大阪市</a>・<a href="area/kanagawa-yokohama">横浜市</a>・<a href="area/kochi-kochi">高知市</a>・<a href="area/miyagi-sendai">仙台市</a>・<a href="area/shizuoka-shizuoka">静岡市</a>ほか <a href="area/">地域一覧はこちら</a></p>
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
<p style="font-size:12.5px;color:#7d8a97;margin-top:10px">議員・政党事務所の方へ: このページを事務所の名前で運用できます → <a href="/bousai-giin.html">地域防災情報サービス</a></p>
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


# ---- 地域ページ（「名古屋 津波」等の地域名×災害の無競合ロングテールを取る） ----
# 2026-09-06 実測: 名古屋ハザードマップ1,900/指数0・愛知県津波ハザードマップ480・
# 名古屋津波320・高知津波1,000・仙台津波1,000 いずれも競合指数0。都市名を主題にした
# 個別ランディングで拾う。薄いページにしないため、県の収録区画数・データ時点・
# 近隣製品への実リンクを必ず入れる。CSS/JSは本体PAGEから取り出して共有する。
_STYLE = re.search(r"<style>.*?</style>", PAGE, re.S).group(0)
# 地域ページは /ktsunami.php/area/<slug> と1階層深いので、本体の相対 fetch('api/check')
# のままだと /area/api/check を叩いて404になる。共有JSのAPIパスを ../ で補正する。
_SCRIPT = re.search(r"<script>.*?</script>", PAGE, re.S).group(0).replace("'api/check", "'../api/check")

AREAS = [
    ("aichi-nagoya", "名古屋市", "23", "愛知県", "愛知県名古屋市港区港明"),
    ("aichi-toyohashi", "豊橋市", "23", "愛知県", "愛知県豊橋市神野新田町"),
    ("osaka-osaka", "大阪市", "27", "大阪府", "大阪府大阪市住之江区南港北"),
    ("kanagawa-yokohama", "横浜市", "14", "神奈川県", "神奈川県横浜市中区海岸通"),
    ("shizuoka-shizuoka", "静岡市", "22", "静岡県", "静岡県静岡市清水区港町"),
    ("kochi-kochi", "高知市", "39", "高知県", "高知県高知市種崎"),
    ("miyagi-sendai", "仙台市", "04", "宮城県", "宮城県仙台市宮城野区蒲生"),
    ("hyogo-kobe", "神戸市", "28", "兵庫県", "兵庫県神戸市中央区波止場町"),
    ("fukuoka-fukuoka", "福岡市", "40", "福岡県", "福岡県福岡市博多区沖浜町"),
    ("wakayama-wakayama", "和歌山市", "30", "和歌山県", "和歌山県和歌山市湊"),
]
AREA_BY_SLUG = {a[0]: a for a in AREAS}


def _area_head(city, pref, slug, desc):
    url = "https://kurage.exbridge.jp/ktsunami.php/area/" + slug
    title = city + "の津波浸水想定マップ｜住所を入れて浸水の深さと海抜を調べる | Kurage"
    ga = ('<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script>'
          '<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}'
          "gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>")
    bc = json.dumps({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Kurage 津波浸水想定マップ",
         "item": "https://kurage.exbridge.jp/ktsunami.php/"},
        {"@type": "ListItem", "position": 2, "name": city, "item": url}]}, ensure_ascii=False)
    faq = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": city + "の津波浸水想定はどこで調べられますか？",
         "acceptedAnswer": {"@type": "Answer", "text": "このページで" + city + "の住所を入れると、津波浸水想定の深さと海抜が表示されます。" + pref + "が公表したデータにもとづく参考情報で、最終確認は自治体の最新ハザードマップで行ってください。"}}]}, ensure_ascii=False)
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>" + title + "</title>"
            '<meta name="description" content="' + desc + '">'
            '<link rel="canonical" href="' + url + '">'
            '<meta property="og:type" content="website">'
            '<meta property="og:title" content="' + city + 'の津波浸水想定マップ｜Kurage">'
            '<meta property="og:description" content="' + desc + '">'
            '<meta property="og:url" content="' + url + '">'
            '<meta property="og:image" content="https://kurage.exbridge.jp/pv/ktsunami-pv-poster.jpg">'
            '<meta name="twitter:card" content="summary_large_image">'
            '<script type="application/ld+json">' + bc + '</script>'
            '<script type="application/ld+json">' + faq + '</script>' + ga)


@app.get("/area/{slug}", response_class=HTMLResponse)
def area(slug: str):
    a = AREA_BY_SLUG.get(slug)
    if not a:
        raise HTTPException(404, "地域が見つかりません")
    _, city, pcode, pref, example = a
    ds = conn().cursor().execute("SELECT * FROM datasets WHERE pref_code=?", (pcode,)).fetchone()
    cells = ("{:,}区画".format(ds["cells"])) if ds else "収録あり"
    vint = ds["data_vintage"] if ds else "—"
    exq = requests.utils.quote(example)
    desc = (city + "（" + pref + "）の住所を入れると、津波で何メートル浸かる想定かと海抜を表示します。"
            + pref + "が公表した津波浸水想定データを収録。無料・登録不要。データの時点も明記します。")
    body = (
        '<h1><a href="/ktsunami.php/">' + city + "の津波浸水想定マップ</a></h1>"
        '<p class="lead">' + city + "（" + pref + "）の住所を入れると、その地点が<strong>津波で何メートル浸かる想定か</strong>を表示します。"
        "<strong>海抜（標高）</strong>も一緒に出るので、避難先が今いる場所より高いかを判断できます。"
        + pref + "の津波浸水想定データ（" + cells + "・データ時点 " + vint + "）を収録しています。</p>"
        '<div class="card"><form id="f">'
        '<input id="q" placeholder="例: ' + example + '" value="' + example + '" autocomplete="off">'
        '<button id="b">調べる</button></form><div class="res" id="r"></div></div>'
        '<section class="doc">'
        "<h2>" + city + "で津波浸水想定を調べる</h2>"
        "<p>" + city + "の沿岸部の住所を入れると、浸水深の区分（0.3m未満〜10m以上）と、"
        "取るべき行動（垂直避難で足りるか、高台への水平避難が必要か）を返します。"
        + pref + "が公表した津波浸水想定データにもとづきます。</p>"
        "<h2>「区域外」と出たとき</h2>"
        "<p>海から離れた" + city + "内陸部では、区域外であることに特別な意味はありません。"
        "収録していない都道府県では「区域外」ではなく「未収録」と表示し、区別しています。</p>"
        "<h2>あわせて確認したい方へ</h2>"
        '<p>' + city + "で津波のとき使える避難所は "
        '<a href="/krefuge.php/?q=' + exq + '&hazard=tsunami">避難所マップ</a>、土砂災害の警戒区域は '
        '<a href="/khazard.php/?q=' + exq + '">土砂災害ハザードマップ</a> で調べられます。'
        '全国版は <a href="/ktsunami.php/">Kurage 津波浸水想定マップ</a> です。</p></section>'
        '<p class="src">出典: 国土数値情報「津波浸水想定データ」（国土交通省）を加工して作成'
        "＜オープンデータとして利用可（商用利用可・再配信可）＞ ／"
        "住所検索・標高: 国土地理院 地名検索API／標高API</p>")
    html = _area_head(city, pref, slug, desc) + _STYLE + '</head><body><div class="wrap">' + body + _SCRIPT + "</body></html>"
    return HTMLResponse(html)


@app.get("/area", response_class=HTMLResponse)
@app.get("/area/", response_class=HTMLResponse)
def area_index():
    links = "".join('<li><a href="/ktsunami.php/area/' + s + '">' + c + "の津波浸水想定マップ</a></li>"
                    for s, c, *_ in AREAS)
    desc = "主要な沿岸都市ごとの津波浸水想定マップの入口です。住所を入れると浸水深と海抜が分かります。"
    html = (_area_head("地域一覧", "全国", "index", desc) + _STYLE
            + '</head><body><div class="wrap"><h1>地域から津波浸水想定を調べる</h1>'
            '<p class="lead">主要な沿岸都市ごとの入口です。全国版は '
            '<a href="/ktsunami.php/">Kurage 津波浸水想定マップ</a> をどうぞ。</p>'
            '<ul style="font-size:15px;line-height:2.2">' + links + "</ul></div></body></html>")
    return HTMLResponse(html)


_LLMS_BODY = """# Kurage 津波浸水想定マップ

> 住所を入れると、津波浸水想定区域の内外と想定される浸水深を返すサイト。海抜も併記する。

## 収録
- セル数: 3,795,197
- 都道府県: 35（津波浸水想定を公表している都道府県）
- 出典: 国土交通省 国土数値情報（津波浸水想定）を加工して作成

## 大事な区別
- **「区域外」は「安全」ではない。** 想定を公表していない地域は「未収録」であって、
  津波が来ないという意味ではない。
- これは想定であり、実際の浸水がこのとおりになると決まっているわけではない。

## 使い方
- 住所で調べる: https://kurage.exbridge.jp/ktsunami.php/?q=<住所>
- 地図で見る: https://kurage.exbridge.jp/ktsunami.php/map/ （浸水想定を地図に重ねて表示。クリックした地点を判定）
- 座標で判定するAPI: https://kurage.exbridge.jp/ktsunami.php/api/at?lat=<緯度>&lon=<経度>
- API: https://kurage.exbridge.jp/ktsunami.php/api/check?q=<住所>

## 関連（同じ運営の防災ツール）
- 洪水・内水・高潮: https://kurage.exbridge.jp/kflood.php/
- 土砂災害: https://kurage.exbridge.jp/khazard.php/
- 避難所: https://kurage.exbridge.jp/krefuge.php/

運営: 株式会社エクスブリッジ https://exbridge.jp/
"""

# ---- AEO/GEO の標準セット（llms.txt / robots.txt / sitemap.xml）----
# 他のKurage製品と同じ形にそろえる。AI検索に「何を答えるサイトか」を最初に渡す。
_MAP_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-BP0650KDFR"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments)}gtag('js',new Date());gtag('config','G-BP0650KDFR');</script>
<script>(function(){var s=document.createElement('script');s.src='https://kurage.exbridge.jp/simpletrack.php?url='+encodeURIComponent(location.href)+'&ref='+encodeURIComponent(document.referrer);s.async=true;document.head.appendChild(s)})();</script>
<title>地図で見る｜Kurage 津波浸水想定マップ</title>
<meta name="description" content="津波浸水想定区域を地図で見られます。クリックするとその地点の想定浸水深と海抜を判定します。都道府県が公表する津波浸水想定データを収録。">
<link rel="canonical" href="https://kurage.exbridge.jp/ktsunami.php/map/">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="website"><meta property="og:site_name" content="Kurage">
<meta property="og:title" content="地図で見る｜Kurage 津波浸水想定マップ">
<meta property="og:description" content="津波浸水想定区域を地図で。クリックで浸水深と海抜を判定します。">
<meta property="og:url" content="https://kurage.exbridge.jp/ktsunami.php/map/">
<meta property="og:image" content="https://kurage.exbridge.jp/pv/ktsunami-pv-poster.jpg">
<meta name="twitter:card" content="summary_large_image">
<link href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<style>
:root{--ink:#12202f;--muted:#5a6a7a;--line:#dce7ea;--teal:#0a9a8f;--deep:#0a726b;--paper:#f7fbfa}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);line-height:1.75;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif}
header{background:#fff;border-bottom:1px solid var(--line)}
.bar{max-width:1040px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.brand{font-weight:800;color:var(--ink);text-decoration:none;font-size:16px}
.brand small{display:block;font-weight:500;font-size:11.5px;color:var(--muted)}
.bar nav{margin-left:auto}.bar nav a{color:var(--deep);text-decoration:none;font-size:13.5px;margin-left:14px}
main{max-width:1040px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:clamp(19px,3.2vw,25px);margin:0 0 8px}
.muted{color:var(--muted);font-size:13.5px}
.maprow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,320px);gap:14px;margin-top:14px}
@media(max-width:820px){.maprow{grid-template-columns:minmax(0,1fr)}}
#map{height:min(70vh,620px);border-radius:12px;border:1px solid var(--line);min-width:0}
.side{min-width:0}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.legend{background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12.5px;margin-top:10px}
.legend i{display:inline-block;width:14px;height:14px;border-radius:3px;vertical-align:-2px;margin-right:6px}
.note{background:#fff8e8;border:1px solid #ecd8a7;border-radius:9px;padding:10px 12px;font-size:12.5px;margin:10px 0 0}
form.search{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
input[type=text]{flex:1;min-width:min(100%,220px);padding:11px 13px;border:1px solid var(--line);border-radius:9px;font-size:15px}
button.go{padding:11px 20px;border:0;border-radius:9px;background:linear-gradient(135deg,var(--teal),var(--deep));color:#fff;font-weight:700;cursor:pointer}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:6px 0}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left}
th{background:#eef6f5;width:40%;white-space:nowrap}
</style></head><body>
<header><div class="bar">
 <a class="brand" href="../">Kurage 津波浸水想定マップ<small>EXBRIDGE, INC.</small></a>
 <nav><a href="../">住所で調べる</a><a href="./">地図で見る</a></nav>
</div></header>
<main>
<h1>地図で見る</h1>
<p class="muted">都道府県が公表する津波浸水想定です。<b>地図をクリック</b>するとその地点を判定します（海抜も出ます）。</p>

<div class="maprow">
 <div id="map"></div>
 <div class="side">
  <div class="card" id="result"><p class="muted" style="margin:0">地図をクリックすると、ここに判定が出ます。</p></div>
  <div class="legend" id="legend"></div>
  <div class="note" id="hint" hidden>もう少し<b>拡大</b>すると浸水想定が表示されます。</div>
  <div class="note" id="trunc" hidden>この範囲はセルが多すぎて<b>一部しか表示していません</b>。拡大すると全部出ます。</div>
  <div class="note"><b>色が付いていない＝安全ではありません。</b>想定を公表していない地域は収録していません。</div>
 </div>
</div>

<form class="search" method="get" action="./">
 <input type="text" name="q" value="__Q__" placeholder="住所で移動（例: 静岡県下田市）">
 <button class="go" type="submit">移動</button>
</form>
</main>
<script>
var BASE='../';
var COLORS=['#cfe8f5','#8fc9e8','#f0b429','#e8743b','#c0392b'];
var LABELS=['0.3m未満','0.3〜1m','1〜3m','3〜5m','5m以上'];
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
(function(){var h='<b>想定される浸水深</b>';
 for(var i=0;i<COLORS.length;i++){h+='<div><i style="background:'+COLORS[i]+'"></i>'+LABELS[i]+'</div>';}
 document.getElementById('legend').innerHTML=h;})();
var map=new maplibregl.Map({container:'map',
 style:{version:8,sources:{gsi:{type:'raster',tiles:['https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png'],tileSize:256,attribution:'国土地理院'}},
 layers:[{id:'gsi',type:'raster',source:'gsi'}]},
 center:[__LON__,__LAT__],zoom:__ZOOM__});
map.addControl(new maplibregl.NavigationControl({showCompass:false}),'top-right');
var loading=false;
function load(){
 if(loading||map.getZoom()<13){document.getElementById('hint').hidden=false;
  if(map.getSource('c'))map.getSource('c').setData({type:'FeatureCollection',features:[]});return;}
 document.getElementById('hint').hidden=true; loading=true;
 var b=map.getBounds();
 fetch(BASE+'api/cells.geojson?bbox='+[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].join(','))
  .then(function(r){return r.json()}).then(function(j){
    if(j.features&&map.getSource('c'))map.getSource('c').setData(j);
    document.getElementById('trunc').hidden=!j.truncated;
  }).catch(function(){}).then(function(){loading=false;});
}
map.on('load',function(){
 map.addSource('c',{type:'geojson',data:{type:'FeatureCollection',features:[]}});
 map.addLayer({id:'c',type:'fill',source:'c',
  paint:{'fill-color':['match',['get','cls'],0,COLORS[0],1,COLORS[1],2,COLORS[2],3,COLORS[3],4,COLORS[4],'#ccc'],'fill-opacity':0.6}});
 load();
});
map.on('moveend',load); map.on('zoomend',load);
var marker=null;
function judge(lat,lon){
 document.getElementById('result').innerHTML='<p class="muted" style="margin:0">判定しています…</p>';
 fetch(BASE+'api/at?lat='+lat+'&lon='+lon).then(function(r){return r.json()}).then(function(j){
  var elev=(j.elevation&&j.elevation.m!=null)?('<tr><th>海抜</th><td>'+esc(j.elevation.m)+' m</td></tr>'):'';
  var h='';
  if(j.inundated){
   h='<div style="font-weight:800;color:#c0392b;margin-bottom:6px">津波浸水想定区域の中です</div><table>'
    +'<tr><th>想定される浸水深</th><td><b>'+esc(j.depth_label||'')+'</b></td></tr>'
    +(j.pref_name?'<tr><th>公表</th><td>'+esc(j.pref_name)+'</td></tr>':'')+elev+'</table>'
    +(j.advice?'<div class="note">'+esc(j.advice)+'</div>':'')
    +'<p style="margin:10px 0 0"><a href="'+BASE+'" style="color:#0a726b">住所で詳しく調べる →</a></p>';
  }else{
   h='<div style="font-weight:800;color:#0a726b;margin-bottom:6px">想定区域には入っていません</div>'
    +(elev?'<table>'+elev+'</table>':'')
    +'<div class="note">'+esc(j.note||'')+'</div>';
  }
  document.getElementById('result').innerHTML=h;
  if(marker)marker.remove();
  marker=new maplibregl.Marker({color:'#0a9a8f'}).setLngLat([lon,lat]).addTo(map);
 }).catch(function(){document.getElementById('result').innerHTML='<p class="muted" style="margin:0">判定できませんでした。もう一度クリックしてください。</p>';});
}
map.on('click',function(e){judge(+e.lngLat.lat.toFixed(6),+e.lngLat.lng.toFixed(6))});
__AUTO__
</script></body></html>"""


# 浸水深の段階（depths.rank）を5色に畳む。実データの rank は
# 0,1,30,50,100,200,300,400,500,1000,1500,2000 の12種類（全国で実測）
def _depth_cls(rank):
    if rank is None: return 0
    if rank < 30:   return 0   # 0.3m未満
    if rank < 100:  return 1   # 0.3〜1m
    if rank < 300:  return 2   # 1〜3m（2m以上を含む区分もここ）
    if rank < 500:  return 3   # 3〜5m
    return 4                   # 5m以上


@app.get("/api/at")
def check_at(request: Request, lat: float, lon: float):
    """座標での判定（地図クリック用）。住所を介さないので代表点のズレが無い。

    区域外のとき「この付近が収録済みかどうか」を、周囲のセルの有無で実測して返す。
    収録していない県を黙って「区域外」と答えると、安全だと誤解させる（/api/check と同じ方針）。
    """
    ip = request.client.host if request.client else "?"
    if limited(ip, per_min=60):
        raise HTTPException(429, "しばらく待ってからお試しください")
    cur = conn().cursor()
    row = cur.execute(
        "SELECT d.label, d.rank, c.pref FROM cells_rtree r"
        " JOIN cells c ON c.id = r.id JOIN depths d ON d.id = c.depth_id"
        " WHERE ? BETWEEN r.min_lat AND r.max_lat AND ? BETWEEN r.min_lon AND r.max_lon"
        " ORDER BY d.rank DESC LIMIT 1", (lat, lon)).fetchone()
    out = {
        "lat": lat, "lon": lon,
        "inundated": bool(row),
        "depth_label": row["label"] if row else None,
        "rank": row["rank"] if row else None,
        "advice": advice(row["rank"]) if row else None,
        "elevation": elevation(lat, lon),
        "pref_name": None,
    }
    if row:
        ds = cur.execute("SELECT pref_name FROM datasets WHERE pref_code=?", (row["pref"],)).fetchone()
        out["pref_name"] = ds["pref_name"] if ds else None
        return JSONResponse(out)

    # 約20km四方に収録セルがあるか。あれば「この付近は収録済みで、ここは区域外」と言い切れる。
    d = 0.09
    near = cur.execute(
        "SELECT c.pref FROM cells_rtree r JOIN cells c ON c.id = r.id"
        " WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ? LIMIT 1",
        (lat - d, lat + d, lon - d, lon + d)).fetchone()
    out["nearby_covered"] = bool(near)
    if near:
        ds = cur.execute("SELECT pref_name FROM datasets WHERE pref_code=?", (near["pref"],)).fetchone()
        out["pref_name"] = ds["pref_name"] if ds else None
        out["note"] = ("この付近の津波浸水想定は収録していますが、この地点は区域に含まれていません。"
                       "想定を超える津波が起きないという意味ではありません。")
    else:
        out["note"] = ("この付近には収録している津波浸水想定がありません。"
                       "内陸のため想定が作られていないか、その県が国土数値情報にまだ登録していない"
                       "かのどちらかです。「区域外なので安全」という意味ではありません。")
    return JSONResponse(out)


@app.get("/api/cells.geojson")
def cells_geojson(bbox: str = "", limit: int = 6000):
    """表示範囲の浸水想定セルを GeoJSON で返す。bbox は minlon,minlat,maxlon,maxlat。

    セルは緯度経度に平行な矩形で、四隅が cells_rtree に入っている。
    ポリゴンを保存していないので、そこから組み立てて返す。
    """
    try:
        minlon, minlat, maxlon, maxlat = [float(v) for v in bbox.split(",")]
    except ValueError:
        return JSONResponse({"error": "bbox は minlon,minlat,maxlon,maxlat の形で渡してください"}, status_code=400)
    c = conn()
    try:
        rows = c.execute(
            "SELECT r.min_lon, r.min_lat, r.max_lon, r.max_lat, d.label, d.rank"
            " FROM cells_rtree r JOIN cells x ON x.id = r.id JOIN depths d ON d.id = x.depth_id"
            " WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ?"
            " LIMIT ?", (minlat, maxlat, minlon, maxlon, int(limit))).fetchall()
    finally:
        c.close()
    feats = [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[
            [r["min_lon"], r["min_lat"]], [r["max_lon"], r["min_lat"]],
            [r["max_lon"], r["max_lat"]], [r["min_lon"], r["max_lat"]], [r["min_lon"], r["min_lat"]]]]},
        "properties": {"cls": _depth_cls(r["rank"]), "label": r["label"] or ""},
    } for r in rows]
    return {"type": "FeatureCollection", "features": feats, "truncated": len(feats) >= limit}


@app.get("/map/", response_class=HTMLResponse)
def map_page(lat: float = None, lon: float = None, q: str = ""):
    if q.strip() and lat is None:
        try:
            g = geocode(q.strip())
            if g:
                lat, lon = g["lat"], g["lon"]
        except Exception:
            pass
    return HTMLResponse(_MAP_HTML
        .replace("__LAT__", str(lat if lat is not None else 34.7)) 
        .replace("__LON__", str(lon if lon is not None else 138.0))
        .replace("__ZOOM__", "15" if lat is not None else "11")
        .replace("__Q__", (q or "")[:100].replace('"', "&quot;"))
        .replace("__AUTO__", ("map.on('load',function(){judge(%r,%r)});" % (lat, lon))
                             if lat is not None else ""))


@app.get("/robots.txt", response_class=PlainTextResponse)
def _robots():
    return "User-agent: *\nAllow: /\n\nSitemap: https://kurage.exbridge.jp/ktsunami.php/sitemap.xml\n"


@app.get("/sitemap.xml")
def _sitemap():
    base = "https://kurage.exbridge.jp/ktsunami.php"
    urls = ["/", "/map/", "/area/"] + ["/area/" + a[0] for a in AREAS]
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + "".join(f'<url><loc>{base}{u}</loc><changefreq>monthly</changefreq></url>' for u in urls)
           + '</urlset>')
    return Response(content=xml, media_type="application/xml")


@app.get("/llms.txt", response_class=PlainTextResponse)
def _llms():
    return _LLMS_BODY
