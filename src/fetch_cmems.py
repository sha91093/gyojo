"""クロロフィルの取得（フェーズ2b / Copernicus Marine）。

外部エンドポイント不調でジョブが溶けた反省（v5）を踏まえ、以下を徹底する:

- 日付の総当たりをしない。open_dataset でデータセットの時間・空間範囲を
  1回で読み、利用可能な最新日だけを subset する（修正1）
- 取得は別プロセスで走らせ、fetch_timeout_sec を超えたら確実に kill する。
  スレッドは終了時 join で詰まるため multiprocessing を使う（修正2）
- HTTP のタイムアウト・リトライは環境変数
  COPERNICUSMARINE_HTTPS_TIMEOUT / COPERNICUSMARINE_HTTPS_RETRIES で
  ワークフロー側から絞る（修正3）

クロロフィルは「取れたら嬉しい」レイヤであり、SST の公開を止める権利はない。
"""

from __future__ import annotations

import datetime as dt
import logging
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path

from src import fetch_mur

log = logging.getLogger(__name__)


class CredentialsMissing(RuntimeError):
    """CMEMS 認証情報が環境にない。"""


@dataclass
class FetchResult:
    path: Path
    date: dt.date
    source: str


def _credentials(cfg: dict) -> tuple[str, str]:
    c = cfg["chla"]
    user = os.environ.get(c["username_env"], "").strip()
    pw = os.environ.get(c["password_env"], "").strip()
    if not user or not pw:
        raise CredentialsMissing(
            f"環境変数 {c['username_env']} / {c['password_env']} が未設定です"
        )
    return user, pw


def _probe_and_fetch(q, layer_cfg, user, pw, b, out_dir, prefix):
    """子プロセス: open_dataset で診断→最新日を1回だけ subset する。

    結果は Queue に (status, payload, diag) で返す。
    status='ok' なら payload=(path, iso_date, dataset_id)。
    status='err' なら payload=エラー文字列。
    diag は時間範囲・変数・空間範囲の診断文字列（結論をログに残すため）。
    """
    diag = None
    try:
        import copernicusmarine
        import pandas as pd

        did = layer_cfg["dataset_id"]
        ds = copernicusmarine.open_dataset(dataset_id=did, username=user, password=pw)

        lon_name = "longitude" if "longitude" in ds.coords else "lon"
        lat_name = "latitude" if "latitude" in ds.coords else "lat"
        tmin = pd.Timestamp(ds["time"].min().values).date()
        tmax = pd.Timestamp(ds["time"].max().values).date()
        var_list = list(ds.data_vars)
        lo0, lo1 = float(ds[lon_name].min()), float(ds[lon_name].max())
        la0, la1 = float(ds[lat_name].min()), float(ds[lat_name].max())
        diag = (f"{prefix} [{did}] 時間 {tmin}〜{tmax} / 変数 {var_list} / "
                f"経度 {lo0:.2f}〜{lo1:.2f} / 緯度 {la0:.2f}〜{la1:.2f}")

        # 空間カバー判定（対象海域がデータ範囲に含まれるか）
        if not (lo0 <= b["min_lon"] and b["max_lon"] <= lo1
                and la0 <= b["min_lat"] and b["max_lat"] <= la1):
            q.put(("err", f"対象海域がデータ範囲外（{prefix}）", diag))
            return

        primary = layer_cfg["variable"]
        if primary not in var_list:
            q.put(("err", f"変数 {primary} がデータセットに無い（{prefix}）", diag))
            return
        variables = [primary]
        grad = layer_cfg.get("gradient_variable")
        if grad and grad in var_list:
            variables.append(grad)
        elif grad:
            diag += f" / {grad} は無いため勾配スキップ"

        # 利用可能な最新日から数日だけ降りて、空でない日を採る
        for back in range(int(layer_cfg.get("lookback_days", 3)) + 1):
            date = tmax - dt.timedelta(days=back)
            fname = f"{prefix}_{date.isoformat()}.nc"
            copernicusmarine.subset(
                dataset_id=did,
                variables=variables,
                minimum_longitude=b["min_lon"],
                maximum_longitude=b["max_lon"],
                minimum_latitude=b["min_lat"],
                maximum_latitude=b["max_lat"],
                start_datetime=f"{date.isoformat()}T00:00:00",
                end_datetime=f"{date.isoformat()}T23:59:59",
                username=user,
                password=pw,
                output_directory=str(out_dir),
                output_filename=fname,
                overwrite=True,
                disable_progress_bar=True,
            )
            path = out_dir / fname
            if path.exists() and path.stat().st_size > 0:
                q.put(("ok", (str(path), date.isoformat(), did), diag))
                return
        q.put(("err", f"最新日({tmax})付近で空データ（{prefix}）", diag))
    except Exception as exc:  # noqa: BLE001  子プロセス内の全例外を親に伝える
        q.put(("err", f"{type(exc).__name__}: {exc}", diag))


def _fetch_layer(cfg: dict, layer_cfg: dict, out_dir: Path, prefix: str) -> FetchResult:
    """指定レイヤを別プロセスで取得し、ハードタイムアウトを課す。"""
    user, pw = _credentials(cfg)
    b = fetch_mur.buffered_bbox(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(layer_cfg.get("fetch_timeout_sec", 180))

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(
        target=_probe_and_fetch, args=(q, layer_cfg, user, pw, b, out_dir, prefix)
    )
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join()
        raise RuntimeError(f"{prefix} の取得が {timeout}秒 を超えたため中断しました")
    if q.empty():
        raise RuntimeError(f"{prefix} の取得が結果を返さず終了しました")

    status, payload, diag = q.get()
    if diag:
        log.info("CMEMS診断: %s", diag)  # chla_hires の結論をログに残す
    if status == "err":
        raise RuntimeError(payload)
    path_s, date_s, did = payload
    log.info("%s 取得成功: %s", prefix, Path(path_s).name)
    return FetchResult(path=Path(path_s), date=dt.date.fromisoformat(date_s), source=did)


def fetch_latest(cfg: dict, out_dir: Path) -> FetchResult:
    """広域クロロフィル（gap-free L4 4km）を取得する。"""
    return _fetch_layer(cfg, cfg["chla"], out_dir, "chla")


def fetch_hires(cfg: dict, out_dir: Path) -> FetchResult:
    """高解像度クロロフィル（L3 OLCI 300m・晴天時のみ）を取得する。"""
    return _fetch_layer(cfg, cfg["chla_hires"], out_dir, "chla_hires")
