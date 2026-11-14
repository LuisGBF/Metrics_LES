from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from src.db import (
    DB_PATH,
    add_account_snapshot,
    add_audience,
    add_breakdown,
    add_metric_snapshot,
    connect,
    upsert_post,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def seed_youtube(conn, n: int = 100, days: int = 240, seed: int = 42) -> None:
    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)
    followers = 2400.0
    add_account_snapshot(conn, "youtube", followers=int(followers), posts_count=n)

    hour_weight = np.array(
        [0.2, 0.1, 0.1, 0.1, 0.2, 0.4, 0.8, 1.4, 1.8, 1.6, 1.4, 1.5,
         1.8, 2.0, 2.1, 2.2, 2.4, 2.8, 3.4, 4.0, 4.3, 3.9, 2.6, 1.2]
    )
    hour_weight = hour_weight / hour_weight.sum()
    weekday_weight = np.array([1.0, 1.05, 1.1, 1.15, 1.25, 0.9, 0.75])
    weekday_weight = weekday_weight / weekday_weight.sum()

    for i in range(n):
        offset_days = float(rng.uniform(0, days))
        posted = now - timedelta(days=offset_days, hours=float(rng.uniform(0, 23)))
        local_hour = int((posted - timedelta(hours=3)).hour)
        weekday = (posted - timedelta(hours=3)).weekday()
        is_short = rng.random() < 0.55
        fmt = "short" if is_short else "video"

        quality = float(np.clip(rng.lognormal(0.0, 0.55), 0.2, 5.0))
        seasonal = 0.75 + 1.1 * hour_weight[local_hour] * 24 * weekday_weight[weekday] * 7
        dip = 0.45 if offset_days < 12 else 1.0
        views = int(followers * (0.35 if is_short else 0.6) * seasonal * quality * dip)

        likes = int(views * rng.uniform(0.02, 0.06))
        comments = int(views * rng.uniform(0.001, 0.004))
        shares = int(views * rng.uniform(0.0005, 0.002))

        vid = f"DEMO{i:04d}"
        upsert_post(
            conn,
            {
                "id": f"youtube:{vid}",
                "platform": "youtube",
                "platform_id": vid,
                "posted_at": _iso(posted),
                "format": fmt,
                "title": f"Video de exemplo {i + 1}",
                "text": "Conteudo sintetico gerado para demonstracao.",
                "url": f"https://youtu.be/{vid}",
                "duration_s": int(rng.integers(30, 60)) if is_short else int(rng.integers(300, 1200)),
                "extra": {"demo": True},
            },
        )
        add_metric_snapshot(
            conn,
            f"youtube:{vid}",
            collected_at=_iso(min(now, posted + timedelta(days=7))),
            views=views,
            impressions=int(views * rng.uniform(6, 12)),
            likes=likes,
            comments=comments,
            shares=shares,
            watch_time_s=int(views * rng.uniform(20, 90)),
            avg_view_duration_s=float(rng.uniform(15, 80)),
            avg_view_percentage=float(rng.uniform(25, 60)),
            ctr=float(rng.uniform(0.02, 0.09)),
            subscribers_gained=int(views * 0.004),
            subscribers_lost=int(views * 0.0005),
            followers=int(followers),
            raw={"demo": True},
        )
        add_breakdown(
            conn,
            f"youtube:{vid}",
            "insightTrafficSourceType",
            {
                "BROWSE": views * (0.18 if offset_days < 12 else 0.30),
                "SUGGESTED_VIDEO": views * (0.14 if offset_days < 12 else 0.26),
                "YT_SEARCH": views * 0.22,
                "EXTERNAL": views * 0.08,
                "OTHER": views * (0.38 if offset_days < 12 else 0.14),
            },
        )
        add_breakdown(
            conn,
            f"youtube:{vid}",
            "subscribedStatus",
            {
                "SUBSCRIBED": views * (0.55 if offset_days < 12 else 0.35),
                "NOT_SUBSCRIBED": views * (0.45 if offset_days < 12 else 0.65),
            },
        )

    add_audience(conn, "youtube", "hour", {str(h): float(w) for h, w in enumerate(hour_weight)})
    add_audience(conn, "youtube", "weekday", {str(d): float(w) for d, w in enumerate(weekday_weight)})


def seed_x(conn, n: int = 80, days: int = 240, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    now = datetime.now(timezone.utc)
    followers = 1500
    add_account_snapshot(conn, "x", followers=followers, following=420, posts_count=n + 900)

    for i in range(n):
        offset_days = float(rng.uniform(0, days))
        posted = now - timedelta(days=offset_days, hours=float(rng.uniform(0, 23)))
        local_hour = int((posted - timedelta(hours=3)).hour)
        peak = 1.0 + 0.85 * np.exp(-((local_hour - 21) ** 2) / 8) + 0.4 * np.exp(-((local_hour - 12) ** 2) / 6)
        quality = float(np.clip(rng.lognormal(0.0, 0.6), 0.2, 4.5))
        impressions = int(followers * 0.9 * peak * quality)
        likes = int(impressions * rng.uniform(0.008, 0.03))
        replies = int(impressions * rng.uniform(0.0005, 0.003))
        reposts = int(impressions * rng.uniform(0.0005, 0.004))
        bookmarks = int(impressions * rng.uniform(0.0005, 0.002))

        tweet_id = f"DEMO{i:04d}"
        upsert_post(
            conn,
            {
                "id": f"x:{tweet_id}",
                "platform": "x",
                "platform_id": tweet_id,
                "posted_at": _iso(posted),
                "format": "tweet",
                "title": None,
                "text": f"Post de exemplo {i + 1}",
                "url": f"https://x.com/exemplo/status/{tweet_id}",
                "duration_s": None,
                "extra": {"demo": True},
            },
        )
        add_metric_snapshot(
            conn,
            f"x:{tweet_id}",
            collected_at=_iso(min(now, posted + timedelta(days=3))),
            views=impressions,
            impressions=impressions,
            likes=likes,
            comments=replies,
            shares=reposts,
            saves=bookmarks,
            followers=followers,
            raw={"demo": True},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria o banco e opcionalmente dados de demonstracao.")
    parser.add_argument("--demo", action="store_true", help="insere dados sinteticos para visualizar o painel")
    parser.add_argument("--reset", action="store_true", help="apaga o banco atual antes de recriar")
    args = parser.parse_args()

    if args.reset and DB_PATH.exists():
        DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = DB_PATH.with_name(DB_PATH.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        print(f"[db] banco removido: {DB_PATH}")

    conn = connect()
    print(f"[db] pronto em {DB_PATH}")

    if args.demo:
        seed_youtube(conn)
        seed_x(conn)
        conn.commit()
        print("[db] dados de demonstracao inseridos (youtube + x)")
    conn.close()


if __name__ == "__main__":
    main()
