"""Backtest dos métodos de perfil para as renováveis do DESSEM (UTE, PCH, CGH, UHE, MGD).

Simula a lacuna real: em cada quinta-feira, usa só o histórico até a sexta
seguinte (último dia coberto pelo DESSEM) e prevê o nível diário de sábado até a
quinta seguinte (7 dias). Compara com o D+0 do DESSEM desses dias.

Entrada: data/renov_diario_d0.csv (média diária D+0 por fonte e submercado).
Saída:   output/renovaveis/backtest_*.csv e .png
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONTES = ["UTE", "PCH", "CGH", "UHE", "MGD"]
HORIZONTE = 7  # sábado..quinta


def carregar() -> pd.DataFrame:
    d = pd.read_csv("data/renov_diario_d0.csv", sep=";", decimal=",", parse_dates=["rodada_dia"])
    d = d[d.tipo_fonte_energia.isin(FONTES)]
    # série diária larga: colunas (fonte, submercado); dias faltantes ficam NaN
    s = d.pivot_table(index="rodada_dia", columns=["tipo_fonte_energia", "submercado"], values="media_mw")
    return s.reindex(pd.date_range(s.index.min(), s.index.max()))


# ---------- métodos: recebem histórico (até a sexta) e devolvem 1 nível por série ----------
def persistencia(h):        return h.ffill().iloc[-1]
def media(n):               return lambda h: h.tail(n).mean()
def mediana(n):             return lambda h: h.tail(n).median()

def tendencia(n):
    """Reta ajustada nos últimos n dias, extrapolada para o meio do horizonte."""
    def f(h):
        x = h.tail(n)
        t = np.arange(len(x))
        out = {}
        for c in x.columns:
            y = x[c].values
            m = ~np.isnan(y)
            if m.sum() < 3:
                out[c] = np.nanmean(y)
                continue
            a, b = np.polyfit(t[m], y[m], 1)
            out[c] = a * (len(x) - 1 + HORIZONTE / 2) + b
        return pd.Series(out)
    return f

def sazonal_ajustado(n=14):
    """Nível do mesmo período no ano anterior (semana-alvo), escalado pela razão
    entre os últimos n dias deste ano e os mesmos n dias do ano passado."""
    def f(h, alvo):
        ano_ant = h.reindex(alvo - pd.DateOffset(years=1)).mean()
        rec = h.tail(n)
        rec_ant = h.reindex(rec.index - pd.DateOffset(years=1))
        razao = (rec.mean() / rec_ant.mean()).replace([np.inf, -np.inf], np.nan)
        prev = ano_ant * razao
        return prev.fillna(rec.mean())  # sem histórico do ano anterior -> média recente
    return f

METODOS = {
    "persistencia": persistencia,
    "media_7d": media(7),
    "media_14d": media(14),
    "media_28d": media(28),
    "mediana_14d": mediana(14),
    "tendencia_14d": tendencia(14),
    "tendencia_28d": tendencia(28),
    "sazonal_aj_14d": sazonal_ajustado(14),
}


def rodar(s: pd.DataFrame) -> pd.DataFrame:
    quintas = [d for d in s.index if d.weekday() == 3 and d + pd.Timedelta(days=HORIZONTE + 1) <= s.index.max()]
    linhas = []
    for q in quintas:
        sexta = q + pd.Timedelta(days=1)
        alvo = pd.date_range(sexta + pd.Timedelta(days=1), periods=HORIZONTE)
        hist = s.loc[:sexta]
        if hist.tail(28).notna().sum().min() < 3:
            continue
        real = s.reindex(alvo)
        for nome, m in METODOS.items():
            prev = m(hist, alvo) if nome.startswith("sazonal") else m(hist)
            for dia in alvo:
                r = real.loc[dia]
                if r.isna().all():
                    continue
                for col in s.columns:
                    if pd.notna(r[col]):
                        linhas.append((q, dia, (dia - sexta).days, nome, col[0], col[1], prev[col], r[col]))
    return pd.DataFrame(linhas, columns=["quinta", "dia", "antecedencia", "metodo", "fonte", "submercado", "prev", "real"])


def resumir(bt: pd.DataFrame):
    # SIN = soma dos submercados
    sin = bt.groupby(["quinta", "dia", "antecedencia", "metodo", "fonte"])[["prev", "real"]].sum().reset_index()
    sin["erro"] = sin.prev - sin.real
    g = sin.groupby(["fonte", "metodo"])
    res = pd.DataFrame({
        "MAE_MW": g.erro.apply(lambda e: e.abs().mean()),
        "vies_MW": g.erro.mean(),
        "real_medio_MW": g.real.mean(),
    })
    res["MAPE_%"] = 100 * res.MAE_MW / res.real_medio_MW
    res = res.round(1).reset_index().sort_values(["fonte", "MAE_MW"])
    return sin, res


def main():
    out = Path("output/renovaveis")
    out.mkdir(parents=True, exist_ok=True)
    s = carregar()
    bt = rodar(s)
    sin, res = resumir(bt)
    res.to_csv(out / "backtest_resultado.csv", sep=";", decimal=",", index=False)
    bt.to_csv(out / "backtest_detalhe.csv", sep=";", decimal=",", index=False)

    print(f"Semanas testadas: {bt.quinta.nunique()} | período {bt.dia.min().date()} a {bt.dia.max().date()}\n")
    for f in FONTES:
        print(res[res.fonte == f].drop(columns="fonte").to_string(index=False), "\n")

    # gráfico 1: MAPE por fonte e método
    piv = res.pivot(index="metodo", columns="fonte", values="MAPE_%")[FONTES]
    ax = piv.plot.bar(figsize=(12, 5), width=0.8)
    ax.set_ylabel("MAPE (%)"); ax.set_title("Erro do nível diário (SIN), sáb→qui, por método"); ax.grid(axis="y", alpha=.3)
    plt.xticks(rotation=30, ha="right"); plt.tight_layout(); plt.savefig(out / "backtest_mape.png", dpi=120); plt.close()

    # gráfico 2: erro por antecedência (melhor método vs persistência), por fonte
    fig, axs = plt.subplots(1, len(FONTES), figsize=(20, 4))
    for ax, f in zip(axs, FONTES):
        x = sin[sin.fonte == f]
        for m in ["persistencia", "media_7d", "media_14d", "media_28d", "sazonal_aj_14d"]:
            e = x[x.metodo == m].groupby("antecedencia").erro.apply(lambda v: v.abs().mean())
            ax.plot(e.index, e.values, marker="o", label=m)
        ax.set_title(f); ax.set_xlabel("dias após a sexta"); ax.grid(alpha=.3)
    axs[0].set_ylabel("MAE (MW)"); axs[0].legend(fontsize=8)
    fig.suptitle("Erro por antecedência (SIN)"); fig.tight_layout(); fig.savefig(out / "backtest_antecedencia.png", dpi=120); plt.close()

    # gráfico 3: erro mensal da media_14d, para ver onde ela falha (safra etc.)
    x = sin[sin.metodo == "media_14d"].copy(); x["mes"] = x.dia.dt.to_period("M")
    em = x.groupby(["mes", "fonte"]).erro.mean().unstack()[FONTES]
    fig, axs = plt.subplots(len(FONTES), 1, figsize=(14, 11), sharex=True)
    for ax, f in zip(axs, FONTES):
        ax.bar(em.index.to_timestamp(), em[f], width=20); ax.axhline(0, color="k", lw=.6); ax.set_ylabel(f"{f} (MW)"); ax.grid(alpha=.3)
    axs[0].set_title("Viés mensal da média de 14 dias (prev − real, SIN)")
    fig.tight_layout(); fig.savefig(out / "backtest_vies_mensal_media14.png", dpi=120)


if __name__ == "__main__":
    main()
