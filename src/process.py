"""取得した NetCDF の前処理（フェーズ1）。

- 変数の取り出しと単位の正規化（ケルビン → 摂氏）
- bbox での切り出し
- rioxarray 用の地理参照付与

注意: PO.DAAC 直系の MUR は analysed_sst がケルビン、
CoastWatch ERDDAP (jplMURSST41) は摂氏に変換済み。
units 属性を見て判定し、決め打ちしない。
"""

from __future__ import annotations

import logging

import numpy as np
import rioxarray  # noqa: F401  (.rio アクセサ登録のため)
import xarray as xr

log = logging.getLogger(__name__)

_KELVIN_UNITS = {"kelvin", "k", "degree_kelvin", "degrees_kelvin"}
_CELSIUS_UNITS = {"degree_c", "degrees_c", "celsius", "degc", "degree_celsius"}


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


def load_sst(nc_path, cfg: dict) -> xr.DataArray:
    """NetCDF から SST を読み、摂氏・bbox 切り出し・CRS付与済みの 2D DataArray を返す。"""
    var = cfg["sst"]["erddap"]["variable"]
    b = cfg["bbox"]

    with xr.open_dataset(nc_path, mask_and_scale=True) as ds:
        if var not in ds:
            raise KeyError(f"変数 {var} が見つかりません。存在する変数: {list(ds.data_vars)}")
        da = ds[var].load()

    # 時刻次元を潰して 2D にする
    if "time" in da.dims:
        da = da.isel(time=-1, drop=True)

    # 座標名を latitude/longitude → lat/lon に揃える
    rename = {}
    for src, dst in (("latitude", "lat"), ("longitude", "lon")):
        if src in da.dims:
            rename[src] = dst
    if rename:
        da = da.rename(rename)

    # 緯度が降順で届いた場合に備えて昇順に揃えてから切り出す
    if float(da.lat[0]) > float(da.lat[-1]):
        da = da.sortby("lat")
    da = da.sel(
        lat=slice(b["min_lat"], b["max_lat"]),
        lon=slice(b["min_lon"], b["max_lon"]),
    )
    if da.sizes["lat"] == 0 or da.sizes["lon"] == 0:
        raise ValueError("bbox で切り出した結果が空です。bbox と取得データの範囲を確認してください")

    da = _to_celsius(da.astype("float32"))

    # 地理参照（GeoTIFF は北が上 = 緯度降順が慣例）
    da = da.sortby("lat", ascending=False)
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    da = da.rio.write_crs("EPSG:4326")

    log.info(
        "SST 処理完了: %d x %d px, 値域 %.2f〜%.2f ℃",
        da.sizes["lon"], da.sizes["lat"],
        float(da.min(skipna=True)), float(da.max(skipna=True)),
    )
    return da
