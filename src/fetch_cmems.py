"""クロロフィルa の取得（フェーズ2b）。

Copernicus Marine の gap-free L4（雲欠測を時空間補間で埋めた日次プロダクト）を
`copernicusmarine` ツールキットで bbox+バッファ指定の部分取得する。

- 認証は GitHub Secrets と同名の環境変数（既定 CMEMS_USERNAME / CMEMS_PASSWORD）
  から読み、ツールキットに明示的に渡す
- gap-free でも公開ラグがあるため、MUR と同様に直近日を遡って最新日を採用する
- gap-free とはいえ「補間で埋めた値」であることに注意（元観測が古い可能性）。
  観測日は meta にレイヤ単位で持ち、古いデータを最新に見せない
"""

from __future__ import annotations

import datetime as dt
import logging
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


def fetch_latest(cfg: dict, out_dir: Path) -> FetchResult:
    """直近で取得可能なクロロフィルを out_dir にダウンロードする。

    認証情報が無い場合は CredentialsMissing、全日取得失敗で RuntimeError。
    """
    import copernicusmarine

    c = cfg["chla"]
    user, pw = _credentials(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    b = fetch_mur.buffered_bbox(cfg)  # SST と同じバッファ付き範囲

    today = dt.datetime.now(dt.timezone.utc).date()
    lookback = int(c["lookback_days"])
    errors: list[str] = []
    for delta in range(lookback + 1):
        date = today - dt.timedelta(days=delta)
        fname = f"chla_{date.isoformat()}.nc"
        try:
            copernicusmarine.subset(
                dataset_id=c["dataset_id"],
                variables=[c["variable"]],
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
                log.info("クロロフィル取得成功: %s (%d bytes)", fname, path.stat().st_size)
                return FetchResult(path=path, date=date, source=c["dataset_id"])
            errors.append(f"{date}: 空ファイル")
        except Exception as exc:  # 未公開日は例外になる。遡って再試行する
            errors.append(f"{date}: {type(exc).__name__}")
            log.info("%s のクロロフィルは取得できず（%s）", date, type(exc).__name__)

    raise RuntimeError(
        f"直近 {lookback + 1} 日分のクロロフィルをいずれも取得できませんでした: "
        + "; ".join(errors[:5])
    )
