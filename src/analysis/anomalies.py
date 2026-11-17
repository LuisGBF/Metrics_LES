from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .frame import load_posts

DISCLAIMER = (
    "Nenhuma plataforma expõe 'shadowban'. Estes sinais são heurísticos: "
    "indicam queda anômala de distribuição, não uma penalidade confirmada. "
    "Use como ponto de partida para investigar (conteúdo, violações, mudanças de formato)."
)


def detect_drops(
    posts: pd.DataFrame,
    metric: str = "reach",
    window: int = 6,
    z_threshold: float = -2.5,
    consecutive: int = 2,
) -> pd.DataFrame:
    """Detecta quedas de alcance com z-score robusto (mediana/MAD) sobre a serie anterior."""
    d = posts.dropna(subset=[metric]).sort_values("posted_at").reset_index(drop=True).copy()
    d["norm_metric"] = np.nan
    d["z"] = 0.0
    d["flag_drop"] = False
    d["alert"] = False
    if len(d) < 4:
        return d

    series = d[metric].astype(float)
    if "followers" in d and d["followers"].notna().any():
        normalized = series / d["followers"].where(d["followers"] > 0) * 1000
        series = normalized.fillna(series)
    d["norm_metric"] = series

    previous = series.shift(1)
    median = previous.rolling(window, min_periods=3).median()
    mad = previous.rolling(window, min_periods=3).apply(
        lambda values: np.median(np.abs(values - np.median(values))), raw=True
    )
    scale = 1.4826 * mad
    fallback = previous.rolling(window, min_periods=3).std()
    scale = scale.where(scale > 0, fallback)
    # Serie constante (MAD e desvio zero): usa 10% da mediana como piso de
    # sensibilidade, senao nenhuma queda seria detectada.
    floor = median.abs() * 0.10
    scale = scale.where(scale > floor, floor)
    scale = scale.where(scale > 0, 1e-9)
    z = (series - median) / scale
    d["z"] = z.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-6, 6)
    d["flag_drop"] = (d["z"] <= z_threshold) & series.notna()

    streak = d["flag_drop"].astype(int).rolling(consecutive, min_periods=consecutive).sum()
    d["alert"] = streak >= consecutive
    return d


def describe_drops(d: pd.DataFrame) -> dict[str, object]:
    alerts = d[d["alert"]]
    if alerts.empty:
        return {"alerts": 0, "status": "ok", "first": None, "last": None, "worst_z": None}
    return {
        "alerts": int(len(alerts)),
        "status": "atencao",
        "first": str(alerts["posted_at"].min()),
        "last": str(alerts["posted_at"].max()),
        "worst_z": float(alerts["z"].min()),
    }


def traffic_shares(
    conn: sqlite3.Connection,
    platform: str = "youtube",
    dimension: str = "insightTrafficSourceType",
    profile_id: str | None = None,
    last_n: int = 5,
    baseline_n: int = 20,
) -> pd.DataFrame:
    """Compara a fatia de cada origem de tráfego nos ultimos posts vs. posts anteriores."""
    query = """
    SELECT b.post_id, b.bucket, b.value, p.posted_at
    FROM breakdown_snapshots b
    JOIN posts p ON p.id = b.post_id
    JOIN (
        SELECT post_id, MAX(collected_at) AS collected_at
        FROM breakdown_snapshots
        WHERE dimension = :dimension
        GROUP BY post_id
    ) latest ON latest.post_id = b.post_id AND latest.collected_at = b.collected_at
    WHERE b.dimension = :dimension AND p.platform = :platform
      AND (:profile_id IS NULL OR p.profile_id = :profile_id)
    """
    raw = pd.read_sql_query(
        query,
        conn,
        params={"dimension": dimension, "platform": platform, "profile_id": profile_id},
    )
    if raw.empty:
        return pd.DataFrame(columns=["bucket", "share_recent", "share_baseline", "delta"])

    totals = raw.groupby("post_id")["value"].transform("sum")
    raw["share"] = raw["value"] / totals.where(totals > 0)
    raw["posted_at"] = pd.to_datetime(raw["posted_at"], utc=True, errors="coerce")

    ordered = raw.sort_values("posted_at")["post_id"].drop_duplicates().tolist()
    recent = set(ordered[-last_n:])
    baseline = set(ordered[-(last_n + baseline_n) : -last_n])
    if not baseline:
        baseline = set(ordered[:-last_n])

    def mean_share(ids: set[str]) -> pd.Series:
        subset = raw[raw["post_id"].isin(ids)]
        if subset.empty:
            return pd.Series(dtype=float)
        return subset.groupby("bucket")["share"].mean()

    recent_share = mean_share(recent)
    baseline_share = mean_share(baseline)
    if recent_share.empty or baseline_share.empty:
        return pd.DataFrame(columns=["bucket", "share_recent", "share_baseline", "delta"])

    table = pd.concat([recent_share.rename("share_recent"), baseline_share.rename("share_baseline")], axis=1)
    table = table.fillna(0.0)
    table["delta"] = table["share_recent"] - table["share_baseline"]
    return table.sort_values("share_recent", ascending=False).reset_index()


def shadowban_report(
    conn: sqlite3.Connection,
    platform: str = "youtube",
    profile_id: str | None = None,
) -> dict[str, object]:
    posts = load_posts(conn, platform, profile_id)
    if posts.empty:
        return {
            "score": 0,
            "verdict": "sem dados",
            "signals": [],
            "alerts": pd.DataFrame(),
            "disclaimer": DISCLAIMER,
        }

    drops = detect_drops(posts)
    drop_info = describe_drops(drops)
    signals: list[dict[str, object]] = []
    score = 0

    if drop_info["alerts"]:
        score += 40
        signals.append(
            {
                "sinal": "Queda consecutiva de alcance",
                "status": "forte",
                "detalhe": f"{drop_info['alerts']} posts em queda anômala (z min {drop_info['worst_z']:.1f})",
            }
        )
    else:
        signals.append(
            {
                "sinal": "Queda consecutiva de alcance",
                "status": "ok",
                "detalhe": "nenhuma sequencia anomala detectada",
            }
        )

    if platform == "youtube":
        non_sub = traffic_shares(conn, platform, dimension="subscribedStatus", profile_id=profile_id)
        if not non_sub.empty:
            row = non_sub[non_sub["bucket"] == "NOT_SUBSCRIBED"]
            if not row.empty:
                delta = float(row["delta"].iloc[0])
                if delta < -0.10:
                    score += 30
                    signals.append(
                        {
                            "sinal": "Alcance de nao inscritos",
                            "status": "forte",
                            "detalhe": f"caiu {abs(delta) * 100:.0f} p.p. vs. posts anteriores",
                        }
                    )
                else:
                    signals.append(
                        {
                            "sinal": "Alcance de nao inscritos",
                            "status": "ok",
                            "detalhe": f"variacao de {delta * 100:+.0f} p.p.",
                        }
                    )

        traffic = traffic_shares(
            conn, platform, dimension="insightTrafficSourceType", profile_id=profile_id
        )
        if not traffic.empty:
            keys = ["BROWSE", "SUGGESTED_VIDEO", "YT_SEARCH"]
            subset = traffic[traffic["bucket"].isin(keys)]
            delta = float(subset["delta"].sum()) if not subset.empty else 0.0
            if delta < -0.10:
                score += 25
                signals.append(
                    {
                        "sinal": "Distribuicao (navegacao/busca/sugestao)",
                        "status": "forte",
                        "detalhe": f"queda de {abs(delta) * 100:.0f} p.p. nas fontes de descoberta",
                    }
                )
            else:
                signals.append(
                    {
                        "sinal": "Distribuicao (navegacao/busca/sugestao)",
                        "status": "ok",
                        "detalhe": f"variacao de {delta * 100:+.0f} p.p.",
                    }
                )

    score = min(score, 100)
    if score < 25:
        verdict = "sem sinal relevante"
    elif score < 60:
        verdict = "sinal de atencao"
    else:
        verdict = "anomalia forte - vale investigar"

    return {
        "score": score,
        "verdict": verdict,
        "signals": signals,
        "alerts": drops[drops["alert"]],
        "disclaimer": DISCLAIMER,
    }
