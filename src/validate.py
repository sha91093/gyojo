"""独立した SST プロダクトと MUR を突き合わせて異常日を検証する（レビューv7 修正3-3）。

「7/13 はどの地点も同じ水温」のような異常日が、実際の海洋現象か MUR プロダクト側の
問題かを切り分ける。同じ ERDDAP・同じ bbox・同じ日で MUR と独立プロダクト（既定は
NOAA OISST）を取得し、平均値と空間標準偏差を比較する。

使い方:
    python -m src.validate 2026-07-13
    python -m src.validate 2026-07-11 2026-07-13   # 複数日まとめて

ネットワークが必要（ERDDAP に接続）。GitHub Actions のサンドボックスや
外部遮断環境では実行できない点に注意。
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import sys
from pathlib import Path

import numpy as np
import requests
import xarray as xr
import yaml

log = logging.getLogger(__name__)

# 独立検証用の ERDDAP データセット（MUR とは別系統）。
# NOAA OISST v2.1 日次。dataset_id / 変数名は稼働環境で必ず確認すること
# （erddap の /info/<id>/index.json で一覧できる）
INDEPENDENT = {
    "dataset_id": "ncdcOisst21Agg_LonPM180",
    "variable": "sst",
    "time_of_day": "12:00:00Z",
    "zlev": True,  # OISST は zlev 次元を持つ
}
TIMEOUT = 120


def _erddap_bbox_url(base_url: str, ds: dict, date: dt.date, bbox: dict) -> str:
    t = f"{date.isoformat()}T{ds['time_of_day'].rstrip('Z')}Z"
    dims = f"[({t}):({t})]"
    if ds.get("zlev"):
        dims += "[(0.0):(0.0)]"
    dims += (
        f"[({bbox['min_lat']}):({bbox['max_lat']})]"
        f"[({bbox['min_lon']}):({bbox['max_lon']})]"
    )
    return f"{base_url}/griddap/{ds['dataset_id']}.nc?{ds['variable']}{dims}"


def _fetch_field(url: str, var: str) -> np.ndarray | None:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("取得失敗 HTTP %d: %s", r.status_code, url)
            return None
        with xr.open_dataset(io.BytesIO(r.content)) as ds:
            arr = np.asarray(ds[var].values, dtype="float64").squeeze()
        return arr
    except Exception as exc:
        log.warning("取得エラー: %s", exc)
        return None


def _summary(name: str, arr: np.ndarray) -> dict:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"name": name, "n": 0}
    # MUR はケルビン、OISST は摂氏。摂氏に揃える
    if float(np.nanmedian(finite)) > 200:
        finite = finite - 273.15
    return {
        "name": name,
        "n": int(finite.size),
        "mean": round(float(finite.mean()), 3),
        "std": round(float(finite.std()), 3),
        "min": round(float(finite.min()), 2),
        "max": round(float(finite.max()), 2),
    }


def validate_date(cfg: dict, date: dt.date) -> None:
    e = cfg["sst"]["erddap"]
    bbox = cfg["bbox"]
    mur = _fetch_field(
        _erddap_bbox_url(e["base_url"], {
            "dataset_id": e["dataset_id"], "variable": e["variable"],
            "time_of_day": e["time_of_day"],
        }, date, bbox), e["variable"])
    ind = _fetch_field(
        _erddap_bbox_url(e["base_url"], INDEPENDENT, date, bbox), INDEPENDENT["variable"])

    print(f"\n=== {date} ===")
    for name, arr in (("MUR", mur), (f"独立({INDEPENDENT['dataset_id']})", ind)):
        if arr is None:
            print(f"  {name}: 取得失敗")
            continue
        s = _summary(name, arr)
        if s["n"] == 0:
            print(f"  {name}: 有効ピクセルなし")
        else:
            print(f"  {name}: 平均 {s['mean']}℃ / 空間標準偏差 {s['std']}℃ "
                  f"/ 値域 {s['min']}〜{s['max']} (n={s['n']})")
    if mur is not None and ind is not None:
        ms, is_ = _summary("m", mur), _summary("i", ind)
        if ms.get("n") and is_.get("n"):
            print("  → 判定材料: 独立プロダクトの空間標準偏差も小さければ実際の海洋現象、"
                  "MUR だけ小さければ MUR プロダクト側の問題の可能性")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("使い方: python -m src.validate YYYY-MM-DD [YYYY-MM-DD ...]")
        return 2
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    for a in args:
        validate_date(cfg, dt.date.fromisoformat(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
