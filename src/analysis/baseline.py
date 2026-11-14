from __future__ import annotations

import numpy as np
import pandas as pd


def expected_vs_actual(
    posts: pd.DataFrame,
    target: str = "engagement_rate",
    min_history: int = 3,
    group_cols: tuple[str, ...] = ("platform", "format"),
) -> pd.DataFrame:
    """Compara cada post com a mediana dos posts anteriores do mesmo formato.

    Usa apenas informacao disponivel antes do post (expanding + shift), para nao
    vazar o futuro. Sem historico suficiente, cai para a mediana global anterior.
    """
    d = posts.dropna(subset=[target]).sort_values("posted_at").reset_index(drop=True).copy()
    if d.empty:
        return d

    d["_target"] = d[target].astype(float)
    grouped = d.groupby(list(group_cols))["_target"]
    prev_count = grouped.transform(lambda s: s.shift(1).expanding().count())
    prev_median = grouped.transform(lambda s: s.shift(1).expanding().median())
    overall_median = d["_target"].shift(1).expanding().median()

    d["expected"] = prev_median.where(prev_count >= min_history, overall_median)
    d["history_n"] = prev_count.fillna(0).astype(int)
    d["residual"] = d["_target"] - d["expected"]
    d["ratio"] = d["_target"] / d["expected"] - 1.0

    residuals = d["residual"].dropna()
    if len(residuals) >= 3:
        center = float(residuals.median())
        mad = float((residuals - center).abs().median())
        scale = 1.4826 * mad if mad > 0 else float(residuals.std(ddof=0) or 0)
    else:
        center, scale = 0.0, 0.0

    d["z"] = (d["residual"] - center) / scale if scale > 0 else 0.0
    d["status"] = np.select(
        [d["ratio"] > 0.25, d["ratio"] < -0.25],
        ["acima do esperado", "abaixo do esperado"],
        default="dentro do esperado",
    )
    d.loc[d["expected"].isna(), "status"] = "sem historico"
    return d.drop(columns=["_target"])


def summary(posts: pd.DataFrame, target: str = "engagement_rate") -> dict[str, float | int]:
    d = expected_vs_actual(posts, target=target)
    valid = d.dropna(subset=["expected"])
    if valid.empty:
        return {"posts": len(d), "comparaveis": 0, "abaixo": 0, "acima": 0, "delta_mediano": 0.0}
    return {
        "posts": len(d),
        "comparaveis": int(len(valid)),
        "abaixo": int((valid["status"] == "abaixo do esperado").sum()),
        "acima": int((valid["status"] == "acima do esperado").sum()),
        "delta_mediano": float(valid["ratio"].median()),
    }
