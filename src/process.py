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

    sst = _to_celsius(_normalize(sst))
    if err is not None:
        err = _normalize(err)  # 誤差は温度差なのでケルビン/摂氏どちらでも同じ値
        err = err.assign_attrs(units="degree_C")
    else:
        log.warning("推定誤差変数が無いため信頼度表示なしで続行します")

    log.info(
        "読み込み完了: %d x %d px, SST %.2f〜%.2f ℃",
        sst.sizes["lon"], sst.sizes["lat"],
        float(sst.min(skipna=True)), float(sst.max(skipna=True)),
    )
    return sst, err


def load_chla(nc_path, cfg: dict) -> xr.DataArray:
    """Copernicus Marine の NetCDF からクロロフィルを読む。

    バッファ付きの取得範囲のまま返す（クリップは呼び出し側）。
    値は mg/m^3。負値や 0 以下は対数表示で扱えないため NaN にする。
    """
    var = cfg["chla"]["variable"]
    with xr.open_dataset(nc_path, mask_and_scale=True) as ds:
        if var not in ds:
            raise KeyError(
                f"変数 {var} が見つかりません。存在する変数: {list(ds.data_vars)}"
            )
        chla = ds[var].load()

    # 深さ次元があれば表層を取る
    for dim in ("depth", "elevation"):
        if dim in chla.dims:
            chla = chla.isel({dim: 0}, drop=True)
    chla = _normalize(chla)
    chla = chla.where(chla > 0)  # 対数表示のため 0 以下を除外
    chla = chla.assign_attrs(units="mg m-3")

    finite = chla.values[np.isfinite(chla.values)]
    if finite.size:
        log.info(
            "クロロフィル読み込み: %d x %d px, %.3f〜%.3f mg/m^3",
            chla.sizes["lon"], chla.sizes["lat"], float(finite.min()), float(finite.max()),
        )
    return chla


def compute_front(sst: xr.DataArray) -> xr.DataArray:
    """水温フロント強度 (℃/km) を計算する。

    - Sobel フィルタで x/y 勾配を推定（カーネル和のスケール 8 で割って ℃/px に）
    - ピクセル実距離で割って ℃/km に正規化
    - 経度方向のピクセル幅は cos(lat) で緯度ごとに補正する
    - バッファ付きグリッドに対して呼び、結果を clip_bbox すること
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
