"""取得した NetCDF の前処理とフロント計算。

- 変数の取り出しと単位の正規化（ケルビン → 摂氏）
- 水温フロント強度（Sobel 勾配、℃/km、cos(lat) 補正つき）
- bbox での切り出し（フロント計算はバッファ付きグリッドで行い、
  計算後に表示範囲へクリップする）

注意: PO.DAAC 直系の MUR は analysed_sst がケルビン、
CoastWatch ERDDAP (jplMURSST41) は摂氏に変換済み。
units 属性を見て判定し、決め打ちしない。
"""

from __future__ import annotations

import logging

import numpy as np
import rioxarray  # noqa: F401  (.rio アクセサ登録のため)
import xarray as xr
from scipy import ndimage

log = logging.getLogger(__name__)

_KELVIN_UNITS = {"kelvin", "k", "degree_kelvin", "degrees_kelvin"}
_CELSIUS_UNITS = {"degree_c", "degrees_c", "celsius", "degc", "degree_celsius"}

# 緯度1度・経度1度（赤道）の距離 (km)
KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON = 111.320


def _to_celsius(da: xr.DataArray) -> xr.DataArray:
    units = str(da.attrs.get("units", "")).strip().lower()
    if units in _KELVIN_UNITS:
        log.info("単位 %s → 摂氏に変換します", units)
        da = da - 273.15
    elif units in _CELSIUS_UNITS:
        log.info("単位は摂氏 (%s)。変換不要", units)
    else:
        # 単位属性が読めない場合は値域から推定する（290℃の海を防ぐ安全弁）
        median = float(np.nanmedian(da.values))
        if median > 200.0:
            log.warning("units 属性が不明 (%r) だが中央値 %.1f のためケルビンとみなす", units, median)
            da = da - 273.15
        else:
            log.warning("units 属性が不明 (%r)。摂氏とみなす（中央値 %.1f）", units, median)
    da = da.assign_attrs(units="degree_C")
    return da


def _normalize(da: xr.DataArray) -> xr.DataArray:
    """座標名を lat/lon に揃え、北が上（lat 降順）にし、CRS を付与する。"""
    rename = {}
    for src, dst in (("latitude", "lat"), ("longitude", "lon")):
        if src in da.dims:
            rename[src] = dst
    if rename:
        da = da.rename(rename)
    if "time" in da.dims:
        da = da.isel(time=-1, drop=True)
    da = da.sortby("lat", ascending=False)
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    da = da.rio.write_crs("EPSG:4326")
    return da.astype("float32")


def clip_bbox(da: xr.DataArray, bbox: dict) -> xr.DataArray:
    """表示用 bbox で切り出す（lat は降順前提）。"""
    out = da.sel(
        lat=slice(bbox["max_lat"], bbox["min_lat"]),
        lon=slice(bbox["min_lon"], bbox["max_lon"]),
    )
    if out.sizes["lat"] == 0 or out.sizes["lon"] == 0:
        raise ValueError("bbox で切り出した結果が空です。bbox と取得データの範囲を確認してください")
    # sel() で rio アクセサの空間次元情報が落ちることがあるため再付与する
    out = out.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    out = out.rio.write_crs("EPSG:4326")
    return out


def load_dataset(nc_path, cfg: dict) -> tuple[xr.DataArray, xr.DataArray | None]:
    """NetCDF から (SST, 推定誤差) を読む。

    どちらもバッファ付きの取得範囲のまま返す（クリップは呼び出し側で行う）。
    推定誤差の変数が無いファイルでは None を返す。
    """
    e = cfg["sst"]["erddap"]
    with xr.open_dataset(nc_path, mask_and_scale=True) as ds:
        if e["variable"] not in ds:
            raise KeyError(
                f"変数 {e['variable']} が見つかりません。存在する変数: {list(ds.data_vars)}"
            )
        sst = ds[e["variable"]].load()
        err_var = e.get("error_variable")
        err = ds[err_var].load() if err_var and err_var in ds else None
        an_var = e.get("anomaly_variable")
        anomaly = ds[an_var].load() if an_var and an_var in ds else None

    sst = _to_celsius(_normalize(sst))
    if err is not None:
        err = _normalize(err)  # 誤差は温度差なのでケルビン/摂氏どちらでも同じ値
        err = err.assign_attrs(units="degree_C")
    else:
        log.warning("推定誤差変数が無いため信頼度表示なしで続行します")

    # 平年偏差の要約（異常日の切り分け用 / 修正3-1）
    if anomaly is not None:
        av = _normalize(anomaly).values
        af = av[np.isfinite(av)]
        if af.size:
            log.info(
                "sst_anomaly: 平均 %+.2f℃ / 空間標準偏差 %.3f℃"
                "（偏差が一様に大きいならプロダクト側を疑う）",
                float(af.mean()), float(af.std()),
            )

    log.info(
        "読み込み完了: %d x %d px, SST %.2f〜%.2f ℃",
        sst.sizes["lon"], sst.sizes["lat"],
        float(sst.min(skipna=True)), float(sst.max(skipna=True)),
    )
    return sst, err


def spatial_std(da: xr.DataArray) -> float:
    """有効ピクセルの空間標準偏差。観測が乏しくならされた日は小さくなる。"""
    v = da.values[np.isfinite(da.values)]
    return float(v.std()) if v.size else 0.0


def load_gee_raster(tif_path, cfg: dict, *, kind: str = "sst") -> xr.DataArray:
    """GEE が出力した GeoTIFF（scale/offset 適用済み）を読む。

    kind='sst'  … ℃。kind='chla' … mg/m^3（対数表示のため0以下を除外）。
    バッファ付きの取得範囲のまま返す（クリップは呼び出し側）。
    """
    da = rioxarray.open_rasterio(tif_path, masked=True)
    if "band" in da.dims:
        da = da.isel(band=0, drop=True)
    da = da.rename({"y": "lat", "x": "lon"})
    da = da.where(np.isfinite(da))  # 極端値の欠測化保険
    da = _normalize(da)
    if kind == "chla":
        da = da.where(da > 0).assign_attrs(units="mg m-3")
        label, unit = "GCOM-C クロロフィル", "mg/m^3"
    else:
        da = da.assign_attrs(units="degree_C")
        label, unit = "GCOM-C SST", "℃"
    finite = da.values[np.isfinite(da.values)]
    if finite.size:
        log.info("%s 読み込み: %d x %d px, %.3f〜%.3f %s",
                 label, da.sizes["lon"], da.sizes["lat"],
                 float(finite.min()), float(finite.max()), unit)
    return da


def _read_var(nc_path, var: str) -> xr.DataArray:
    """NetCDF から 1 変数を読み、lat/lon 正規化・表層抽出したものを返す。"""
    with xr.open_dataset(nc_path, mask_and_scale=True) as ds:
        if var not in ds:
            raise KeyError(
                f"変数 {var} が見つかりません。存在する変数: {list(ds.data_vars)}"
            )
        da = ds[var].load()
    for dim in ("depth", "elevation"):
        if dim in da.dims:
            da = da.isel({dim: 0}, drop=True)
    return _normalize(da)


def load_chla(nc_path, cfg: dict, layer_key: str = "chla") -> xr.DataArray:
    """Copernicus Marine の NetCDF からクロロフィルを読む。

    バッファ付きの取得範囲のまま返す（クリップは呼び出し側）。
    値は mg/m^3。負値や 0 以下は対数表示で扱えないため NaN にする。
    """
    var = cfg[layer_key]["variable"]
    chla = _read_var(nc_path, var)
    chla = chla.where(chla > 0)  # 対数表示のため 0 以下を除外
    chla = chla.assign_attrs(units="mg m-3")

    finite = chla.values[np.isfinite(chla.values)]
    if finite.size:
        log.info(
            "%s 読み込み: %d x %d px, %.3f〜%.3f mg/m^3",
            layer_key, chla.sizes["lon"], chla.sizes["lat"],
            float(finite.min()), float(finite.max()),
        )
    return chla


def load_chla_uncertainty(nc_path, cfg: dict) -> xr.DataArray | None:
    """広域L4の推定誤差 CHL_uncertainty を読む。無ければ None。

    SST の analysis_error と同じく、値が大きいピクセルを半透明化するのに使う。
    校正のため値域をログに出す（閾値は実データを見てから決める）。
    """
    var = cfg["chla"].get("uncertainty_variable")
    if not var:
        return None
    try:
        unc = _read_var(nc_path, var)
    except KeyError:
        log.warning("%s が無いためクロロフィルの信頼度表示はスキップします", var)
        return None
    finite = unc.values[np.isfinite(unc.values)]
    if finite.size:
        log.info(
            "CHL_uncertainty 値域: min=%.4f p50=%.4f p90=%.4f max=%.4f（閾値校正の参考）",
            float(finite.min()), float(np.percentile(finite, 50)),
            float(np.percentile(finite, 90)), float(finite.max()),
        )
    return unc


def log_flags_summary(nc_path, cfg: dict) -> None:
    """flags 変数の中身を確認用にログへ要約出力する（補間/実測ビットの手がかり）。"""
    var = cfg["chla"].get("flags_variable")
    if not var:
        return
    try:
        flags = _read_var(nc_path, var)
    except KeyError:
        return
    vals = flags.values[np.isfinite(flags.values)]
    if vals.size:
        uniq = np.unique(vals.astype("int64"))
        head = ", ".join(str(int(u)) for u in uniq[:12])
        log.info("flags のユニーク値（先頭12件 / 全%d種）: %s", uniq.size, head)


def compute_chla_gradient(chla: xr.DataArray) -> xr.DataArray:
    """広域(gap-free 4km)クロロフィルの勾配強度 (mg/m^3/km) を計算する。

    穴のない広域データに対して SST フロントと同じ Sobel 法を使う。
    L3(300m)は雲で穴だらけのため勾配計算に使わない（大穴を埋めると虚構になる）。
    """
    # 雲穴の多い光学データなので、有効近傍が5px以上ある所だけ勾配を出す
    grad = compute_front(chla, min_valid_neighbors=5)
    grad.attrs.update(units="mg m-3 km-1", long_name="chlorophyll gradient")
    grad = grad.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    return grad.rio.write_crs("EPSG:4326")


def measured_mask_on(broad: xr.DataArray, hires: xr.DataArray) -> np.ndarray:
    """広域(L4)グリッドの各セルが高解像度(L3)で実測されているかの真偽配列。

    L3 は雲があると欠測するので「L3 に値がある = その日実測された」。
    L4 gap-free はその欠測を補間で埋めている。各 L4 セルの範囲に
    有効な L3 ピクセルが1つでもあれば実測(True)、無ければ補間(False)とみなす。

    戻り値は broad と同じ形状の bool 配列（行0=北端）。
    """
    hlat = hires["lat"].values
    hlon = hires["lon"].values
    hvalid = np.isfinite(hires.values)
    blat = broad["lat"].values
    blon = broad["lon"].values

    # L4 セル境界（緯度は降順前提）
    dlat = abs(float(blat[1] - blat[0])) / 2 if blat.size > 1 else 0.02
    dlon = abs(float(blon[1] - blon[0])) / 2 if blon.size > 1 else 0.02

    # 各 L3 ピクセルがどの L4 セルに入るかを索引化して集計
    measured = np.zeros((blat.size, blon.size), dtype=bool)
    lat_edges = np.concatenate(([blat[0] + dlat], blat - dlat))  # 降順の外縁
    lon_edges = np.concatenate(([blon[0] - dlon], blon + dlon))  # 昇順の外縁
    # 緯度は降順なので反転して digitize する
    row_idx = np.searchsorted(-lat_edges, -hlat) - 1
    col_idx = np.searchsorted(lon_edges, hlon) - 1

    for r_h in range(hlat.size):
        rr = row_idx[r_h]
        if rr < 0 or rr >= blat.size:
            continue
        valid_row = hvalid[r_h]
        cols = col_idx[valid_row]
        cols = cols[(cols >= 0) & (cols < blon.size)]
        if cols.size:
            measured[rr, cols] = True
    return measured


def compute_front(sst: xr.DataArray, min_valid_neighbors: int | None = None) -> xr.DataArray:
    """水温フロント強度 (℃/km) を計算する。

    - Sobel フィルタで x/y 勾配を推定（カーネル和のスケール 8 で割って ℃/px に）
    - ピクセル実距離で割って ℃/km に正規化
    - 経度方向のピクセル幅は cos(lat) で緯度ごとに補正する
    - バッファ付きグリッドに対して呼び、結果を clip_bbox すること

    min_valid_neighbors を指定すると、3x3 近傍の有効ピクセルがその数未満の
    ピクセルの勾配を捨てる。雲で穴だらけの光学データ（GCOM-C クロロフィル）で
    大穴を最近傍で埋めて偽の勾配を描くのを防ぐ（陸1pxの海=通常8近傍なので残る）。
    """
    data = sst.values.astype("float64")
    valid = np.isfinite(data)

    # 陸(NaN)を最近傍の海面値で埋めてから微分する。
    # そのまま Sobel をかけると 3x3 カーネルが NaN を拾い、岸から1pxの
    # 海ピクセルまで NaN に汚染される（実測で海の9.4%、別府湾域は14.7%が
    # 欠損した）。潮汐フロントは湾口・海峡などの沿岸にこそ立つため、
    # ここを落とすと一番見たい場所が消える。
    # 最近傍埋めは岸沿いの勾配をやや過小評価する点に注意（NaN全落ちよりマシ）。
    if valid.any() and not valid.all():
        idx = ndimage.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        data = data[tuple(idx)]

    lat = sst["lat"].values
    lon = sst["lon"].values

    dlat = float(abs(lat[1] - lat[0]))
    dlon = float(abs(lon[1] - lon[0]))
    dy_km = dlat * KM_PER_DEG_LAT
    # 行（緯度）ごとの経度方向ピクセル幅 (km)
    dx_km = dlon * KM_PER_DEG_LON * np.cos(np.deg2rad(lat))[:, np.newaxis]

    # Sobel は [1,2,1]（平滑, 和4）×[-1,0,1]（中央差分, 間隔2px）なので 8 で割ると ℃/px
    gx = ndimage.sobel(data, axis=1, mode="nearest") / 8.0 / dx_km
    gy = ndimage.sobel(data, axis=0, mode="nearest") / 8.0 / dy_km
    strength = np.hypot(gx, gy).astype("float32")

    # 陸のみ再マスクする（海ピクセルは岸沿いも含めて全て残る）
    strength[~valid] = np.nan

    # 雲の大穴対策: 3x3 の有効近傍が乏しいピクセルの勾配は捨てる
    if min_valid_neighbors is not None:
        vcount = ndimage.uniform_filter(
            valid.astype("float64"), size=3, mode="constant"
        ) * 9.0
        strength[vcount < float(min_valid_neighbors)] = np.nan

    front = xr.DataArray(
        strength,
        dims=("lat", "lon"),
        coords={"lat": sst["lat"], "lon": sst["lon"]},
        attrs={"units": "degC/km", "long_name": "SST front strength"},
    )
    front = front.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    front = front.rio.write_crs("EPSG:4326")
    log.info(
        "フロント強度: 最大 %.3f ℃/km, p98 %.3f ℃/km",
        float(np.nanmax(strength)), float(np.nanpercentile(strength, 98)),
    )
    return front


def check_coverage(source: xr.DataArray, derived: xr.DataArray, name: str) -> int:
    """派生レイヤの有効ピクセル数を入力と突き合わせる（NaN汚染の再発防止）。

    「入力に値があるのに出力が NaN」のピクセル数を返す。0 が正常。
    CI ログに毎回出し、閾値超過は WARNING で目立たせる。
    """
    src_ok = np.isfinite(source.values)
    lost = int(np.sum(src_ok & ~np.isfinite(derived.values)))
    total = int(src_ok.sum())
    if lost > 0:
        log.warning(
            "%s: 入力に値がある %d ピクセル中 %d (%.1f%%) が出力で欠損しています。"
            "カーネル処理による NaN 伝播を疑ってください",
            name, total, lost, lost / total * 100,
        )
    else:
        log.info("%s: 有効ピクセル網羅性 OK (%d/%d)", name, total - lost, total)
    return lost
