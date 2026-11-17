from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import DB_PATH

PROFILES_DDL = """
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    handle      TEXT NOT NULL,
    label       TEXT,
    mode        TEXT NOT NULL DEFAULT 'owned',
    external_id TEXT,
    created_at  TEXT NOT NULL
)
"""

POSTS_DDL = """
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    platform      TEXT NOT NULL,
    platform_id   TEXT NOT NULL,
    posted_at     TEXT NOT NULL,
    format        TEXT,
    title         TEXT,
    text          TEXT,
    url           TEXT,
    duration_s    INTEGER,
    extra         TEXT,
    first_seen_at TEXT
)
"""

METRICS_DDL = """
CREATE TABLE IF NOT EXISTS metric_snapshots (
    post_id               TEXT NOT NULL,
    collected_at          TEXT NOT NULL,
    views                 INTEGER,
    impressions           INTEGER,
    engagements           INTEGER,
    likes                 INTEGER,
    comments              INTEGER,
    shares                INTEGER,
    saves                 INTEGER,
    watch_time_s          REAL,
    avg_view_duration_s   REAL,
    avg_view_percentage   REAL,
    ctr                   REAL,
    subscribers_gained    INTEGER,
    subscribers_lost      INTEGER,
    followers             INTEGER,
    raw                   TEXT,
    PRIMARY KEY (post_id, collected_at)
)
"""

BREAKDOWNS_DDL = """
CREATE TABLE IF NOT EXISTS breakdown_snapshots (
    post_id      TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    dimension    TEXT NOT NULL,
    bucket       TEXT NOT NULL,
    value        REAL,
    PRIMARY KEY (post_id, collected_at, dimension, bucket)
)
"""

AUDIENCE_DDL = """
CREATE TABLE IF NOT EXISTS audience_snapshots (
    profile_id   TEXT NOT NULL,
    platform     TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    dimension    TEXT NOT NULL,
    bucket       TEXT NOT NULL,
    value        REAL,
    PRIMARY KEY (profile_id, collected_at, dimension, bucket)
)
"""

ACCOUNT_DDL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    profile_id   TEXT NOT NULL,
    platform     TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    followers    INTEGER,
    following    INTEGER,
    posts_count  INTEGER,
    extra        TEXT,
    PRIMARY KEY (profile_id, collected_at)
)
"""

SCHEMA = ";\n\n".join(
    [PROFILES_DDL, POSTS_DDL, METRICS_DDL, BREAKDOWNS_DDL, AUDIENCE_DDL, ACCOUNT_DDL]
)

# Versoes sem "IF NOT EXISTS", usadas ao reconstruir tabelas antigas (sem profile_id).
AUDIENCE_DDL_NEW = AUDIENCE_DDL.replace("IF NOT EXISTS ", "")
ACCOUNT_DDL_NEW = ACCOUNT_DDL.replace("IF NOT EXISTS ", "")

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_profiles_handle ON profiles(platform, handle);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_profile ON posts(profile_id);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_account_profile ON account_snapshots(profile_id);
CREATE INDEX IF NOT EXISTS idx_audience_profile ON audience_snapshots(profile_id);
"""

METRIC_COLUMNS = (
    "views",
    "impressions",
    "engagements",
    "likes",
    "comments",
    "shares",
    "saves",
    "watch_time_s",
    "avg_view_duration_s",
    "avg_view_percentage",
    "ctr",
    "subscribers_gained",
    "subscribers_lost",
    "followers",
    "raw",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Migra bancos antigos (sem profile_id) sem perder dados coletados."""
    if "profile_id" not in _table_columns(conn, "posts"):
        conn.execute("ALTER TABLE posts ADD COLUMN profile_id TEXT")

    if "engagements" not in _table_columns(conn, "metric_snapshots"):
        conn.execute("ALTER TABLE metric_snapshots ADD COLUMN engagements INTEGER")

    if "profile_id" not in _table_columns(conn, "account_snapshots"):
        conn.executescript(
            f"""
            ALTER TABLE account_snapshots RENAME TO _account_snapshots_old;
            {ACCOUNT_DDL_NEW};
            INSERT INTO account_snapshots
                (profile_id, platform, collected_at, followers, following, posts_count, extra)
            SELECT platform || ':legacy', platform, collected_at, followers, following,
                   posts_count, extra
            FROM _account_snapshots_old;
            DROP TABLE _account_snapshots_old;
            """
        )

    if "profile_id" not in _table_columns(conn, "audience_snapshots"):
        conn.executescript(
            f"""
            ALTER TABLE audience_snapshots RENAME TO _audience_snapshots_old;
            {AUDIENCE_DDL_NEW};
            INSERT INTO audience_snapshots
                (profile_id, platform, collected_at, dimension, bucket, value)
            SELECT platform || ':legacy', platform, collected_at, dimension, bucket, value
            FROM _audience_snapshots_old;
            DROP TABLE _audience_snapshots_old;
            """
        )

    orphan_platforms = {
        row["platform"]
        for row in conn.execute(
            """
            SELECT platform FROM posts
             WHERE profile_id IS NULL OR profile_id = ''
                OR profile_id NOT IN (SELECT id FROM profiles)
            UNION
            SELECT platform FROM account_snapshots
             WHERE profile_id IS NULL OR profile_id = ''
                OR profile_id NOT IN (SELECT id FROM profiles)
            UNION
            SELECT platform FROM audience_snapshots
             WHERE profile_id IS NULL OR profile_id = ''
                OR profile_id NOT IN (SELECT id FROM profiles)
            """
        )
    }
    for platform in orphan_platforms:
        profile_id = f"{platform}:legacy"
        conn.execute(
            """
            INSERT OR IGNORE INTO profiles (id, platform, handle, label, mode, created_at)
            VALUES (?, ?, 'legacy', ?, 'owned', ?)
            """,
            (profile_id, platform, f"{platform.upper()} · dados antigos", now_iso()),
        )
        for table in ("posts", "account_snapshots", "audience_snapshots"):
            conn.execute(
                f"UPDATE {table} SET profile_id = ? WHERE platform = ? "
                "AND (profile_id IS NULL OR profile_id = '')",
                (profile_id, platform),
            )

    conn.executescript(INDEXES)
    conn.commit()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def default_profile_id(conn: sqlite3.Connection, platform: str) -> str:
    """Perfil mais antigo da plataforma (usado quando o chamador nao informa um)."""
    row = conn.execute(
        "SELECT id FROM profiles WHERE platform = ? ORDER BY created_at LIMIT 1",
        (platform,),
    ).fetchone()
    return row["id"] if row else f"{platform}:legacy"


def upsert_post(conn: sqlite3.Connection, post: Mapping[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO posts (id, profile_id, platform, platform_id, posted_at, format, title,
                           text, url, duration_s, extra, first_seen_at)
        VALUES (:id, :profile_id, :platform, :platform_id, :posted_at, :format, :title,
                :text, :url, :duration_s, :extra, :first_seen_at)
        ON CONFLICT(id) DO UPDATE SET
            profile_id   = excluded.profile_id,
            posted_at    = excluded.posted_at,
            format       = excluded.format,
            title        = excluded.title,
            text         = excluded.text,
            url          = excluded.url,
            duration_s   = excluded.duration_s,
            extra        = excluded.extra
        """,
        {
            "id": post["id"],
            "profile_id": post.get("profile_id") or default_profile_id(conn, post["platform"]),
            "platform": post["platform"],
            "platform_id": post["platform_id"],
            "posted_at": post["posted_at"],
            "format": post.get("format"),
            "title": post.get("title"),
            "text": post.get("text"),
            "url": post.get("url"),
            "duration_s": post.get("duration_s"),
            "extra": _json(post.get("extra")),
            "first_seen_at": post.get("first_seen_at") or now_iso(),
        },
    )


def add_metric_snapshot(
    conn: sqlite3.Connection,
    post_id: str,
    collected_at: str | None = None,
    **metrics: Any,
) -> None:
    row = {column: None for column in METRIC_COLUMNS}
    for key, value in metrics.items():
        if key in row:
            row[key] = _json(value) if key == "raw" else value

    conn.execute(
        f"""
        INSERT OR REPLACE INTO metric_snapshots
            (post_id, collected_at, {", ".join(METRIC_COLUMNS)})
        VALUES (?, ?, {", ".join("?" for _ in METRIC_COLUMNS)})
        """,
        (post_id, collected_at or now_iso(), *[row[c] for c in METRIC_COLUMNS]),
    )


def add_breakdown(
    conn: sqlite3.Connection,
    post_id: str,
    dimension: str,
    buckets: Mapping[str, float],
    collected_at: str | None = None,
) -> None:
    stamp = collected_at or now_iso()
    conn.executemany(
        """
        INSERT OR REPLACE INTO breakdown_snapshots
            (post_id, collected_at, dimension, bucket, value)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(post_id, stamp, dimension, str(bucket), float(value)) for bucket, value in buckets.items()],
    )


def add_audience(
    conn: sqlite3.Connection,
    platform: str,
    dimension: str,
    buckets: Mapping[str, float],
    profile_id: str | None = None,
    collected_at: str | None = None,
) -> None:
    stamp = collected_at or now_iso()
    owner = profile_id or default_profile_id(conn, platform)
    conn.executemany(
        """
        INSERT OR REPLACE INTO audience_snapshots
            (profile_id, platform, collected_at, dimension, bucket, value)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (owner, platform, stamp, dimension, str(bucket), float(value))
            for bucket, value in buckets.items()
        ],
    )


def add_account_snapshot(
    conn: sqlite3.Connection,
    platform: str,
    profile_id: str | None = None,
    followers: int | None = None,
    following: int | None = None,
    posts_count: int | None = None,
    extra: Any = None,
    collected_at: str | None = None,
) -> None:
    owner = profile_id or default_profile_id(conn, platform)
    conn.execute(
        """
        INSERT OR REPLACE INTO account_snapshots
            (profile_id, platform, collected_at, followers, following, posts_count, extra)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (owner, platform, collected_at or now_iso(), followers, following, posts_count, _json(extra)),
    )
