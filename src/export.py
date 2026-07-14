"""成果物の出力。

- COG (Cloud Optimized GeoTIFF): QGIS / 解析用。物理値をそのまま格納
- 値配列 JSON (sst_values.json / front_values.json): Web マップが
  ブラウザ側 Canvas で色付けするための生値。カラーレンジの切替や
  クリックでの数値表示をクライアントだけで実現する
- meta.json: 観測日・データソース・値域・凡例。Web マップが参照する
- latest/ は常に同名で上書き（参照URLを固定するため）、archive/ に日付別コピー
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from pathlib import Path

import numpy as np
import xarray as xr

log = logging.getLogger(__name__)


def _round_half(x: float, up: bool) -> float:
    return float(np.ceil(x * 2) / 2) if up else float(np.floor(x * 2) / 2)


def auto_range(values: np.ndarray, disp: dict) -> tuple[float, float]:
    """自動カラーレンジ（percentile + 最低幅 + 安全弁 + 0.5℃丸め）。"""
    p_lo, p_hi = disp.get("auto_percentiles", [2, 98])
    finite = values[np.isfinite(values)]
    vmin = float(np.percentile(finite, p_lo))
    vmax = float(np.percentile(finite, p_hi))

    min_span = float(disp.get("min_span", 3.0))
    if vmax - vmin < min_span:
        mid = (vmax + vmin) / 2
        vmin, vmax = mid - min_span / 2, mid + min_span / 2

    vmin = max(vmin, float(disp["vmin"]))
    vmax = min(vmax, float(disp["vmax"]))
    return _round_half(vmin, up=False), _round_half(vmax, up=True)


def fixed_range_for(date: dt.date, disp: dict) -> list[float]:
    """季節別の固定カラーレンジ（6-10月=summer, 11-5月=winter）。"""
    season = "summer" if 6 <= date.month <= 10 else "winter"
    return [float(v) for v in disp["fixed_range"][season]]


def log_auto_range(values: np.ndarray, disp: dict) -> tuple[float, float]:
    """対数スケール向けの自動レンジ（percentile を取り vmin/vmax でクランプ）。"""
    p_lo, p_hi = disp.get("auto_percentiles", [2, 98])
    finite = values[np.isfinite(values) & (values > 0)]
    if finite.size == 0:
        return float(disp["vmin"]), float(disp["vmax"])
    vmin = max(float(np.percentile(finite, p_lo)), float(disp["vmin"]))
    vmax = min(float(np.percentile(finite, p_hi)), float(disp["vmax"]))
    if vmax <= vmin:
        vmax = vmin * 2
    return round(vmin, 3), round(vmax, 3)


def write_cog(da: xr.DataArray, path: Path) -> None:
    da.rio.to_raster(path, driver="COG", compress="DEFLATE")
    log.info("COG 出力: %s (%d bytes)", path, path.stat().st_size)


def _to_list(values: np.ndarray, ndigits: int) -> list:
    """行優先（行0=北端）の1次元リスト。NaN は None。"""
    flat = np.round(values.astype("float64"), ndigits).ravel()
    return [None if not np.isfinite(v) else float(v) for v in flat]


def write_values_json(
    da: xr.DataArray,
    path: Path,
    *,
    errors: xr.DataArray | None = None,
    ndigits: int = 2,
) -> dict:
    """Canvas 描画・クリック参照用の値配列。行0が北端、列0が西端。

    bbox はグリッドのピクセル外縁（セル中心±半ピクセル）から導出する。
    こうすると Canvas の貼り付け位置・クリック座標の逆算が COG と一致する。
    """
    values = da.values
    finite = values[np.isfinite(values)]
    lat = da["lat"].values
    lon = da["lon"].values
    half_lat = abs(float(lat[1] - lat[0])) / 2
    half_lon = abs(float(lon[1] - lon[0])) / 2
    payload = {
        "bbox": [
            round(float(lon.min()) - half_lon, 6),
            round(float(lat.min()) - half_lat, 6),
            round(float(lon.max()) + half_lon, 6),
            round(float(lat.max()) + half_lat, 6),
        ],
        "width": int(da.sizes["lon"]),
        "height": int(da.sizes["lat"]),
        "values": _to_list(values, ndigits),
        "stats": {
            "min": round(float(finite.min()), ndigits),
            "max": round(float(finite.max()), ndigits),
            "mean": round(float(finite.mean()), ndigits),
            "p2": round(float(np.percentile(finite, 2)), ndigits),
            "p98": round(float(np.percentile(finite, 98)), ndigits),
        },
    }
    if errors is not None:
        payload["errors"] = _to_list(errors.values, ndigits)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log.info("値配列 JSON 出力: %s (%d bytes)", path, path.stat().st_size)
    return payload


def export_all(
    *,
    sst: xr.DataArray,
    err: xr.DataArray | None,
    front: xr.DataArray | None,
    date: dt.date,
    source_url: str,
    cfg: dict,
    chla: xr.DataArray | None = None,
    chla_date: dt.date | None = None,
) -> None:
    """latest/ へ出力し、archive/YYYY-MM-DD/ へコピー、古い archive を削除する。"""
    data_dir = Path(cfg["output"]["data_dir"])
    latest = data_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    bbox = cfg["bbox"]
    disp = cfg["sst"]["display"]

    # --- SST ---
    write_cog(sst, latest / "sst.tif")
    sst_payload = write_values_json(sst, latest / "sst_values.json", errors=err)
    vmin, vmax = auto_range(sst.values, disp)

    sst_meta = {
        "date": date.isoformat(),
        "source": "MUR SST v4.1 (NASA JPL PO.DAAC / NOAA CoastWatch ERDDAP)",
        "source_url": source_url,
        "variable": str(cfg["sst"]["erddap"]["variable"]),
        "units": "℃",
        "cog": "sst.tif",
        "values": "sst_values.json",
        "range_mode": disp.get("range_mode", "auto"),
        "auto_range": [vmin, vmax],
        "fixed_range": fixed_range_for(date, disp),
        # 後方互換（旧フロントエンドが参照）
        "vmin": vmin,
        "vmax": vmax,
        "error_threshold": float(cfg["sst"]["confidence"]["error_threshold_c"]),
        "stats": sst_payload["stats"],
    }

    layers = {"sst": sst_meta}
    written = ["sst.tif", "sst_values.json"]

    # --- フロント強度 ---
    if front is not None:
        write_cog(front, latest / "front.tif")
        # errors は SST の analysis_error を流用する。沿岸SSTの信頼度が低い以上、
        # そこから微分したフロントの信頼度はさらに低いため、同じ基準で薄く表示する
        front_payload = write_values_json(
            front, latest / "front_values.json", errors=err, ndigits=3
        )
        fcfg = cfg.get("front", {})
        pct = float(fcfg.get("vmax_percentile", 98))
        finite = front.values[np.isfinite(front.values)]
        fvmax = max(
            float(np.percentile(finite, pct)), float(fcfg.get("vmax_floor", 0.1))
        )
        layers["front"] = {
            "date": date.isoformat(),
            "source": "MUR SST から算出（Sobel 勾配, ℃/km, cos(lat) 補正済み）",
            "units": "℃/km",
            "cog": "front.tif",
            "values": "front_values.json",
            "vmin": 0.0,
            "vmax": round(fvmax, 3),
            "error_threshold": float(cfg["sst"]["confidence"]["error_threshold_c"]),
            "stats": front_payload["stats"],
        }
        written += ["front.tif", "front_values.json"]

    # --- クロロフィル ---
    if chla is not None:
        cdisp = cfg["chla"]["display"]
        write_cog(chla, latest / "chla.tif")
        chla_payload = write_values_json(chla, latest / "chla_values.json", ndigits=3)
        cvmin, cvmax = log_auto_range(chla.values, cdisp)
        layers["chla"] = {
            "date": (chla_date or date).isoformat(),
            "source": "Copernicus Marine gap-free L4 (OCEANCOLOUR_GLO_BGC_L4_NRT_009_102)",
            "variable": str(cfg["chla"]["variable"]),
            "units": "mg/m³",
            "cog": "chla.tif",
            "values": "chla_values.json",
            "scale": cdisp.get("scale", "log"),
            "colormap": cdisp.get("colormap", "algae"),
            "vmin": cvmin,
            "vmax": cvmax,
            "stats": chla_payload["stats"],
        }
        written += ["chla.tif", "chla_values.json"]

    meta = {
        "layers": layers,
        "bbox": [bbox["min_lon"], bbox["min_lat"], bbox["max_lon"], bbox["max_lat"]],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    (latest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    written.append("meta.json")
    log.info("meta.json 出力: %s", latest / "meta.json")

    # --- archive ---
    archive = data_dir / "archive" / date.isoformat()
    archive.mkdir(parents=True, exist_ok=True)
    for name in written:
        shutil.copy2(latest / name, archive / name)
    log.info("archive へコピー: %s", archive)

    _prune_archive(data_dir / "archive", int(cfg["output"]["archive_retention_days"]))
    _write_archive_index(data_dir / "archive")


def _write_archive_index(archive_root: Path) -> None:
    """閲覧可能な過去データの日付一覧。Web マップの日付送りUIが参照する。"""
    dates = []
    for child in sorted(archive_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            dt.date.fromisoformat(child.name)
        except ValueError:
            continue
        dates.append(child.name)
    (archive_root / "index.json").write_text(
        json.dumps({"dates": dates}, indent=2), encoding="utf-8"
    )
    log.info("archive index 出力: %d 日分", len(dates))


def _prune_archive(archive_root: Path, retention_days: int) -> None:
    if not archive_root.is_dir():
        return
    cutoff = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=retention_days)
    for child in sorted(archive_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            day = dt.date.fromisoformat(child.name)
        except ValueError:
            continue  # 日付形式でないディレクトリには触らない
        if day < cutoff:
            shutil.rmtree(child)
            log.info("保持期間 (%d日) 超過のため削除: %s", retention_days, child)
