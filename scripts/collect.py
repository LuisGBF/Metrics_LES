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
    parser.add_argument(
        "--profile",
        help="@handle, URL ou ID do perfil. Sem isso, coleta a conta autorizada (OAuth).",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "owned", "public"],
        default="auto",
        help="auto: owned sem --profile, public com --profile.",
    )
    parser.add_argument("--limit", type=int, default=None, help="maximo de posts por plataforma")
    parser.add_argument("--max-spend", type=float, default=None, help="orcamento em USD para o X")
    parser.add_argument(
        "--dry-run", action="store_true", help="apenas estima o custo do X, sem chamar a API"
    )
    args = parser.parse_args()

    if args.platform == "all" and args.profile:
        parser.error("--profile exige uma plataforma especifica: 'youtube' ou 'x'.")

    if args.dry_run and args.platform in ("youtube", "all"):
        print("[youtube] --dry-run so se aplica ao X; pulando o YouTube")

    if args.platform in ("youtube", "all") and not args.dry_run:
        from src.collectors import youtube

        youtube.collect(handle=args.profile, mode=args.mode, limit=args.limit)

    if args.platform in ("x", "all"):
        from src.collectors import x

        x.collect(
            handle=args.profile,
            mode=args.mode,
            limit=args.limit or 200,
            dry_run=args.dry_run,
            budget=args.max_spend,
        )


if __name__ == "__main__":
    main()
