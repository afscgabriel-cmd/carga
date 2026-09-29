"""Backtest: persistência pura vs persistência com deriva sazonal, horizontes de 1 a 28 dias.

Deriva sazonal = razão entre o nível do dia-alvo e o nível da sexta, medida no
mesmo período do(s) ano(s) anterior(es), suavizada com média móvel de 7 dias.
    prev(dia) = nivel_sexta * S(dia - 1 ano) / S(sexta - 1 ano)
Variantes: 1 ano anterior; média de 2 anos anteriores; média-7d recente como base.
"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

FONTES = ["UTE", "PCH", "CGH", "UHE", "MGD"]
H = 28


def carregar():
    d = pd.read_csv("data/renov_diario_d0.csv", sep=";", decimal=",", parse_dates=["rodada_dia"])
    d = d[d.tipo_fonte_energia.isin(FONTES)]
    s = d.groupby(["rodada_dia", "tipo_fonte_energia"]).media_mw.sum().unstack()  # SIN
    s = s.reindex(pd.date_range(s.index.min(), s.index.max()))
    return s


def deriva(suave, alvo, sexta, anos):
    """razão sazonal média sobre `anos` anos anteriores; NaN -> 1."""
    r = []
    for a in anos:
        num = suave.reindex(alvo - pd.DateOffset(years=a)).values
        den = suave.reindex([sexta - pd.DateOffset(years=a)]).values[0]
        r.append(num / den)
    r = np.nanmean(np.vstack(r), axis=0)
    return np.where(np.isnan(r), 1.0, r)


def main():
    out = Path("output/renovaveis"); out.mkdir(exist_ok=True, parents=True)
    s = carregar()
    suave = s.interpolate(limit=10).rolling(7, center=True, min_periods=3).mean()
    linhas = []
    for q in s.index:
        if q.weekday() != 3 or q < s.index.min() + pd.DateOffset(years=1, days=7) or q + pd.Timedelta(days=H + 1) > s.index.max():
            continue
        sexta = q + pd.Timedelta(days=1)
        alvo = pd.date_range(sexta + pd.Timedelta(days=1), periods=H)
        hist = s.loc[:sexta]
        base_p = hist.ffill().iloc[-1]
        base_m7 = hist.tail(7).mean()
        for f in FONTES:
            real = s[f].reindex(alvo).values
            d1 = deriva(suave[f], alvo, sexta, [1])
            d2 = deriva(suave[f], alvo, sexta, [1, 2])
            prevs = {
                "persistencia": np.full(H, base_p[f]),
                "persist_deriva1a": base_p[f] * d1,
                "persist_deriva2a": base_p[f] * d2,
                "media7": np.full(H, base_m7[f]),
                "media7_deriva1a": base_m7[f] * d1,
                "media7_deriva2a": base_m7[f] * d2,
            }
            for met, p in prevs.items():
                for k in range(H):
                    if not np.isnan(real[k]):
                        linhas.append((q, f, met, k + 1, alvo[k].month, p[k] - real[k], real[k]))
    r = pd.DataFrame(linhas, columns=["quinta", "fonte", "metodo", "antec", "mes", "erro", "real"])

    def tab(x):
        g = x.groupby(["fonte", "metodo"])
        t = pd.DataFrame({"MAE": g.erro.apply(lambda e: e.abs().mean()), "vies": g.erro.mean(), "real": g.real.mean()})
        t["MAPE%"] = 100 * t.MAE / t.real
        return t.round(1).reset_index()

    for nome, sel in [("1-7 dias (sáb→qui)", r.antec <= 7), ("8-14 dias", (r.antec > 7) & (r.antec <= 14)), ("15-28 dias", r.antec > 14)]:
        t = tab(r[sel]); t["horizonte"] = nome
        print(f"\n=== {nome} | semanas: {r[sel].quinta.nunique()} ===")
        print(t.pivot(index="metodo", columns="fonte", values="MAPE%")[FONTES].to_string())
        t.to_csv(out / f"backtest_sazonal_{nome.split()[0].replace('-', '_')}.csv", sep=";", decimal=",", index=False)

    # erro por antecedência, por fonte
    fig, axs = plt.subplots(1, 5, figsize=(21, 4))
    for ax, f in zip(axs, FONTES):
        x = r[r.fonte == f]
        for met in ["persistencia", "persist_deriva1a", "persist_deriva2a", "media7", "media7_deriva2a"]:
            e = x[x.metodo == met].groupby("antec").erro.apply(lambda v: 100 * v.abs().mean()) / x.real.mean()
            ax.plot(e.index, e.values, label=met, lw=1.3)
        ax.set_title(f); ax.set_xlabel("dias após a sexta"); ax.grid(alpha=.3)
    axs[0].set_ylabel("MAPE (%)"); axs[0].legend(fontsize=7)
    fig.suptitle("Erro por antecedência: persistência vs persistência com deriva sazonal (SIN)"); fig.tight_layout()
    fig.savefig(out / "backtest_sazonal_antecedencia.png", dpi=120); plt.close()

    # UTE: viés mensal por método, horizonte 8-28 dias
    x = r[(r.fonte == "UTE") & (r.antec > 7)]
    vm = x.groupby(["mes", "metodo"]).erro.mean().unstack()[["persistencia", "persist_deriva2a", "media7_deriva2a"]]
    ax = vm.plot.bar(figsize=(12, 4), width=.8); ax.axhline(0, color="k", lw=.6); ax.grid(axis="y", alpha=.3)
    ax.set_ylabel("viés (MW)"); ax.set_xlabel("mês do dia previsto"); ax.set_title("UTE biomassa: viés por mês, horizonte 8-28 dias")
    plt.tight_layout(); plt.savefig(out / "backtest_sazonal_ute_mes.png", dpi=120)


if __name__ == "__main__":
    main()
