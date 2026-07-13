"""成果物の出力（フェーズ1）。

- COG (Cloud Optimized GeoTIFF): QGIS / 解析用。物理値（℃）をそのまま格納
- カラーマップ適用済み PNG: Web マップのオーバーレイ用
- meta.json: 観測日・データソース・値域・凡例。Web マップが参照する
- latest/ は常に同名で上書き（参照URLを固定するため）、archive/ に日付別コピー
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
from pathlib import Path

import matplotlib
import numpy as np
import xarray as xr

matplotlib.use("Agg")
from matplotlib import colormaps
from matplotlib.colors import Normalize, to_hex
from PIL import Image

log = logging.getLogger(__name__)

LEGEND_STOPS = 9  # 凡例グラデーションの色数


def _display_range(da: xr.DataArray, disp: dict) -> tuple[float, float]:
    """表示用カラーレンジを決める（COG の物理値には影響しない）。"""
    if disp.get("range_mode", "auto") == "fixed":
        return float(disp["vmin"]), float(disp["vmax"])

    p_lo, p_hi = disp.get("auto_percentiles", [2, 98])
    values = da.values[np.isfinite(da.values)]
    vmin = float(np.percentile(values, p_lo))
    vmax = float(np.percentile(values, p_hi))

    # 幅が狭すぎるとノイズが強調されるので最低幅を確保
    min_span = float(disp.get("min_span", 3.0))
    if vmax - vmin < min_span:
        mid = (vmax + vmin) / 2
        vmin, vmax = mid - min_span / 2, mid + min_span / 2

    # 安全弁: fixed 用のレンジを超えない
    vmin = max(vmin, float(disp["vmin"]))
    vmax = min(vmax, float(disp["vmax"]))
    # 0.5℃ 刻みに丸めて凡例を読みやすくする
    vmin = np.floor(vmin * 2) / 2
    vmax = np.ceil(vmax * 2) / 2
    return float(vmin), float(vmax)


def write_cog(da: xr.DataArray, path: Path) -> None:
    da.rio.to_raster(path, driver="COG", compress="DEFLATE")
    log.info("COG 出力: %s (%d bytes)", path, path.stat().st_size)


def write_png(da: xr.DataArray, path: Path, vmin: float, vmax: float, cmap_name: str) -> None:
    """カラーマップ適用済み PNG。欠測（陸域）は透明にする。"""
    data = da.values  # (lat 降順, lon 昇順) = 北が上
    cmap = colormaps[cmap_name]
    norm = Normalize(vmin=vmin, vmax=vmax, clip=True)
    rgba = cmap(norm(data))  # NaN も 0..1 に写るので後で alpha を消す
    rgba[..., 3] = np.where(np.isfinite(data), 0.9, 0.0)
    img = Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA")
    # ピクセルの粗さを見せないよう Web 表示向けに 4 倍へ拡大（bicubic）
    img = img.resize((img.width * 4, img.height * 4), Image.BICUBIC)
    img.save(path, optimize=True)
    log.info("PNG 出力: %s", path)


def _legend_stops(cmap_name: str) -> list[str]:
    cmap = colormaps[cmap_name]
    return [to_hex(cmap(i / (LEGEND_STOPS - 1))) for i in range(LEGEND_STOPS)]


def write_meta(
    da: xr.DataArray,
    path: Path,
    *,
    date: dt.date,
    source_url: str,
    cfg: dict,
    vmin: float,
    vmax: float,
) -> dict:
    b = cfg["bbox"]
    values = da.values[np.isfinite(da.values)]
    meta = {
        "layers": {
            "sst": {
                "date": date.isoformat(),
                "source": "MUR SST v4.1 (NASA JPL PO.DAAC / NOAA CoastWatch ERDDAP)",
                "source_url": source_url,
                "variable": str(cfg["sst"]["erddap"]["variable"]),
                "units": "℃",
                "png": "sst.png",
                "cog": "sst.tif",
                "vmin": vmin,
                "vmax": vmax,
                "colormap": cfg["sst"]["display"]["colormap"],
                "legend_colors": _legend_stops(cfg["sst"]["display"]["colormap"]),
                "stats": {
                    "min": round(float(values.min()), 2),
                    "max": round(float(values.max()), 2),
                    "mean": round(float(values.mean()), 2),
                },
            }
        },
        "bbox": [b["min_lon"], b["min_lat"], b["max_lon"], b["max_lat"]],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("meta.json 出力: %s", path)
    return meta


def export_all(da: xr.DataArray, *, date: dt.date, source_url: str, cfg: dict) -> None:
    """latest/ へ出力し、archive/YYYY-MM-DD/ へコピー、古い archive を削除する。"""
    data_dir = Path(cfg["output"]["data_dir"])
    latest = data_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)

    disp = cfg["sst"]["display"]
    vmin, vmax = _display_range(da, disp)
    log.info("表示レンジ: %.1f〜%.1f ℃", vmin, vmax)

    write_cog(da, latest / "sst.tif")
    write_png(da, latest / "sst.png", vmin, vmax, disp["colormap"])
    write_meta(
        da, latest / "meta.json",
        date=date, source_url=source_url, cfg=cfg, vmin=vmin, vmax=vmax,
    )

    archive = data_dir / "archive" / date.isoformat()
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("sst.tif", "sst.png", "meta.json"):
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
