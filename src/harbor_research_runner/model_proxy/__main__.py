from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from harbor_research_runner.model_proxy.server import serve
from harbor_research_runner.records.proxy_control import read


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--lifeline-fd", type=int)
    args = parser.parse_args()
    config = read(args.config)
    log_path = Path(config.ledger_path).parent / "proxy.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return serve(config, args.config, os.environ, args.lifeline_fd)


if __name__ == "__main__":
    raise SystemExit(main())
