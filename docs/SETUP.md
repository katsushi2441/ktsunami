# 設置手順書 — Kurage 津波浸水想定マップ (ktsunami)

PostGIS も Docker も要りません。Python と GDAL だけで動きます。

## 1. 動作条件

- Linux (Ubuntu 22.04 で動作確認) / Python 3.10以上
- GDAL (`ogr2ogr`) — `sudo apt install gdal-bin`
- ディスク: 取り込み時 約1.5GB（生zip）+ DB 約300MB
- メモリ 1GB以上

## 2. 展開と依存

```bash
unzip ktsunami.zip -d /opt/ktsunami
cd /opt/ktsunami
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" requests
```

## 3. データの取得と取り込み

国土数値情報から直接ダウンロードして取り込みます。**再配布物にデータは含まれていません**
（最新版を使っていただくためです）。

```bash
.venv/bin/python scripts/load_a40.py --fresh
```

- 沿岸のある都道府県を順に取得します（全国で30〜60分。生zipは `data/raw/` に残ります）
- **整備年度は県ごとに違います。** スクリプトが新しい年度から自動で探し、
  見つかった年度をその県のデータ時点として記録します
- 特定の県だけなら `--pref 23` のように指定できます（23=愛知）

成功すると県ごとの件数と時点が出ます:

```
  23 愛知県      129834 セル  時点 2023-03-31
  22 静岡県      190417 セル  時点 2017-03-31
  合計 3795197 セル / 35 都道府県
```

## 4. 起動

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 18380
```

常駐は `systemd/ktsunami.service` のパスを書き換えて設置します。

```bash
cp systemd/ktsunami.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ktsunami
```

`Restart=always` を入れてあります。

## 5. 動作確認

```bash
curl 'http://127.0.0.1:18380/healthz'
# {"status":"ok","cells":3795197,"prefs":35}

curl -G 'http://127.0.0.1:18380/api/check' --data-urlencode 'q=宮城県石巻市中央'
```

## 6. レンタルサーバーから公開する (任意)

`php/ktsunami.php` をレンタルサーバーに置き、同じディレクトリに `ktsunami_config.php` を作ります。

```php
<?php define('KTSUNAMI_BACKEND', 'http://あなたのサーバー:18380');
```

`https://example.com/ktsunami.php/`（末尾スラッシュ必須）で開きます。

## 7. データの更新

都道府県の公表・更新は不定期です。年1回程度 `--fresh` で再取り込みしてください。

## 収録されない地域について（重要）

- **東京都・福井県・香川県**は、沿岸があるのに国土数値情報のA40には登録がありません
  （2026-09-06 時点の実測）。本システムは「未収録」と明示して返します
- 内陸8県（栃木・群馬・埼玉・山梨・長野・岐阜・滋賀・奈良）は津波浸水想定そのものが
  作成されていません。「想定不要」と返します
- この区別を画面から消さないでください。「区域外」と「データが無い」の混同は
  安全だという誤解を生みます

## トラブル

| 症状 | 原因と対処 |
| --- | --- |
| 取り込みが「配布なし」ばかり | 通信失敗の可能性。時間を置いて再実行（取得済みzipは再利用されます） |
| `ogr2ogr: not found` | `sudo apt install gdal-bin` |
| 住所が見つからない | 国土地理院の地名検索APIの結果に依存します。町丁目まで入れてください |
