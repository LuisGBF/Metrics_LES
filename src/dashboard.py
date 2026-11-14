from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from src.analysis.anomalies import shadowban_report, traffic_shares
from src.analysis.baseline import expected_vs_actual
from src.analysis.frame import load_posts
from src.analysis.timing import (
    WEEKDAYS,
    audience_heatmap,
    audience_profile,
    best_slots,
    engagement_heatmap,
    slot_table,
)
from src.db import connect

st.set_page_config(page_title="Painel de Redes", page_icon="📊", layout="wide")


@st.cache_data(ttl=60)
def _posts(platform: str | None) -> pd.DataFrame:
    return load_posts(connect(), platform)


@st.cache_data(ttl=60)
def _profile(platform: str | None) -> dict:
    return audience_profile(connect(), platform)


@st.cache_data(ttl=60)
def _report(platform: str) -> dict:
    return shadowban_report(connect(), platform)


@st.cache_data(ttl=60)
def _traffic(platform: str, dimension: str) -> pd.DataFrame:
    return traffic_shares(connect(), platform, dimension)


st.title("📊 Painel de desempenho")
st.caption("Dados das APIs oficiais · análise local · nada sai da sua máquina")


def _post_label(row: pd.Series, size: int = 55) -> str:
    for key in ("title", "text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:size]
    return str(row.get("id", "post"))

with st.sidebar:
    st.header("Filtros")
    platform_label = st.selectbox("Plataforma", ["Todas", "YouTube", "X"])
    platform = {"Todas": None, "YouTube": "youtube", "X": "x"}[platform_label]
    target_label = st.selectbox("Métrica de comparação", ["Taxa de engajamento", "Alcance"])
    target = "engagement_rate" if target_label.startswith("Taxa") else "reach"
    st.divider()
    st.caption("Atualize os dados com `python scripts/collect.py all`.")

posts = _posts(platform)
if posts.empty:
    st.info(
        "Ainda não há dados. Para ver o painel funcionando agora, rode:\n\n"
        "`python scripts/init_db.py --demo`\n\n"
        "Para usar seus dados reais: `python scripts/collect.py youtube` e `python scripts/collect.py x`."
    )
    st.stop()

if posts["id"].str.contains("DEMO", na=False).any():
    st.warning(
        "⚠️ **Dados fictícios de demonstração.** Nenhuma conta está conectada. "
        "Para usar dados reais: `python scripts/init_db.py --reset` e depois "
        "`python scripts/collect.py youtube` (e/ou `x`)."
    )

# ---------------------------------------------------------------- KPIs
followers = int(posts["followers"].max()) if posts["followers"].notna().any() else 0
comparable = posts.dropna(subset=["engagement_rate"])
c1, c2, c3, c4 = st.columns(4)
c1.metric("Posts analisados", f"{len(posts):,}".replace(",", "."))
c2.metric("Seguidores (última coleta)", f"{followers:,}".replace(",", ".") if followers else "—")
c3.metric(
    "Engajamento mediano",
    f"{comparable['engagement_rate'].median():.1%}" if not comparable.empty else "—",
)
recent = posts[posts["posted_at"] >= posts["posted_at"].max() - pd.Timedelta(days=30)]
c4.metric("Posts nos últimos 30 dias", f"{len(recent)}")

tabs = st.tabs(["Visão geral", "Horários", "Posts", "Diagnóstico"])

# ---------------------------------------------------------------- Visão geral
with tabs[0]:
    left, right = st.columns([2, 1])

    with left:
        fig = px.scatter(
            posts.dropna(subset=["reach"]),
            x="posted_at_local",
            y="reach",
            color="format",
            hover_data={"title": True, "engagement_rate": ":.2%"},
            labels={"posted_at_local": "Publicado em", "reach": "Alcance", "format": "Formato"},
            title="Alcance por post ao longo do tempo",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig.update_layout(legend_title_text="Formato", hovermode="closest")
        st.plotly_chart(fig, width="stretch")

    with right:
        by_format = (
            posts.dropna(subset=["engagement_rate"])
            .groupby(["format", "platform"])["engagement_rate"]
            .median()
            .reset_index()
            .sort_values("engagement_rate", ascending=True)
        )
        fig = px.bar(
            by_format,
            x="engagement_rate",
            y="format",
            color="platform",
            orientation="h",
            title="Engajamento mediano por formato",
            labels={"engagement_rate": "Taxa de engajamento", "format": "", "platform": "Plataforma"},
        )
        fig.update_layout(xaxis_tickformat=".1%")
        st.plotly_chart(fig, width="stretch")

    st.subheader("Evolução mensal")
    monthly = (
        posts.dropna(subset=["engagement_rate"])
        .assign(mes=posts["posted_at_local"].dt.to_period("M").astype(str))
        .groupby(["mes", "platform"])
        .agg(posts=("id", "count"), engajamento=("engagement_rate", "median"))
        .reset_index()
    )
    st.plotly_chart(
        px.line(
            monthly,
            x="mes",
            y="engajamento",
            color="platform",
            markers=True,
            labels={"mes": "Mês", "engajamento": "Engajamento mediano", "platform": "Plataforma"},
        ).update_layout(yaxis_tickformat=".1%"),
        width="stretch",
    )

# ---------------------------------------------------------------- Horários
with tabs[1]:
    profile = _profile(platform)
    heat = audience_heatmap(profile)

    st.subheader("Quando seu público está ativo")
    if heat.empty:
        st.caption(
            "A API do YouTube fornece esse dado. Para o X não existe equivalente público — "
            "a análise abaixo usa seu próprio histórico de engajamento."
        )
    else:
        st.plotly_chart(
            px.imshow(
                heat,
                aspect="auto",
                color_continuous_scale="Blues",
                text_auto=".1%",
                labels={"x": "Hora", "y": "Dia da semana", "color": "Fatia da audiência"},
                title="Estimativa hora × dia (dados de audiência do YouTube)",
            ).update_layout(coloraxis_showscale=False, height=340),
            width="stretch",
        )

    slots = slot_table(posts, target=target, min_posts=2)
    st.subheader("Seu desempenho por horário")
    if slots.empty:
        st.caption("Poucos posts por horário para estimar. Continue coletando.")
    else:
        matrix = engagement_heatmap(posts.assign(engagement_rate=posts[target]))
        if not matrix.empty:
            st.plotly_chart(
                px.imshow(
                    matrix,
                    aspect="auto",
                    color_continuous_scale="Viridis",
                    text_auto=".2f",
                    labels={"x": "Hora", "y": "Dia da semana", "color": target_label},
                ).update_layout(height=340, coloraxis_showscale=False),
                width="stretch",
            )

        st.subheader("Melhores janelas (com intervalo de confiança)")
        top = best_slots(slots, top=8)
        display = top[["weekday_name", "hour", "n", "shrunk", "ci_low", "ci_high"]].copy()
        display.columns = ["Dia", "Hora", "Posts", "Estimativa", "Piso (P10)", "Teto (P90)"]
        st.dataframe(
            display.style.format({"Estimativa": "{:.2%}", "Piso (P10)": "{:.2%}", "Teto (P90)": "{:.2%}"}),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Ordenado pelo piso do intervalo, não pela média: horários com poucos posts "
            "não sobem só por sorte. A estimativa já é encolhida para a média geral."
        )

# ---------------------------------------------------------------- Posts
with tabs[2]:
    comparison = expected_vs_actual(posts, target=target)
    valid = comparison.dropna(subset=["expected"])

    st.subheader("Esperado vs. real")
    if valid.empty:
        st.caption("Ainda não há histórico suficiente para montar a linha de base (mínimo ~3 posts por formato).")
    else:
        below = int((valid["status"] == "abaixo do esperado").sum())
        above = int((valid["status"] == "acima do esperado").sum())
        st.markdown(
            f"**{len(valid)} posts comparáveis** · {above} acima do esperado · {below} abaixo · "
            f"desvio mediano {valid['ratio'].median():+.1%}"
        )

        chart = valid.copy()
        chart["delta_pct"] = chart["ratio"] * 100
        chart["rotulo"] = chart.apply(_post_label, axis=1)
        fig = px.bar(
            chart.sort_values("delta_pct"),
            x="delta_pct",
            y="rotulo",
            orientation="h",
            color="delta_pct",
            color_continuous_scale=["#d62728", "#f0f0f0", "#2ca02c"],
            color_continuous_midpoint=0,
            labels={"delta_pct": "% vs. esperado", "rotulo": ""},
            title="Desempenho relativo ao esperado (por post)",
            height=max(380, min(900, 22 * len(chart))),
        )
        fig.update_layout(coloraxis_showscale=False)
        st.plotly_chart(fig, width="stretch")

        st.subheader("Detalhe")
        table = comparison[
            ["posted_at_local", "platform", "format", "title", "reach", "engagement_rate", "expected", "ratio", "status"]
        ].copy()
        table["title"] = table["title"].fillna(table["platform"].str.upper())
        st.dataframe(
            table.sort_values("posted_at_local", ascending=False).style.format(
                {"engagement_rate": "{:.2%}", "expected": "{:.2%}", "ratio": "{:+.1%}"}
            ),
            width="stretch",
            hide_index=True,
        )

        st.subheader("Por que os piores posts caíram?")
        worst = valid.nsmallest(5, "ratio")
        for _, row in worst.iterrows():
            with st.expander(f"{_post_label(row)} · {row['ratio']:+.0%} vs. esperado"):
                st.write(
                    f"- Formato: **{row['format']}** · publicado em **{row['posted_at_local']:%d/%m/%Y %H:%M}**\n"
                    f"- Alcance: **{row['reach']:.0f}** · engajamento: **{row['engagement_rate']:.2%}**\n"
                    f"- Esperado para o formato: **{row['expected']:.2%}** (base: {row['history_n']} posts anteriores)"
                )
                st.caption(
                    "Compare o gancho dos primeiros segundos, o tema e o horário com os posts "
                    "do mesmo formato que performaram acima da linha."
                )

# ---------------------------------------------------------------- Diagnóstico
with tabs[3]:
    if platform is None:
        st.caption("Selecione uma plataforma específica no filtro lateral para o diagnóstico detalhado.")
        focus = "youtube"
    else:
        focus = platform

    report = _report(focus)
    score = int(report["score"])

    col_a, col_b = st.columns([1, 2])
    with col_a:
        st.metric("Índice de anomalia", f"{score}/100", report["verdict"])
    with col_b:
        st.progress(score / 100)
        st.caption(report["disclaimer"])

    signals = pd.DataFrame(report["signals"])
    if not signals.empty:
        st.subheader("Sinais observados")
        st.dataframe(signals, width="stretch", hide_index=True)

    alerts = report["alerts"]
    if alerts is not None and not alerts.empty:
        st.subheader("Posts na sequência de queda")
        st.dataframe(
            alerts[["posted_at", "title", "reach", "z"]].style.format({"z": "{:.1f}"}),
            width="stretch",
            hide_index=True,
        )

    if focus == "youtube":
        st.subheader("Origem do tráfego")
        traffic = _traffic(focus, "insightTrafficSourceType")
        if traffic.empty:
            st.caption("Sem dados de origem de tráfego coletados ainda.")
        else:
            fig = px.bar(
                traffic.sort_values("share_recent"),
                x=["share_baseline", "share_recent"],
                y="bucket",
                orientation="h",
                barmode="group",
                labels={"value": "Fatia do tráfego", "bucket": "Origem", "variable": ""},
                title="Últimos posts vs. histórico",
            )
            fig.update_layout(xaxis_tickformat=".0%", legend_title_text="")
            st.plotly_chart(fig, width="stretch")

        st.subheader("Inscritos vs. não inscritos")
        subs = _traffic(focus, "subscribedStatus")
        if subs.empty:
            st.caption("Sem dados de inscrição coletados ainda.")
        else:
            st.dataframe(
                subs.style.format({"share_recent": "{:.1%}", "share_baseline": "{:.1%}", "delta": "{:+.1%}"}),
                width="stretch",
                hide_index=True,
            )
