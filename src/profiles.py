from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import now_iso

PLATFORM_LABELS = {"youtube": "YouTube", "x": "X"}
VALID_MODES = ("owned", "public")

# IDs de canal do YouTube diferenciam maiusculas de minusculas (ex.: UCabc...).
YOUTUBE_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
_HOST_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be|x\.com|twitter\.com)/?",
    re.IGNORECASE,
)
_RESERVED_PATHS = {"channel", "c", "user", "handle"}


def normalize_handle(platform: str, handle: str) -> str:
    """Aceita `@handle`, URL completa, ID de canal ou usuario e devolve o identificador."""
    platform = platform.lower().strip()
    if platform not in PLATFORM_LABELS:
        raise ValueError(f"Plataforma desconhecida: {platform!r}")

    value = (handle or "").strip().strip("/")
    value = _HOST_RE.sub("", value)
    segments = [segment for segment in value.split("/") if segment]
    if not segments:
        raise ValueError("Informe um @handle, URL de perfil ou ID de canal.")

    if platform == "youtube":
        if segments[0].lower() in _RESERVED_PATHS and len(segments) > 1:
            segments = segments[1:]
        if YOUTUBE_CHANNEL_ID.fullmatch(segments[0]):
            return segments[0]

    return segments[0].lstrip("@").strip().lower()


def make_profile_id(platform: str, handle: str) -> str:
    return f"{platform.lower()}:{handle}"


def get_or_create_profile(
    conn: sqlite3.Connection,
    platform: str,
    handle: str,
    mode: str | None = None,
    external_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Registra (ou atualiza) um perfil. `mode` nunca regride de owned para public."""
    platform = platform.lower().strip()
    if mode is not None and mode not in VALID_MODES:
        raise ValueError(f"Modo invalido: {mode!r}. Use 'owned' ou 'public'.")
    normalized = normalize_handle(platform, handle)
    profile_id = make_profile_id(platform, normalized)

    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO profiles (id, platform, handle, label, mode, external_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (profile_id, platform, normalized, label, mode or "owned", external_id, now_iso()),
        )
    else:
        # Perfil que ja teve coleta completa continua marcado como owned.
        effective_mode = "owned" if row["mode"] == "owned" else mode
        conn.execute(
            """
            UPDATE profiles
               SET label       = COALESCE(?, label),
                   mode        = COALESCE(?, mode),
                   external_id = COALESCE(?, external_id)
             WHERE id = ?
            """,
            (label, effective_mode, external_id, profile_id),
        )

    return dict(conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone())


def get_profile(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def find_profile(conn: sqlite3.Connection, platform: str, handle: str) -> dict[str, Any] | None:
    normalized = normalize_handle(platform, handle)
    row = conn.execute(
        "SELECT * FROM profiles WHERE platform = ? AND handle = ?",
        (platform.lower().strip(), normalized),
    ).fetchone()
    return dict(row) if row else None


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT p.id, p.platform, p.handle, p.label, p.mode, p.external_id, p.created_at,
               (SELECT a.followers FROM account_snapshots a
                 WHERE a.profile_id = p.id
                 ORDER BY a.collected_at DESC LIMIT 1) AS followers,
               (SELECT COUNT(*) FROM posts po WHERE po.profile_id = p.id) AS posts
          FROM profiles p
         ORDER BY p.platform, p.handle
        """
    )
    return [dict(row) for row in rows]


def delete_profile(conn: sqlite3.Connection, profile_id: str) -> bool:
    exists = conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not exists:
        return False
    conn.execute(
        "DELETE FROM metric_snapshots WHERE post_id IN (SELECT id FROM posts WHERE profile_id = ?)",
        (profile_id,),
    )
    conn.execute(
        "DELETE FROM breakdown_snapshots WHERE post_id IN (SELECT id FROM posts WHERE profile_id = ?)",
        (profile_id,),
    )
    conn.execute("DELETE FROM posts WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM account_snapshots WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM audience_snapshots WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    return True
