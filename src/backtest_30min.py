"""Backtest em 30 min: como estender a curva do DESSEM de sábado a quinta.

Métodos (todos usam só dados até a sexta):
  persist_sexta      repete a curva de 30 min da sexta em todos os dias
  persist_semana     repete a curva do mesmo dia da semana anterior
  nivel_x_perfil     nível da sexta x perfil (formato normalizado) médio dos últimos 28 dias
  nivel_x_perfil_dow nível da sexta x perfil médio dos últimos 28 dias, por tipo de dia
                     (útil / sábado / domingo)
Métrica: MAE de 30 min no SIN (soma dos submercados), por fonte e hora do dia.
"""
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONTES = ["UTE", "PCH", "CGH", "UHE", "MGD"]
H = 7


def carregar():
    z = zipfile.ZipFile("data/renov_30min_2025.zip")
    d = pd.read_csv(z.open(z.namelist()[0]), sep=";", decimal=",", parse_dates=["rodada_dia", "valido_para"])
    d = d[d.tipo_fonte_energia.isin(FONTES)]
    # SIN por fonte, matriz dia x 48 slots
    s = d.groupby(["tipo_fonte_energia", "rodada_dia", "valido_para"]).previsao.sum().reset_index()
    s["slot"] = (s.valido_para - s.rodada_dia).dt.total_seconds() // 1800
    return {f: s[s.tipo_fonte_energia == f].pivot(index="rodada_dia", columns="slot", values="previsao")
            .reindex(pd.date_range("2025-01-01", s.rodada_dia.max())) for f in FONTES}


def tipo_dia(idx):
    w = idx.weekday
    return np.where(w == 5, "sab", np.where(w == 6, "dom", "util"))


def prever(m: pd.DataFrame, sexta, alvo):
    hist = m.loc[:sexta]
    base = hist.ffill().loc[sexta]                # curva da sexta (ou última disponível)
    nivel = base.mean()
    rec = hist.tail(28).dropna(how="all")
    perfil = (rec.div(rec.mean(axis=1), axis=0)).mean()
    td = tipo_dia(rec.index)
    perfil_dow = {t: (rec[td == t].div(rec[td == t].mean(axis=1), axis=0)).mean() for t in ["util", "sab", "dom"]}
    out = {}
    for dia in alvo:
        t = tipo_dia(pd.DatetimeIndex([dia]))[0]
        semana = hist.get(dia - pd.Timedelta(days=7)) if False else hist.reindex([dia - pd.Timedelta(days=7)]).iloc[0]
        out[("persist_sexta", dia)] = base
        out[("persist_semana", dia)] = semana if semana.notna().any() else base
        out[("nivel_x_perfil", dia)] = nivel * perfil
        p = perfil_dow[t] if perfil_dow[t].notna().any() else perfil
        out[("nivel_x_perfil_dow", dia)] = nivel * p
    return out


def main():
    out = Path("output/renovaveis"); out.mkdir(exist_ok=True, parents=True)
    M = carregar()
    idx = M["UTE"].index
    quintas = [d for d in idx if d.weekday() == 3 and d + pd.Timedelta(days=H + 1) <= idx.max() and d >= idx.min() + pd.Timedelta(days=28)]
    res = []
    for f, m in M.items():
        for q in quintas:
            sexta = q + pd.Timedelta(days=1)
            alvo = pd.date_range(sexta + pd.Timedelta(days=1), periods=H)
            for (met, dia), prev in prever(m, sexta, alvo).items():
                real = m.reindex([dia]).iloc[0]
                if real.isna().all():
                    continue
                e = (prev - real)
                res.append(pd.DataFrame({"fonte": f, "metodo": met, "dia": dia, "slot": e.index, "erro": e.values, "real": real.values}))
    r = pd.concat(res)
    r["hora"] = r.slot / 2

    tab = r.groupby(["fonte", "metodo"]).apply(lambda x: pd.Series({"MAE_MW": x.erro.abs().mean(), "vies_MW": x.erro.mean(), "MAPE_%": 100 * x.erro.abs().mean() / x.real.mean()})).round(1)
    tab = tab.reset_index().sort_values(["fonte", "MAE_MW"])
    tab.to_csv(out / "backtest30_resultado.csv", sep=";", decimal=",", index=False)
    print(f"Semanas: {len(quintas)} ({quintas[0].date()} a {quintas[-1].date()})\n")
    for f in FONTES:
        print(tab[tab.fonte == f].drop(columns="fonte").to_string(index=False), "\n")

    # erro por hora do dia, melhor método por fonte
    fig, axs = plt.subplots(1, 5, figsize=(20, 4))
    for ax, f in zip(axs, FONTES):
        x = r[r.fonte == f]
        for met in x.metodo.unique():
            e = x[x.metodo == met].groupby("hora").erro.apply(lambda v: v.abs().mean())
            ax.plot(e.index, e.values, label=met, lw=1.2)
        ax.set_title(f); ax.set_xlabel("hora"); ax.grid(alpha=.3)
    axs[0].set_ylabel("MAE (MW)"); axs[0].legend(fontsize=7)
    fig.suptitle("Erro por hora do dia, 30 min (SIN), sáb→qui"); fig.tight_layout(); fig.savefig(out / "backtest30_hora.png", dpi=120); plt.close()

    # perfil normalizado por tipo de dia (últimos 90 dias) para mostrar a forma
    fig, axs = plt.subplots(1, 5, figsize=(20, 4))
    for ax, f in zip(axs, FONTES):
        m = M[f].tail(90).dropna(how="all"); td = tipo_dia(m.index)
        for t in ["util", "sab", "dom"]:
            p = m[td == t].div(m[td == t].mean(axis=1), axis=0).mean()
            ax.plot(p.index / 2, p.values, label=t)
        ax.set_title(f); ax.set_xlabel("hora"); ax.grid(alpha=.3)
    axs[0].set_ylabel("MW / média do dia"); axs[0].legend()
    fig.suptitle("Formato intradiário normalizado, últimos 90 dias, por tipo de dia"); fig.tight_layout(); fig.savefig(out / "perfil_tipo_dia.png", dpi=120)


if __name__ == "__main__":
    main()
