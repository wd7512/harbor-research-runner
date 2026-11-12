"""The standard-library script the fake harness runs inside the task container."""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import signal
import time
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_MODEL = "fake-model"
_DEFAULT_OUT = Path("/app/hello.txt")
_BODY = {
    "model": _MODEL,
    "messages": [{"role": "user", "content": "hello"}],
    "stream": True,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the script's command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--requests", type=int, default=1)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--sleep-s", type=float, default=0)
    parser.add_argument("--ignore-stop", action="store_true")
    parser.add_argument("--keep-requesting-s", type=float, default=0)
    parser.add_argument("--solve", action="store_true")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    return parser.parse_args(argv)


def _send_request(base_url: str) -> None:
    request = Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(_BODY).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        for _line in response:
            pass


def main(argv: list[str] | None = None) -> int:
    """Send the scripted requests and return the requested exit code."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.ignore_stop:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        for _ in range(args.requests):
            _send_request(args.base_url)
    except (OSError, http.client.HTTPException) as exc:
        logger.error("request failed: %s", exc)
        return 1
    if args.sleep_s > 0:
        time.sleep(args.sleep_s)
    if args.solve:
        try:
            args.out.write_text("hello\n", encoding="utf-8")
        except OSError as exc:
            logger.error("could not write %s: %s", args.out, exc)
            return 1
    if args.keep_requesting_s > 0:
        deadline = time.monotonic() + args.keep_requesting_s
        while time.monotonic() < deadline:
            try:
                _send_request(args.base_url)
            except (OSError, http.client.HTTPException) as exc:
                logger.warning("request refused: %s", exc)
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
