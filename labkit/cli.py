"""``labkit``: deploy-pi, init, install-skills."""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from labkit import __version__, deploy_pi, scaffold


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="labkit", description="Shared tools for the lab's projects.")
    ap.add_argument("--version", action="version", version=f"labkit {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    deploy_pi.add_parser(sub)
    scaffold.add_parsers(sub)
    args = ap.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
