# 沿岸漁場支援マップ 開発計画書

杵築市沖（別府湾・伊予灘）を対象とした、衛星海面水温・クロロフィルの日次自動更新マップ。

参考事例: 宙畑「沿岸漁業で衛星データ活用チャレンジ！ ~大分のサワラ流し網漁編~」<https://sorabatake.jp/16769/>

---

## 0. このドキュメントの読み方（Claude Code 向け）

- 本書は仕様であって完成コードではない。着手前に「1. ゴール」と「6. フェーズ」を必ず読むこと。
- **フェーズ1が完走するまで、フェーズ2以降のコードを書かないこと。** MUR SST だけで「取得 → COG → GitHub Pages → 表示」の一本の筋を通すのが最優先。
- 外部API（NASA Earthdata、Copernicus Marine、JAXA G-Portal）の仕様は変わりうる。**記憶で書かず、必ず公式ドキュメントを確認してから実装すること。** 本書に書かれたパラメータ名・コレクション名も検証対象とみなす。
- 認証情報は絶対にコミットしない。`.env` はローカル、CI では GitHub Secrets。

---

## 1. ゴール

### 最終形

1. 毎日決まった時刻に GitHub Actions が起動し、衛星データを自動取得する
2. 対象海域だけを切り出し、COG（Cloud Optimized GeoTIFF）に変換する
3. 成果物を GitHub Pages に公開する
4. **Webマップ**（スマホで閲覧）と **QGIS**（PCで詳細分析）が、同一のCOGを参照する

### 成功の定義

- Webマップを開くと、当日または前日の海面水温分布が色分けされて見える
- QGIS でレイヤ追加 → HTTP → COG の URL を貼るだけで、同じデータが開ける
- 数日放置しても、人の手を介さず最新データに更新されている

---

## 2. 対象海域

| 項目 | 値 |
|---|---|
| 西端（min lon） | 131.4 |
| 東端（max lon） | 132.4 |
| 南端（min lat） | 33.2 |
| 北端（max lat） | 33.9 |
| CRS | EPSG:4326 |

守江湾・別府湾・伊予灘南部・国東半島沖・姫島周辺をカバーする。
このbboxは `config.yaml` に外出しし、ハードコードしないこと。

参考: MUR（1km）でおよそ 110 × 80 ピクセル。1ファイル数十KB程度。GitHub Pages の容量制限（リポジトリ推奨1GB、単一ファイル100MB）に対して十分小さい。

---

## 3. データソース

### 3-1. MUR SST（主軸・フェーズ1）

- 提供元: NASA JPL PO.DAAC
- コレクション名: `MUR-JPL-L4-GLOB-v4.1`
- 解像度: 1km / 日次 / **L4解析値のため雲による欠測がない**
- 取得方法: **NOAA CoastWatch ERDDAP（データセット `jplMURSST41`）の griddap から bbox 指定で部分取得**
  （当初案の `podaac-data-downloader` は `-b` がファイル絞り込みにしか効かず、MUR は全球1ファイル＝毎日数百MB
  になるため変更した。ERDDAP はサーバー側切り出しができ1日数十KBで済む。詳細は README 参照）
- 認証: **不要**（ERDDAP 経由のため。PO.DAAC 直接取得に切り替える場合のみ NASA Earthdata Login が必要）
- 主要変数: `analysed_sst`（PO.DAAC 直系は**ケルビン**、ERDDAP 配信版は**摂氏**。
  `process.py` は units 属性で自動判定する）、`analysis_error`（推定誤差。沿岸ピクセルの信頼度表示に使用）

**採用理由**: 雲があっても穴が空かない。日次自動更新の土台にできる唯一の選択肢。

**注意**: MUR は夜間観測を主体にした foundation SST。日中の表層加熱を含まないため、船の水温計の実測値とは数度ずれうる。**絶対値ではなく空間分布・勾配を見る道具**として位置づける。この旨は Web マップ上にも注記を出すこと。

### 3-2. GCOM-C / SGLI（高解像度・フェーズ3）

- 提供元: JAXA G-Portal
- プロダクト: SGLI L2 沿岸域 海面水温（250m）、クロロフィルa濃度（250m）
- 認証: G-Portal アカウント（SFTP でのダウンロードが可能）
- 形式: HDF5

**採用理由**: 別府湾・守江湾のような狭い湾では 1km だと数ピクセルしかない。地形に沿った水温構造を見るには 250m が要る。

**難所（ここが一番ハマる）**:
- 光学センサのため雲があると欠測する。「晴れた日のご褒美レイヤ」と割り切る
- L2 プロダクトは衛星軌道に沿ったグリッド（非等間隔）で、緯度経度が別データセットとして格納されている。**GeoTIFF にするには再投影（リサンプリング）が必要**。`pyresample` か GDAL の `-geoloc` オプションを使う
- QGIS は HDF5 を直接扱えないことが多い。前処理でGeoTIFF化するのが前提

### 3-3. Copernicus Marine 海色（クロロフィル自動更新・フェーズ2）

- 提供元: Copernicus Marine Service（CMEMS）
- プロダクト: Sentinel-3 OLCI 由来の Ocean Colour（クロロフィルa）。ギャップフィル済みのL4があればそれを優先
- 取得方法: `copernicusmarine` Python toolkit（`copernicusmarine subset` で bbox 指定の部分取得が可能）
- 認証: Copernicus Marine アカウント（無料）

**採用理由**: GCOM-C のクロロフィルは雲で欠測し、自動化も面倒。日次で必ず絵が出るクロロフィル層として、こちらを自動更新の主軸にする。

---

## 4. 派生レイヤ：水温フロント強度

**これが本プロジェクトの肝。** サワラのような回遊魚は、水温そのものより暖水と冷水の**境目（フロント）**に集まる傾向がある。豊後水道・速吸瀬戸は潮流が強く、フロントが立ちやすい海域。

### 計算方法

SST ラスタに Sobel フィルタをかけ、勾配の大きさを求める。

```
gx = sobel(sst, axis=1)
gy = sobel(sst, axis=0)
front_strength = sqrt(gx**2 + gy**2)   # 単位: ℃/pixel → ℃/km に換算する
```

`scipy.ndimage.sobel` で数行。ピクセルサイズで割って **℃/km** に正規化すること（緯度によって経度方向のピクセル幅が変わる点に注意）。

### 出力

- `front.tif` … フロント強度ラスタ
- `front_contour.geojson` … 閾値（例: 0.15 ℃/km）以上の等値線。Webマップ上に線として重ねる

**クロロフィルの濃淡境界と水温フロントが重なる場所が、好漁場の候補**という仮説を可視化できる状態を目指す。

---

## 5. リポジトリ構成（案）

```
coastal-fishery-map/
├── README.md
├── PLAN.md                     # 本書
├── config.yaml                 # bbox、閾値、カラーレンジなど全パラメータ
├── requirements.txt
├── .env.example                # 認証情報のキー名のみ（値は入れない）
├── src/
│   ├── fetch_mur.py            # フェーズ1
│   ├── fetch_cmems.py          # フェーズ2
│   ├── fetch_gcomc.py          # フェーズ3
│   ├── process.py              # 切り出し・単位変換・フロント計算
│   ├── export.py               # COG / PNG / GeoJSON 出力
│   └── main.py                 # エントリポイント
├── .github/workflows/
│   └── daily.yml               # cron
├── docs/                       # ← GitHub Pages の公開ディレクトリ
│   ├── index.html              # MapLibre のWebマップ
│   ├── data/
│   │   ├── latest/
│   │   │   ├── sst.tif
│   │   │   ├── chla.tif
│   │   │   ├── front.tif
│   │   │   ├── front_contour.geojson
│   │   │   └── meta.json       # 観測日時、データソース、値域
│   │   └── archive/YYYY-MM-DD/ # 過去分
└── qgis/
    └── coastal_fishery.qgz     # COGのURLを直接参照するQGISプロジェクト
```

### 重要な設計判断

- **`latest/` は常に同じファイル名で上書きする。** これにより QGIS プロジェクトと Web マップの参照 URL が固定される。日付入りファイル名は `archive/` 側だけ。
- `meta.json` に観測日時とデータソースを必ず書く。Web マップに「いつのデータか」を表示するため。データが古いまま更新が止まったことに気づけないのが一番危険。

---

## 6. フェーズ

### フェーズ1: MUR SST の一本通し（最優先）

**これが終わるまで他に手を出さない。**

1. NASA Earthdata アカウントを作成し、`.netrc` またはトークンでローカル認証を通す
2. `fetch_mur.py`: 直近日の MUR を bbox 指定でダウンロード（NetCDF）
3. `process.py`: `analysed_sst` を摂氏に変換、bbox で切り出し
4. `export.py`: `rioxarray` の `.rio.to_raster(driver="COG")` で `sst.tif` を出力。あわせてカラーマップ適用済み PNG と `meta.json` も
5. `docs/index.html`: MapLibre GL JS で、海図または OSM ベースマップの上に PNG を重ねる。凡例と観測日時を表示
6. GitHub Pages を `docs/` ディレクトリから公開する設定にする
7. `.github/workflows/daily.yml`: cron（例 `0 22 * * *` = JST 07:00）で実行し、`docs/` の差分をコミット・プッシュ

**フェーズ1の完了条件**
- [ ] Web マップを開くと直近の水温分布が見える
- [ ] QGIS で `https://<user>.github.io/<repo>/data/latest/sst.tif` をラスタレイヤとして開ける
- [ ] Actions が2日連続で自動成功している

### フェーズ2: クロロフィル + フロント  ← 実装済み

1. [x] `process.py` にフロント強度計算（第4節）を追加（Sobel, ℃/km, cos(lat)補正,
   バッファ取得で端の破綻を回避。岸沿い NaN 伝播も distance_transform で修正済み）
2. [x] `fetch_cmems.py` で Copernicus Marine のクロロフィルを追加
   （gap-free L4 `cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D`。
   認証は CMEMS_USERNAME/PASSWORD。取得失敗でも SST/フロントは継続する）
3. [x] Web マップにレイヤ切り替え UI（水温 / フロント / クロロフィル）を追加
   （色付けはブラウザ側 Canvas。クロロフィルは対数スケール、観測日はレイヤ独立）
4. [ ] フロント等値線を GeoJSON で重ねる（未実装。当面はラスタ表示で運用）

### フェーズ3: GCOM-C 250m

1. G-Portal からの SFTP ダウンロード
2. HDF5 の緯度経度グリッド → 等間隔グリッドへの再投影（`pyresample`）
3. 雲で欠測した日は「データなし」を明示し、**古いデータを最新に見せかけないこと**
4. Web マップでは「高解像度（晴天時のみ）」として別レイヤ扱い

### フェーズ4: QGIS プロジェクト整備

1. `.qgz` に COG の HTTP レイヤを登録
2. 疑似カラー（Singleband pseudocolor）のカラーランプと値域を保存
3. 「任意の温度範囲だけ表示」を再現するため、値域クランプを設定
4. 過去の漁獲ポイント CSV → ポイントレイヤのテンプレートも同梱

---

## 7. 技術スタック

| 用途 | ライブラリ |
|---|---|
| NetCDF/HDF5 読み込み | `xarray`, `netCDF4`, `h5py` |
| 地理参照・COG出力 | `rioxarray`, `rasterio` |
| 再投影（GCOM-C L2） | `pyresample` または GDAL |
| フロント計算 | `scipy.ndimage` |
| MUR取得 | `podaac-data-downloader` |
| CMEMS取得 | `copernicusmarine` |
| Webマップ | MapLibre GL JS（CDN、ビルド不要） |
| CI | GitHub Actions |

**Web フロントはビルドツールを入れないこと。** 単一の `index.html` + CDN で完結させる。保守コストを最小にする。

---

## 8. 認証情報

`.env`（ローカル）と GitHub Secrets（CI）に同名で設定する。

```
EARTHDATA_USERNAME=   # フェーズ1では不要（ERDDAP 経由のため）。PO.DAAC 直接取得に切り替える場合のみ
EARTHDATA_PASSWORD=
CMEMS_USERNAME=       # フェーズ2（クロロフィル）で必要
CMEMS_PASSWORD=
GPORTAL_USERNAME=     # フェーズ3（GCOM-C）で必要
GPORTAL_PASSWORD=
```

- `.env` は `.gitignore` に必ず入れる
- ワークフロー内で `echo` や `set -x` によって値が出力されないよう注意する
- パブリックリポジトリでも Secrets はログにマスクされるが、意図的な出力は防げない

---

## 9. 既知の落とし穴

1. **単位**: PO.DAAC 直系の MUR は `analysed_sst` がケルビン。摂氏変換を忘れると 290℃ の海になる。
   ※現在使用中の ERDDAP（`jplMURSST41`）は摂氏配信のため変換不要だが、`process.py` の units 自動判定は
   取得先を切り替えた際の保険として残している。
2. **タイムラグ**: MUR は観測から公開まで概ね1日程度かかる。「今日のデータ」を要求して404にならないよう、直近N日を遡って取得できた最新日を採用するリトライロジックを入れる。
3. **フロント強度の単位**: ピクセル単位のままだと解像度の違うデータ間で比較できない。必ず ℃/km に正規化する。
4. **緯度による歪み**: EPSG:4326 のままだと経度方向の実距離が緯度で変わる。フロント計算では `cos(lat)` 補正を入れるか、UTM（Zone 52N）に再投影してから計算する。
5. **リポジトリ肥大化**: `archive/` を無制限に貯めるとリポジトリが膨らむ。保持期間（例: 90日）を決め、古いものを削除する処理を入れる。
6. **大分県「海況・魚群速報」は機械可読でない**: PDF/画像で公開されており、自動取り込みは現実的でない。Web マップ上に参考リンクを置く割り切りとする。<https://www.pref.oita.jp/soshiki/15090/beppusokuhou.html>
7. **Actions のコミットループ**: ワークフロー自身のコミットが再びワークフローを起動しないよう、`paths-ignore` や `[skip ci]` で制御する。

---

## 10. 免責・位置づけ

本ツールは漁場を保証するものではない。あくまで「あたりをつける」ための参考情報であり、出航判断は利用者の責任で行うものとする。この旨を Web マップにも明記すること。
