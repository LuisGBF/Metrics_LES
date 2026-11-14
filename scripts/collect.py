from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta dados das APIs oficiais.")
    parser.add_argument("platform", choices=["youtube", "x", "all"])
    parser.add_argument("--limit", type=int, default=None, help="maximo de posts por plataforma")
    parser.add_argument("--max-spend", type=float, default=None, help="orcamento em USD para o X")
    parser.add_argument("--dry-run", action="store_true", help="apenas estima o custo do X, sem chamar a API")
    args = parser.parse_args()

    if args.platform in ("youtube", "all"):
        from src.collectors import youtube

        youtube.collect(limit=args.limit)

    if args.platform in ("x", "all"):
        from src.collectors import x

        x.collect(limit=args.limit or 200, dry_run=args.dry_run, budget=args.max_spend)


if __name__ == "__main__":
    main()
