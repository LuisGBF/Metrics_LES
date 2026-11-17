from __future__ import annotations

import sqlite3

import pytest

from src.analysis.frame import load_posts
from src.analysis.timing import audience_heatmap, audience_profile
from src.db import add_audience, add_metric_snapshot, connect, upsert_post
from src.profiles import (
    find_profile,
    get_or_create_profile,
    list_profiles,
    normalize_handle,
)


def test_normalize_handle_accepts_urls_and_at() -> None:
    assert normalize_handle("x", "@Usuario") == "usuario"
    assert normalize_handle("x", "https://x.com/usuario/status/123") == "usuario"
    assert normalize_handle("youtube", "https://www.youtube.com/@Canal") == "canal"
    assert (
        normalize_handle("youtube", "https://youtube.com/channel/UCabcdefghijklmnopqrstuv")
        == "UCabcdefghijklmnopqrstuv"
    )


def test_youtube_channel_id_preserves_case() -> None:
    channel_id = "UCabcdefghijklmnopqrstuv"
    assert normalize_handle("youtube", channel_id) == channel_id


def test_profile_upsert_is_idempotent(tmp_path) -> None:
    conn = connect(tmp_path / "profiles.db")
    first = get_or_create_profile(conn, "x", "@Canal", mode="public", label="Um")
    second = get_or_create_profile(conn, "x", "canal", mode="owned", label="Dois")
    assert first["id"] == second["id"] == "x:canal"
    assert second["mode"] == "owned"
    assert second["label"] == "Dois"

    found = find_profile(conn, "x", "https://x.com/canal")
    assert found is not None and found["id"] == "x:canal"
    conn.close()


def test_owned_mode_does_not_regress(tmp_path) -> None:
    conn = connect(tmp_path / "profiles.db")
    get_or_create_profile(conn, "youtube", "canal", mode="owned")
    again = get_or_create_profile(conn, "youtube", "canal", mode="public")
    assert again["mode"] == "owned"
    conn.close()


def test_load_posts_filters_by_profile(tmp_path) -> None:
    conn = connect(tmp_path / "posts.db")
    alpha = get_or_create_profile(conn, "x", "alpha")
    beta = get_or_create_profile(conn, "x", "beta")
    for profile, post_id in ((alpha, "1"), (beta, "2")):
        upsert_post(
            conn,
            {
                "id": f"x:{post_id}",
                "profile_id": profile["id"],
                "platform": "x",
                "platform_id": post_id,
                "posted_at": "2026-01-01T12:00:00+00:00",
                "format": "tweet",
                "text": f"post {post_id}",
            },
        )
        add_metric_snapshot(conn, f"x:{post_id}", views=100, likes=10, followers=1000)
    conn.commit()

    assert len(load_posts(conn)) == 2
    only_alpha = load_posts(conn, "x", alpha["id"])
    assert list(only_alpha["id"]) == ["x:1"]
    assert only_alpha["profile_handle"].iloc[0] == "alpha"
    conn.close()


def test_audience_profile_keeps_all_buckets(tmp_path) -> None:
    conn = connect(tmp_path / "audience.db")
    profile = get_or_create_profile(conn, "youtube", "canal")
    add_audience(
        conn, "youtube", "hour", {str(hour): 1.0 for hour in range(24)}, profile_id=profile["id"]
    )
    add_audience(
        conn, "youtube", "weekday", {str(day): 1.0 for day in range(7)}, profile_id=profile["id"]
    )
    conn.commit()

    result = audience_profile(conn, "youtube", profile["id"])
    assert result["hour"].sum() == pytest.approx(1.0)
    assert int((result["hour"] > 0).sum()) == 24

    heat = audience_heatmap(result)
    assert heat.shape == (7, 24)
    assert heat.to_numpy().sum() == pytest.approx(1.0)
    conn.close()


def test_migration_from_legacy_schema(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE posts (
            id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_id TEXT NOT NULL,
            posted_at TEXT NOT NULL, format TEXT, title TEXT, text TEXT, url TEXT,
            duration_s INTEGER, extra TEXT, first_seen_at TEXT
        );
        CREATE TABLE account_snapshots (
            platform TEXT NOT NULL, collected_at TEXT NOT NULL, followers INTEGER,
            following INTEGER, posts_count INTEGER, extra TEXT,
            PRIMARY KEY (platform, collected_at)
        );
        CREATE TABLE audience_snapshots (
            platform TEXT NOT NULL, collected_at TEXT NOT NULL, dimension TEXT NOT NULL,
            bucket TEXT NOT NULL, value REAL,
            PRIMARY KEY (platform, collected_at, dimension, bucket)
        );
        """
    )
    legacy.execute(
        "INSERT INTO posts VALUES ('x:1','x','1','2026-01-01T12:00:00+00:00','tweet','t',NULL,NULL,NULL,NULL,NULL)"
    )
    legacy.execute(
        "INSERT INTO account_snapshots VALUES ('x','2026-01-02T00:00:00+00:00',1500,10,5,NULL)"
    )
    legacy.execute(
        "INSERT INTO audience_snapshots VALUES ('youtube','2026-01-02T00:00:00+00:00','hour','9',0.5)"
    )
    legacy.commit()
    legacy.close()

    conn = connect(path)
    posts = load_posts(conn)
    assert posts["profile_id"].iloc[0] == "x:legacy"

    account = conn.execute("SELECT * FROM account_snapshots").fetchone()
    assert account["profile_id"] == "x:legacy"
    audience = conn.execute("SELECT * FROM audience_snapshots").fetchone()
    assert audience["profile_id"] == "youtube:legacy"

    ids = {row["id"] for row in list_profiles(conn)}
    assert {"x:legacy", "youtube:legacy"} <= ids
    conn.close()
