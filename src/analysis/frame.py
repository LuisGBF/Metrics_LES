from __future__ import annotations

import sqlite3
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import TZ
from ..db import connect

WEEKDAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"]

QUERY = """
SELECT p.id, p.profile_id, p.platform, p.platform_id, p.posted_at, p.format, p.title,
       p.text, p.url, p.duration_s,
       pr.handle AS profile_handle, pr.label AS profile_label, pr.mode AS profile_mode,
       m.collected_at, m.views, m.impressions, m.engagements, m.likes, m.comments,
       m.shares, m.saves, m.watch_time_s, m.avg_view_duration_s, m.avg_view_percentage,
       m.ctr, m.subscribers_gained, m.subscribers_lost, m.followers
FROM posts p
LEFT JOIN profiles pr ON pr.id = p.profile_id
LEFT JOIN metric_snapshots m
       ON m.post_id = p.id
      AND m.collected_at = (SELECT MAX(collected_at) FROM metric_snapshots WHERE post_id = p.id)
WHERE (:platform IS NULL OR p.platform = :platform)
  AND (:profile_id IS NULL OR p.profile_id = :profile_id)
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

    for column in (
        "views",
        "impressions",
        "engagements",
        "likes",
        "comments",
        "shares",
        "saves",
        "followers",
    ):
        if column not in d:
            d[column] = 0
        d[column] = pd.to_numeric(d[column], errors="coerce").fillna(0)

    views = d["views"].where(d["views"] > 0)
    impressions = d["impressions"].where(d["impressions"] > 0)
    followers = d["followers"].where(d["followers"] > 0)

    # Perfis publicos do X nao expoem impressoes: usa seguidores como denominador
    # e registra a origem para deixar isso explicito no painel.
    d["reach_basis"] = np.where(
        views.notna(),
        "views",
        np.where(impressions.notna(), "impressions", np.where(followers.notna(), "followers", "sem dados")),
    )
    d["reach"] = views.fillna(impressions).fillna(followers)
    d.loc[d["reach_basis"] == "sem dados", "reach"] = np.nan

    # O X Analytics exportado informa "Engagements" (inclui cliques no perfil/link).
    # Quando existe, respeitamos o numero da plataforma; senao somamos as interacoes.
    interactions = d[["likes", "comments", "shares", "saves"]].sum(axis=1)
    reported = d["engagements"].where(d["engagements"] > 0)
    d["engagement"] = reported.fillna(interactions)
    denominator = d["reach"].where(d["reach"] > 0)
    d["engagement_rate"] = d["engagement"] / denominator
    d["reach_per_1k_followers"] = d["reach"] / followers * 1000
    return d.sort_values("posted_at").reset_index(drop=True)


def load_posts(
    conn: sqlite3.Connection | None = None,
    platform: str | None = None,
    profile_id: str | None = None,
) -> pd.DataFrame:
    own = conn is None
    conn = conn or connect()
    try:
        raw = pd.read_sql_query(
            QUERY, conn, params={"platform": platform, "profile_id": profile_id}
        )
    finally:
        if own:
            conn.close()
    return add_derived(raw)
