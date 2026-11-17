from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import connect
from src.profiles import PLATFORM_LABELS, delete_profile, get_or_create_profile, list_profiles


def cmd_list() -> None:
    conn = connect()
    rows = list_profiles(conn)
    conn.close()
    if not rows:
        print("Nenhum perfil cadastrado. Rode scripts/collect.py para criar um.")
        return

    header = f"{'ID':<32} {'PLATAFORMA':<10} {'MODO':<8} {'POSTS':>6} {'SEGUIDORES':>11}  LABEL"
    print(header)
    print("-" * len(header))
    for row in rows:
        followers = row["followers"] if row["followers"] is not None else "-"
        print(
            f"{row['id']:<32} {PLATFORM_LABELS.get(row['platform'], row['platform']):<10} "
            f"{row['mode']:<8} {row['posts']:>6} {str(followers):>11}  {row['label'] or ''}"
        )


def cmd_add(args: argparse.Namespace) -> None:
    conn = connect()
    profile = get_or_create_profile(
        conn,
        args.platform,
        args.handle,
        mode=args.mode,
        label=args.label,
    )
    conn.commit()
    conn.close()
    print(f"[perfil] {profile['id']} ({profile['mode']}) pronto.")


def cmd_remove(args: argparse.Namespace) -> None:
    conn = connect()
    if not args.yes:
        answer = input(f"Remover {args.profile_id} e todos os posts coletados dele? [s/N] ")
        if answer.strip().lower() not in {"s", "sim", "y", "yes"}:
            print("cancelado")
            conn.close()
            return
    removed = delete_profile(conn, args.profile_id)
    conn.commit()
    conn.close()
    print(f"[perfil] {args.profile_id} removido." if removed else "[perfil] nao encontrado.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gerencia os perfis monitorados.")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("list", help="lista os perfis cadastrados")

    add = subs.add_parser("add", help="cadastra um perfil sem coletar")
    add.add_argument("platform", choices=["youtube", "x"])
    add.add_argument("handle", help="@handle, URL ou ID")
    add.add_argument("--mode", choices=["owned", "public"], default="public")
    add.add_argument("--label", default=None)
    add.set_defaults(func=cmd_add)

    remove = subs.add_parser("remove", help="remove um perfil e seus dados")
    remove.add_argument("profile_id", help="ID exibido em 'list' (ex.: x:usuario)")
    remove.add_argument("--yes", action="store_true", help="nao pedir confirmacao")
    remove.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    if args.command == "list":
        cmd_list()
    else:
        args.func(args)


if __name__ == "__main__":
    main()
