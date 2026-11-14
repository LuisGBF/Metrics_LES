from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from ..config import TZ
from ..db import connect

WEEKDAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

QUERY = """
SELECT p.id, p.platform, p.platform_id, p.posted_at, p.format, p.title, p.text, p.url,
       p.duration_s, m.collected_at, m.views, m.impressions, m.likes, m.comments,
       m.shares, m.saves, m.watch_time_s, m.avg_view_duration_s, m.avg_view_percentage,
       m.ctr, m.subscribers_gained, m.subscribers_lost, m.followers
FROM posts p
LEFT JOIN metric_snapshots m
       ON m.post_id = p.id
      AND m.collected_at = (SELECT MAX(collected_at) FROM metric_snapshots WHERE post_id = p.id)
WHERE (:platform IS NULL OR p.platform = :platform)
ORDER BY p.posted_at
"""


def add_derived(posts: pd.DataFrame, tz: str = TZ) -> pd.DataFrame:
    if posts.empty:
        return posts

    d = posts.copy()
    d["posted_at"] = pd.to_datetime(d["posted_at"], utc=True, errors="coerce")
    d = d.dropna(subset=["posted_at"])

    local = d["posted_at"].dt.tz_convert(ZoneInfo(tz))
    d["posted_at_local"] = local.dt.tz_localize(None)
    d["date"] = local.dt.date
    d["weekday"] = local.dt.day_of_week
    d["weekday_name"] = d["weekday"].map(lambda i: WEEKDAYS[int(i)])
    d["hour"] = local.dt.hour

    for column in ("views", "impressions", "likes", "comments", "shares", "saves"):
        if column not in d:
            d[column] = 0
        d[column] = pd.to_numeric(d[column], errors="coerce").fillna(0)

    d["reach"] = d["views"].where(d["views"] > 0, d["impressions"])
    d["engagement"] = d[["likes", "comments", "shares", "saves"]].sum(axis=1)
    denominator = d["reach"].where(d["reach"] > 0)
    d["engagement_rate"] = d["engagement"] / denominator
    d["reach_per_1k_followers"] = d["reach"] / d["followers"].where(d["followers"] > 0) * 1000
    return d.sort_values("posted_at").reset_index(drop=True)


def load_posts(conn: sqlite3.Connection | None = None, platform: str | None = None) -> pd.DataFrame:
    own = conn is None
    conn = conn or connect()
    try:
        raw = pd.read_sql_query(QUERY, conn, params={"platform": platform})
    finally:
        if own:
            conn.close()
    return add_derived(raw)
