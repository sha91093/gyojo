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
        self.uncertainty = None   # CHL_uncertainty（低信頼ピクセルの半透明化に使う）
        self.gradient = None      # 広域4kmから算出したクロロフィル勾配
        self.measured = None      # L4グリッド上の実測マスク（True=実測, False=補間）
        self.hires = None
        self.hires_date = None


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
        # 推定誤差（半透明化用）と flags の確認、勾配（穴のない4km側で計算）
        unc = process.load_chla_uncertainty(result.path, cfg)
        if unc is not None:
            bundle.uncertainty = process.clip_bbox(unc, b)
        process.log_flags_summary(result.path, cfg)
        bundle.gradient = process.compute_chla_gradient(bundle.chla)
    except fetch_cmems.CredentialsMissing as exc:
        log.warning("クロロフィルをスキップ（認証情報なし）: %s", exc)
        return bundle
    except Exception as exc:
        log.warning("広域クロロフィル取得に失敗（SSTは継続）: %s: %s", type(exc).__name__, exc)
        return bundle

    # --- 高解像度 L3（晴天時のみ。任意） ---
    # 全面雲/未観測なら hires は全 NaN になり、export 側でレイヤをスキップする。
    # その場合でも被覆から実測カバー率0%を算出できるので取得は試みる
    if cfg.get("chla_hires", {}).get("enabled", False):
        try:
            hres = fetch_cmems.fetch_hires(cfg, work_dir)
            hires_buf = process.load_chla(hres.path, cfg, layer_key="chla_hires")
            bundle.hires = process.clip_bbox(hires_buf, b)
            bundle.hires_date = hres.date
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

    # --- 修正4: SST/フロントを先に公開する（クロロフィルの完了を待たない） ---
    # 外部(CMEMS)が落ちていても、確実に取れている SST は必ず公開される
    export.export_all(
        sst=sst, err=err, front=front,
        date=result.date, source_url=result.source_url, cfg=cfg,
    )
    log.info("SST / フロントを公開しました（%s）", result.date)

    # --- クロロフィルは追記。取れたら meta を上書き再出力する ---
    cb = _try_fetch_chla(cfg, Path(args.work_dir))
    if cb.chla is not None:
        export.export_all(
            sst=sst, err=err, front=front,
            chla=cb.chla, chla_date=cb.chla_date,
            chla_uncertainty=cb.uncertainty, chla_measured=cb.measured,
            chla_gradient=cb.gradient,
            chla_hires=cb.hires, chla_hires_date=cb.hires_date,
            date=result.date, source_url=result.source_url, cfg=cfg,
        )
        log.info("クロロフィルを追記しました")
    else:
        log.info("クロロフィルは今回取得できませんでした（SST/フロントのみ公開）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
