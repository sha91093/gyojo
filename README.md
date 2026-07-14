# 杵築市沖 沿岸漁場支援マップ

杵築市沖（別府湾・伊予灘）を対象とした、衛星海面水温（SST）と水温フロント強度の日次自動更新マップ。
毎朝 GitHub Actions が衛星データを取得し、GitHub Pages 上の Web マップと QGIS 用 COG を更新します。

仕様の詳細は [PLAN.md](PLAN.md) を参照。フェーズ1（MUR SST の一本通し）と
フェーズ2の水温フロント強度レイヤまで実装済みです。

## 仕組み

```
GitHub Actions (毎日 JST 07:00)
  └─ src/main.py
       ├─ fetch_mur.py   MUR SST v4.1 + 推定誤差を bbox+バッファで取得（NetCDF）
       ├─ process.py     摂氏変換・フロント強度計算（Sobel, ℃/km）・切り出し
       └─ export.py      COG / 値配列JSON / meta.json を docs/data/ へ出力
            ├─ latest/   常に同名で上書き（Web・QGIS の参照URLを固定）
            └─ archive/YYYY-MM-DD/  過去分（90日で自動削除）
       └─ fetch_cmems.py  クロロフィル gap-free L4 を取得（任意・認証必要）
GitHub Pages (docs/)
  ├─ index.html            MapLibre の Web マップ（スマホ対応）
  └─ data/latest/sst.tif   QGIS から直接開ける COG（front.tif / chla.tif も同様）
```

### Web マップの主な機能

- **水温・フロント・クロロフィル（広域/高解像度/勾配）のレイヤ**をワンタップで切替
- **色付けはブラウザ側の Canvas で実施**（サーバーは物理値の JSON を配るだけ）。
  水温は「フロントを探す」（その日の水温幅でコントラスト最大化）と
  「変化を追う」（季節固定レンジ。日をまたいだ比較用）を切替でき、
  クロロフィルは対数スケールで表示する
- **海面をタップするとその地点の水温・フロント強度・クロロフィル濃度・推定誤差を数値表示**
- クロロフィルは**二層構成**（SST と同じ思想）:
  - **広域**（gap-free L4 4km）: 毎日必ず絵が出る既定表示
  - **高解像度**（L3 OLCI 300m）: 湾内構造が見える「晴天時のご褒美レイヤ」。雲域は欠測
  - 高解像度(L3)の被覆から、広域(L4)の**雲を補間で埋めた箇所を半透明表示**し実測カバー率を凡例に出す
- レイヤごとに観測日を保持し、**水温と別日のレイヤを重ねるときは警告表示**（古いデータを最新に見せない）
- `analysis_error` が閾値（既定 0.5℃）を超えるピクセルは**半透明にして信頼度の低さを可視化**
  （主に沿岸・湾内。マイクロ波放射計の陸地汚染対策）
- 観測日が3日以上古いままの場合は**画面上部に赤い警告バナー**を表示
- ◀ ▶ ボタンで過去データ（90日分）を閲覧可能

## 初回セットアップ

1. **GitHub Pages を有効化**
   リポジトリの Settings → Pages → 「Deploy from a branch」で
   公開したいブランチと `/docs` フォルダを選択する。
2. **初回のデータ生成**
   Actions タブ → `daily-update` → 「Run workflow」で手動実行する。
   成功すると `docs/data/latest/` にデータがコミットされる。
   以後は毎日 JST 07:00 に自動実行される。
3. **確認**
   - Web マップ: `https://<ユーザー名>.github.io/<リポジトリ名>/`
   - COG: `https://<ユーザー名>.github.io/<リポジトリ名>/data/latest/sst.tif`

SST/フロントに認証は不要です（ERDDAP 経由）。**クロロフィルのみ Copernicus Marine の
無料アカウントが必要**で、GitHub Secrets に `CMEMS_USERNAME` / `CMEMS_PASSWORD` を
設定します（未設定でも SST/フロントの更新は止まりません）。`.env.example` 参照。

## ローカル実行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main            # docs/data/ に出力される
python -m http.server -d docs # http://localhost:8000 でWebマップ確認
```

## QGIS での利用（PC で詳細分析）

1. レイヤ → レイヤを追加 → ラスタレイヤを追加
2. ソースタイプに「プロトコル: HTTP(S), cloud など」を選択
3. URI に `https://<ユーザー名>.github.io/<リポジトリ名>/data/latest/sst.tif` を貼る
4. シンボロジを「単バンド疑似カラー」にし、好みの温度範囲で色分けする

URL は常に固定なので、一度プロジェクトを作れば開くたびに最新データになります。

## データソースと取得経路について

- データ本体: **MUR SST v4.1**（NASA JPL PO.DAAC, `MUR-JPL-L4-GLOB-v4.1`）。
  1km / 日次 / L4 解析値のため雲による欠測がない。
- 取得経路: PLAN.md では `podaac-data-downloader` を想定していたが、
  同ツールの `-b`（bbox）は**ダウンロード対象ファイルの絞り込みにしか効かず**、
  MUR は全球1ファイルのため毎日数百MBの取得になる。
  そのため、同一データを再配信しており**サーバー側で bbox 切り出しができ認証も不要**な
  NOAA CoastWatch ERDDAP（データセット `jplMURSST41`）を既定の取得経路とした
  （1日あたり数十KB）。取得先は `config.yaml` で変更できる。
- ERDDAP 配信版の `analysed_sst` は摂氏に変換済み。PO.DAAC 直系はケルビン。
  `process.py` は units 属性を見て自動判定する。

## 注意事項

- MUR は夜間観測主体の foundation SST のため、日中の実測水温と数度ずれることがある。
  **絶対値ではなく空間分布・勾配（フロント）を見る道具**として使うこと。
- 本ツールは漁場を保証するものではない。出航判断は利用者の責任で行うこと。
- 参考リンク: [大分県 海況・魚群速報](https://www.pref.oita.jp/site/nourinsuisan/beppusokuhou.html)（PDF/画像のため自動取り込みはしない）

## 今後のフェーズ

- [x] フェーズ1: MUR SST の一本通し（取得 → COG → Pages → Web/QGIS 表示）
- [x] フェーズ2a: 水温フロント強度レイヤ（Sobel 勾配, ℃/km, cos(lat) 補正, バッファ取得で端の破綻を回避）
- [x] フェーズ2b: クロロフィル二層構成（広域 L4 gap-free + 高解像度 L3 OLCI 300m、
  補間箇所の半透明化・実測カバー率・観測日ずれ警告）
- [ ] 課題4（本命）: フロントとクロロフィル勾配の重ね合わせ・好漁場候補スコア
- [ ] フェーズ3: GCOM-C/SGLI 250m 高解像度レイヤ（晴天時のみ）
- [ ] フェーズ4: QGIS プロジェクト（.qgz）の整備
- [ ] 前日比の差分レイヤ・時系列アニメーション（アーカイブが数日分たまってから）
