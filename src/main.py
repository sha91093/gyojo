"""エントリポイント（フェーズ1: MUR SST）。

使い方:
    python -m src.main [--config config.yaml]

取得 → 前処理 → COG/PNG/meta.json 出力 までを一本で実行する。
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

    result = fetch_mur.fetch_latest(cfg, Path(args.work_dir))
    da = process.load_sst(result.path, cfg)
    export.export_all(da, date=result.date, source_url=result.source_url, cfg=cfg)

    log.info("完了: %s の SST を公開ディレクトリへ出力しました", result.date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
