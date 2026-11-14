from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
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
);
CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    post_id               TEXT NOT NULL,
    collected_at          TEXT NOT NULL,
    views                 INTEGER,
    impressions           INTEGER,
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
);

CREATE TABLE IF NOT EXISTS breakdown_snapshots (
    post_id      TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    dimension    TEXT NOT NULL,
    bucket       TEXT NOT NULL,
    value        REAL,
    PRIMARY KEY (post_id, collected_at, dimension, bucket)
);

CREATE TABLE IF NOT EXISTS audience_snapshots (
    platform     TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    dimension    TEXT NOT NULL,
    bucket       TEXT NOT NULL,
    value        REAL,
    PRIMARY KEY (platform, collected_at, dimension, bucket)
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    platform     TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    followers    INTEGER,
    following    INTEGER,
    posts_count  INTEGER,
    extra        TEXT,
    PRIMARY KEY (platform, collected_at)
);
"""

METRIC_COLUMNS = (
    "views",
    "impressions",
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


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def upsert_post(conn: sqlite3.Connection, post: Mapping[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO posts (id, platform, platform_id, posted_at, format, title, text,
                           url, duration_s, extra, first_seen_at)
        VALUES (:id, :platform, :platform_id, :posted_at, :format, :title, :text,
                :url, :duration_s, :extra, :first_seen_at)
        ON CONFLICT(id) DO UPDATE SET
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
    collected_at: str | None = None,
) -> None:
    stamp = collected_at or now_iso()
    conn.executemany(
        """
        INSERT OR REPLACE INTO audience_snapshots
            (platform, collected_at, dimension, bucket, value)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(platform, stamp, dimension, str(bucket), float(value)) for bucket, value in buckets.items()],
    )


def add_account_snapshot(
    conn: sqlite3.Connection,
    platform: str,
    followers: int | None = None,
    following: int | None = None,
    posts_count: int | None = None,
    extra: Any = None,
    collected_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO account_snapshots
            (platform, collected_at, followers, following, posts_count, extra)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (platform, collected_at or now_iso(), followers, following, posts_count, _json(extra)),
    )
