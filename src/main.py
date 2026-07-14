"""エントリポイント。

使い方:
    python -m src.main [--config config.yaml]

取得 → 前処理 → フロント計算 → COG / 値配列JSON / meta.json 出力 までを
一本で実行する。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from src import export, fetch_cmems, fetch_mur, process

log = logging.getLogger(__name__)


class ChlaBundle:
    """クロロフィル関連の成果物。取得できなかった要素は None。"""

    def __init__(self):
        self.chla = None
        self.chla_date = None
        self.measured = None      # L4グリッド上の実測マスク（True=実測, False=補間）
        self.hires = None
        self.hires_date = None
        self.gradient = None


def _try_fetch_chla(cfg: dict, work_dir: Path) -> ChlaBundle:
    """クロロフィル（広域L4 + 高解像度L3）を取得・前処理する。

    どの段階で失敗しても SST/フロントの更新は止めない。
    高解像度が取れれば、その被覆から広域L4の補間箇所も判定する（課題2）。
    """
    b = cfg["bbox"]
    bundle = ChlaBundle()
    if not cfg.get("chla", {}).get("enabled", False):
        return bundle

    # --- 広域 L4（毎日必ず絵が出る。既定表示） ---
    try:
        result = fetch_cmems.fetch_latest(cfg, work_dir)
        bundle.chla = process.clip_bbox(process.load_chla(result.path, cfg), b)
        bundle.chla_date = result.date
    except fetch_cmems.CredentialsMissing as exc:
        log.warning("クロロフィルをスキップ（認証情報なし）: %s", exc)
        return bundle
    except Exception as exc:
        log.warning("広域クロロフィル取得に失敗（SSTは継続）: %s: %s", type(exc).__name__, exc)
        return bundle

    # --- 高解像度 L3（晴天時のみ。任意） ---
    if cfg.get("chla_hires", {}).get("enabled", False):
        try:
            hres = fetch_cmems.fetch_hires(cfg, work_dir)
            hires_buf = process.load_chla(hres.path, cfg, layer_key="chla_hires")
            bundle.hires = process.clip_bbox(hires_buf, b)
            bundle.hires_date = hres.date
            grad = process.load_chla_gradient(hres.path, cfg)
            if grad is not None:
                bundle.gradient = process.clip_bbox(grad, b)
            # L3 の被覆から広域L4の補間箇所を判定（観測日が一致する場合のみ）
            if bundle.chla_date == hres.date:
                bundle.measured = process.measured_mask_on(bundle.chla, bundle.hires)
            else:
                log.info("広域(%s)と高解像度(%s)の観測日が異なるため補間判定は省略",
                         bundle.chla_date, hres.date)
        except Exception as exc:
            log.warning("高解像度クロロフィルをスキップ: %s: %s", type(exc).__name__, exc)

    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="沿岸漁場支援マップ データ更新")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    parser.add_argument("--work-dir", default="work", help="ダウンロード作業ディレクトリ")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    bbox = cfg["bbox"]

    result = fetch_mur.fetch_latest(cfg, Path(args.work_dir))

    # バッファ付きグリッドのままフロントを計算し、表示範囲へクリップする
    sst_buf, err_buf = process.load_dataset(result.path, cfg)
    front_buf = process.compute_front(sst_buf)

    sst = process.clip_bbox(sst_buf, bbox)
    err = process.clip_bbox(err_buf, bbox) if err_buf is not None else None
    front = process.clip_bbox(front_buf, bbox)

    # 派生レイヤの欠損チェック（岸沿いの NaN 伝播などの再発防止）
    process.check_coverage(sst, front, "front")

    # クロロフィルは独立取得（失敗しても SST/フロントは出力する）
    cb = _try_fetch_chla(cfg, Path(args.work_dir))

    export.export_all(
        sst=sst, err=err, front=front,
        chla=cb.chla, chla_date=cb.chla_date, chla_measured=cb.measured,
        chla_hires=cb.hires, chla_hires_date=cb.hires_date, chla_gradient=cb.gradient,
        date=result.date, source_url=result.source_url, cfg=cfg,
    )

    names = ["SST", "フロント"]
    if cb.chla is not None:
        names.append("クロロフィル")
    if cb.hires is not None:
        names.append("クロロフィル(高解像度)")
    log.info("完了: %s の %s を公開ディレクトリへ出力しました", result.date, " / ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
