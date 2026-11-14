from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .frame import WEEKDAYS


def slot_table(
    posts: pd.DataFrame,
    target: str = "engagement_rate",
    min_posts: int = 2,
    prior_weight: float = 5.0,
    n_boot: int = 1000,
    seed: int = 7,
) -> pd.DataFrame:
    """Desempenho por dia da semana x hora, com encolhimento para a media global.

    O encolhimento evita que um unico post de sucesso num horario vazio seja
    tratado como "o melhor horario". O intervalo vem de bootstrap.
    """
    columns = [
        "weekday",
        "weekday_name",
        "hour",
        "n",
        "mean",
        "shrunk",
        "ci_low",
        "ci_high",
    ]
    d = posts.dropna(subset=[target]).copy()
    if d.empty:
        return pd.DataFrame(columns=columns)

    global_mean = float(d[target].mean())
    rng = np.random.default_rng(seed)
    rows = []

    for (weekday, hour), group in d.groupby(["weekday", "hour"]):
        values = group[target].to_numpy(dtype=float)
        n = len(values)
        if n < min_posts:
            continue
        shrunk = (values.sum() + prior_weight * global_mean) / (n + prior_weight)
        samples = rng.choice(values, size=(n_boot, n), replace=True).mean(axis=1)
        rows.append(
            {
                "weekday": int(weekday),
                "weekday_name": WEEKDAYS[int(weekday)],
                "hour": int(hour),
                "n": n,
                "mean": float(values.mean()),
                "shrunk": float(shrunk),
                "ci_low": float(np.quantile(samples, 0.10)),
                "ci_high": float(np.quantile(samples, 0.90)),
            }
        )

    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows).sort_values(["shrunk", "n"], ascending=False).reset_index(drop=True)


def best_slots(slots: pd.DataFrame, top: int = 6) -> pd.DataFrame:
    """Ordena pelo limite inferior do intervalo: favorece horarios consistentes."""
    if slots.empty:
        return slots
    return slots.sort_values(["ci_low", "n"], ascending=False).head(top).reset_index(drop=True)


def audience_profile(conn: sqlite3.Connection, platform: str | None = None) -> dict[str, pd.Series]:
    """Distribuicao de audiencia ativa por hora e por dia da semana."""
    query = """
    SELECT platform, dimension, bucket, value, collected_at
    FROM audience_snapshots
    WHERE (:platform IS NULL OR platform = :platform)
    """
    raw = pd.read_sql_query(query, conn, params={"platform": platform})
    result: dict[str, pd.Series] = {}
    if raw.empty:
        return result

    latest = raw.sort_values("collected_at").groupby(["platform", "dimension"], as_index=False).last()

    for dimension, index in (("hour", range(24)), ("weekday", range(7))):
        subset = latest[latest["dimension"] == dimension]
        if subset.empty:
            continue
        series = subset.groupby("bucket")["value"].sum()
        series.index = series.index.astype(int)
        series = series.reindex(index).fillna(0.0)
        total = float(series.sum())
        if total > 0:
            result[dimension] = series / total
    return result


def audience_heatmap(profile: dict[str, pd.Series]) -> pd.DataFrame:
    """Combina hora e dia separadamente (independencia assumida, e uma estimativa)."""
    if "hour" not in profile or "weekday" not in profile:
        return pd.DataFrame()
    hours = profile["hour"].reindex(range(24)).fillna(0.0)
    weekdays = profile["weekday"].reindex(range(7)).fillna(0.0)
    matrix = np.outer(weekdays.to_numpy(), hours.to_numpy())
    total = matrix.sum()
    if total > 0:
        matrix = matrix / total
    return pd.DataFrame(matrix, index=WEEKDAYS, columns=[f"{h:02d}h" for h in range(24)])


def engagement_heatmap(posts: pd.DataFrame, target: str = "engagement_rate") -> pd.DataFrame:
    d = posts.dropna(subset=[target])
    if d.empty:
        return pd.DataFrame()
    matrix = d.pivot_table(index="weekday", columns="hour", values=target, aggfunc="mean")
    matrix = matrix.reindex(index=range(7), columns=range(24))
    matrix.index = [WEEKDAYS[i] for i in matrix.index]
    matrix.columns = [f"{h:02d}h" for h in matrix.columns]
    return matrix
