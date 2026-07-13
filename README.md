# 杵築市沖 沿岸漁場支援マップ

杵築市沖（別府湾・伊予灘）を対象とした、衛星海面水温（SST）の日次自動更新マップ。
毎朝 GitHub Actions が衛星データを取得し、GitHub Pages 上の Web マップと QGIS 用 COG を更新します。

仕様の詳細は [PLAN.md](PLAN.md) を参照。現在は**フェーズ1（MUR SST の一本通し）**まで実装済みです。

## 仕組み

```
GitHub Actions (毎日 JST 07:00)
  └─ src/main.py
       ├─ fetch_mur.py   MUR SST v4.1 を bbox 指定で取得（NetCDF）
       ├─ process.py     摂氏変換・切り出し・地理参照
       └─ export.py      COG / PNG / meta.json を docs/data/ へ出力
            ├─ latest/   常に同名で上書き（Web・QGIS の参照URLを固定）
            └─ archive/YYYY-MM-DD/  過去分（90日で自動削除）
GitHub Pages (docs/)
  ├─ index.html          MapLibre の Web マップ（スマホ対応）
  └─ data/latest/sst.tif QGIS から直接開ける COG
```

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

認証情報は不要です（フェーズ1 は ERDDAP 経由のため。`.env.example` 参照）。

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
- [ ] フェーズ2: クロロフィル（Copernicus Marine）+ 水温フロント強度レイヤ
- [ ] フェーズ3: GCOM-C/SGLI 250m 高解像度レイヤ（晴天時のみ）
- [ ] フェーズ4: QGIS プロジェクト（.qgz）の整備
