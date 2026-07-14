"""MUR SST (v4.1) の取得（フェーズ1）。

NOAA CoastWatch ERDDAP の griddap から bbox 指定で NetCDF を部分取得する。
配信データは NASA JPL PO.DAAC の MUR-JPL-L4-GLOB-v4.1 と同一。

- analysed_sst に加えて analysis_error（推定誤差）も取得する。
  沿岸ピクセルの信頼度表示に使う
- 取得範囲は bbox + bbox_buffer_deg。フロント計算のラスタ端破綻を
  表示領域に持ち込まないためのバッファ
- MUR は観測から公開まで概ね1日程度かかるため、今日から lookback_days 日
  遡って、取得できた最新日を採用する
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120  # 秒


@dataclass
class FetchResult:
    path: Path
    date: dt.date
    source_url: str


def buffered_bbox(cfg: dict) -> dict:
    """取得用に四方へ bbox_buffer_deg だけ広げた bbox。"""
    b = cfg["bbox"]
    buf = float(cfg.get("bbox_buffer_deg", 0.0))
    return {
        "min_lon": round(b["min_lon"] - buf, 6),
        "max_lon": round(b["max_lon"] + buf, 6),
        "min_lat": round(b["min_lat"] - buf, 6),
        "max_lat": round(b["max_lat"] + buf, 6),
    }


def _griddap_url(cfg: dict, date: dt.date) -> str:
    e = cfg["sst"]["erddap"]
    b = buffered_bbox(cfg)
    t = f"{date.isoformat()}T{e['time_of_day'].rstrip('Z')}Z"
    dims = (
        f"[({t}):({t})]"
        f"[({b['min_lat']}):({b['max_lat']})]"
        f"[({b['min_lon']}):({b['max_lon']})]"
    )
    variables = [e["variable"]]
    for opt in ("error_variable", "anomaly_variable"):
        if e.get(opt):
            variables.append(e[opt])
    query = ",".join(f"{v}{dims}" for v in variables)
    return f"{e['base_url']}/griddap/{e['dataset_id']}.nc?{query}"


def _latest_available_date(cfg: dict) -> dt.date | None:
    """ERDDAP のメタデータから time_coverage_end を読む。失敗したら None。"""
    e = cfg["sst"]["erddap"]
    url = f"{e['base_url']}/info/{e['dataset_id']}/index.json"
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        for row in r.json()["table"]["rows"]:
            # 行形式: [rowType, variableName, attributeName, dataType, value]
            if row[2] == "time_coverage_end":
                return dt.datetime.fromisoformat(row[4].replace("Z", "+00:00")).date()
    except Exception as exc:  # メタデータが読めなくても日付リトライで拾える
        log.warning("time_coverage_end の取得に失敗: %s", exc)
    return None


def fetch_latest(cfg: dict, out_dir: Path, target_date: dt.date | None = None) -> FetchResult:
    """MUR SST を out_dir にダウンロードする。

    target_date を指定するとその日だけを取得する（過去日検証用 / 修正3-2）。
    未指定なら今日から lookback_days 日遡り、最初に成功した日を返す。
    全滅した場合は RuntimeError。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    if target_date is not None:
        log.info("指定日を取得します: %s", target_date)
        start, lookback = target_date, 0
    else:
        today = dt.datetime.now(dt.timezone.utc).date()
        start = today
        latest = _latest_available_date(cfg)
        if latest is not None:
            start = min(start, latest)
            log.info("ERDDAP の最新データ日: %s", latest)
        lookback = int(cfg["sst"]["lookback_days"])
    errors: list[str] = []
    for delta in range(lookback + 1):
        date = start - dt.timedelta(days=delta)
        url = _griddap_url(cfg, date)
        log.info("取得を試行: %s", date)
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT)
            is_netcdf = r.content[:3] == b"CDF" or r.content[:4] == b"\x89HDF"
            if r.status_code == 200 and is_netcdf:
                path = out_dir / f"mur_sst_{date.isoformat()}.nc"
                path.write_bytes(r.content)
                log.info("取得成功: %s (%d bytes)", path.name, len(r.content))
                return FetchResult(path=path, date=date, source_url=url)
            errors.append(f"{date}: HTTP {r.status_code}")
            log.info("%s は未公開または取得失敗 (HTTP %d)", date, r.status_code)
        except requests.RequestException as exc:
            errors.append(f"{date}: {exc}")
            log.warning("%s の取得でエラー: %s", date, exc)

    raise RuntimeError(
        f"直近 {lookback + 1} 日分の MUR SST をいずれも取得できませんでした: "
        + "; ".join(errors)
    )
