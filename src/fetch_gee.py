"""GCOM-C/SGLI の取得（Google Earth Engine 経由 / 移行フェーズ1）。

MUR(ERDDAP) の代替データ源。GEE 上の JAXA GCOM-C L3 OCEAN から SST を取得する。
config の sst.source == 'gee' のときだけ使う。

設計原則（過去の教訓の踏襲）:
- 認証はサービスアカウントJSON鍵を環境変数(GitHub Secrets)から読み、key_data で渡す
- 最新画像はコレクションから特定（総当たり日付ループはしない）
- 外部呼び出しはハードタイムアウトで打ち切る（別プロセスで実行し確実に kill）
- スケール係数はカタログ確認済み(×0.0012 -10)。QA はビット定義未確認のため
  分布をログに出すだけで半透明化には使わない（閾値の空振りを繰り返さない）

注意: この取得層はサンドボックスからは検証できない。認証が Actions から通ることを
まず手動実行（スモークテスト）で確認すること。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class CredentialsMissing(RuntimeError):
    """GEE のサービスアカウント鍵が環境にない。"""


@dataclass
class FetchResult:
    path: Path
    date: dt.date
    source: str


def _key_json(cfg: dict) -> str:
    env = cfg["gee"]["service_account_key_env"]
    raw = os.environ.get(env, "").strip()
    if not raw:
        raise CredentialsMissing(f"環境変数 {env} が未設定です（サービスアカウントJSON鍵）")
    return raw


def _init_ee(cfg: dict, key_json: str):
    import ee

    info = json.loads(key_json)
    creds = ee.ServiceAccountCredentials(info.get("client_email"), key_data=key_json)
    project = cfg["gee"].get("project") or info.get("project_id")
    ee.Initialize(creds, project=project)
    return ee


def _bbox_geom(ee, cfg: dict):
    from src import fetch_mur  # buffered_bbox を流用（sourceに依存しない）
    b = fetch_mur.buffered_bbox(cfg)
    return ee.Geometry.Rectangle([b["min_lon"], b["min_lat"], b["max_lon"], b["max_lat"]])


def _probe_and_fetch(q, cfg, key_json, out_dir, prefix, target_iso):
    """子プロセス: 認証→最新(or指定)画像を特定→GeoTIFFをダウンロード。"""
    diag = None
    try:
        import requests

        ee = _init_ee(cfg, key_json)
        s = cfg["gee"]["sst"]
        geom = _bbox_geom(ee, cfg)

        col = ee.ImageCollection(s["collection"]).filterBounds(geom)
        if target_iso:
            start = target_iso
            end = (dt.date.fromisoformat(target_iso) + dt.timedelta(days=1)).isoformat()
            col = col.filterDate(start, end)
        else:
            today = dt.datetime.now(dt.timezone.utc).date()
            start = (today - dt.timedelta(days=int(cfg["gee"]["lookback_days"]))).isoformat()
            col = col.filterDate(start, (today + dt.timedelta(days=1)).isoformat())
        if s.get("daytime_only"):
            col = col.filter(ee.Filter.eq("SATELLITE_DIRECTION", "D"))

        col = col.sort("system:time_start", False)
        n = col.size().getInfo()
        if not n:
            q.put(("err", f"対象期間に画像なし（{prefix}）", diag))
            return
        img = ee.Image(col.first())
        date_iso = img.date().format("YYYY-MM-dd").getInfo()

        # 物理値(℃) = raw * scale + offset
        sst = img.select(s["band"]).multiply(float(s["scale"])).add(float(s["offset"])).rename("sst")

        # QA の分布をログ用に確認（ビット定義未確認のため使用はしない）
        try:
            qa_hist = img.select(s["qa_band"]).reduceRegion(
                reducer=ee.Reducer.frequencyHistogram(), geometry=geom,
                scale=int(cfg["gee"]["download_scale_m"]), maxPixels=1e8,
            ).getInfo()
            diag = f"{prefix} {date_iso} QA分布(先頭)={str(qa_hist)[:200]}"
        except Exception:
            diag = f"{prefix} {date_iso} QA分布の取得は省略"

        url = sst.getDownloadURL({
            "region": geom,
            "scale": int(cfg["gee"]["download_scale_m"]),
            "crs": "EPSG:4326",
            "format": "GEO_TIFF",
            "bands": ["sst"],
        })
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        path = out_dir / f"{prefix}_{date_iso}.tif"
        path.write_bytes(r.content)
        if path.stat().st_size == 0:
            q.put(("err", f"空ファイル（{prefix}）", diag))
            return
        q.put(("ok", (str(path), date_iso, s["collection"]), diag))
    except Exception as exc:  # noqa: BLE001
        q.put(("err", f"{type(exc).__name__}: {exc}", diag))


def _fetch(cfg: dict, out_dir: Path, prefix: str, target_date: dt.date | None) -> FetchResult:
    key_json = _key_json(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    timeout = int(cfg["gee"].get("fetch_timeout_sec", 180))
    target_iso = target_date.isoformat() if target_date else None

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_probe_and_fetch, args=(q, cfg, key_json, out_dir, prefix, target_iso))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate(); p.join(5)
        if p.is_alive():
            p.kill(); p.join()
        raise RuntimeError(f"{prefix} の取得が {timeout}秒 を超えたため中断しました")
    if q.empty():
        raise RuntimeError(f"{prefix} の取得が結果を返さず終了しました")
    status, payload, diag = q.get()
    if diag:
        log.info("GEE診断: %s", diag)
    if status == "err":
        raise RuntimeError(payload)
    path_s, date_s, coll = payload
    log.info("%s 取得成功: %s", prefix, Path(path_s).name)
    return FetchResult(path=Path(path_s), date=dt.date.fromisoformat(date_s), source=coll)


def fetch_sst(cfg: dict, out_dir: Path, target_date: dt.date | None = None) -> FetchResult:
    """GCOM-C SST を取得して GeoTIFF(℃) を out_dir に保存する。"""
    return _fetch(cfg, out_dir, "gcomc_sst", target_date)


def _smoke(argv=None) -> int:
    """認証と1枚取得の疎通確認（フェーズ1マイルストーン）。

        python -m src.fetch_gee            # 最新日
        python -m src.fetch_gee 2026-07-12 # 指定日
    """
    import sys
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    target = None
    if args and args[0].strip():
        import re
        try:
            target = dt.date.fromisoformat(re.sub(r"[/_.\s]", "-", args[0].strip()))
        except ValueError:
            print(f"日付の形式が不正です: {args[0]!r}（YYYY-MM-DD）")
            return 2
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    try:
        res = fetch_sst(cfg, Path("work"), target_date=target)
        print(f"OK: {res.date} を取得 -> {res.path} ({res.source})")
        # スケール係数の検証: 実データを読み、物理値(℃)として妥当か確認する
        # （290℃の海になっていないか。この確認をしてから本番切替する）
        try:
            from src import process
            sst = process.load_gee_raster(res.path, cfg)
            v = sst.values[__import__("numpy").isfinite(sst.values)]
            if v.size:
                print(f"SST値域チェック: min={v.min():.2f} mean={v.mean():.2f} "
                      f"max={v.max():.2f} ℃ / 空間標準偏差={v.std():.3f}℃")
                print("→ 夏の海として妥当（概ね20〜30℃）なら scale/offset は正しい")
        except Exception as e:  # noqa: BLE001
            print(f"（値域チェックは省略: {type(e).__name__}: {e}）")
        return 0
    except CredentialsMissing as e:
        print(f"認証情報なし: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"失敗: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
