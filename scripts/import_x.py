from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importa o CSV do Post Activity Dashboard (analytics.x.com), sem usar API."
    )
    parser.add_argument("csv", help="arquivo .csv exportado do analytics.x.com")
    parser.add_argument("--profile", required=True, help="@usuario dono do perfil")
    parser.add_argument(
        "--followers", type=int, default=None, help="seguidores atuais (opcional, para normalizar alcance)"
    )
    parser.add_argument(
        "--dayfirst", action="store_true", help="interpretar datas como DD/MM/AAAA"
    )
    parser.add_argument("--dry-run", action="store_true", help="so validar o CSV, sem gravar")
    args = parser.parse_args()

    from src.importers.x_analytics import import_x_analytics

    import_x_analytics(
        args.csv,
        args.profile,
        followers=args.followers,
        dayfirst=args.dayfirst,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
