"""
WeatherPredictor entrypoint.

    python -m src.run                 # run the loop in the configured WP_MODE
    python -m src.run --once          # one poll+scan cycle, print stats, exit
    python -m src.run --selfcheck     # wire everything, print what's configured, exit

Mode/venue/keys all come from the environment (see .env.example). Default mode
is SIM (real public market + weather data, simulated fills) — safe to run with
no venue credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys

from src.config import get_config
from src.runtime.factory import build_loop


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


async def _run(once: bool) -> None:
    loop = build_loop()
    if once:
        stats = await loop.run_once()
        print(json.dumps({"cycle_stats": stats}, indent=2))
        return

    stop = asyncio.Event()

    def _handle(*_):
        loop.stop()
        stop.set()

    try:
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
    except Exception:
        pass
    await loop.run()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WeatherPredictor prediction-market bot")
    ap.add_argument("--once", action="store_true", help="run a single poll+scan cycle and exit")
    ap.add_argument("--selfcheck", action="store_true", help="wire everything, print config, exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    _setup_logging(args.verbose)

    config = get_config()
    try:
        config.assert_runnable()
    except Exception as exc:
        print(f"REFUSING TO START: {exc}", file=sys.stderr)
        return 2

    if args.selfcheck:
        loop = build_loop()
        print(json.dumps({
            "config": config.redacted(),
            "venues": list(loop.catalogs),
            "strategies": [s.name for s in loop.strategies],
            "obs_feeds": [type(f).__name__ for f in loop.obs_feeds],
            "adapters": loop.router.registered(),
        }, indent=2, default=str))
        return 0

    asyncio.run(_run(args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
