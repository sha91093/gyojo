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

from src import export, fetch_mur, process

log = logging.getLogger(__name__)


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

    export.export_all(
        sst=sst, err=err, front=front,
        date=result.date, source_url=result.source_url, cfg=cfg,
    )

    log.info("完了: %s の SST / フロントを公開ディレクトリへ出力しました", result.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
