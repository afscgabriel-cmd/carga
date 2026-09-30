# -*- coding: utf-8 -*-
"""Solar dia a dia: máximo realizado x máximo previsto (deck D+1), razão 9h-13h, por dia.
Responde se o corte é um teto fixo ou intermitente. Só leitura."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gerar_renovaveis as gr
import solar_ons as so
import validar_solar as vs

pd.set_option("display.width", 200)

prev = vs.previsoes_horarias(so.PASTA_DECKS)
prev = prev[(prev.subsistema == "SIN") & (prev.antecedencia == 1)]
real = vs.ler_realizado(gr.engine_banco(), None, prev.valido_para.min().date(), pd.Timestamp.today().date())
real = real[real.subsistema == "SIN"]

c = prev.merge(real, on="valido_para", how="outer").sort_values("valido_para")
c["dia"] = c.valido_para.dt.normalize()
meio = c[(c.valido_para.dt.hour >= 9) & (c.valido_para.dt.hour <= 13)]
t = pd.DataFrame({
    "dow": c.groupby("dia").valido_para.first().dt.strftime("%a"),
    "max_prev_D1": c.groupby("dia").prev.max(),
    "max_real": c.groupby("dia").mw.max(),
    "hora_max_real": c.loc[c.groupby("dia").mw.idxmax().dropna(), ["dia", "valido_para"]].set_index("dia").valido_para.dt.strftime("%H:%M"),
    "media_9h13h_prev": meio.groupby("dia").prev.mean(),
    "media_9h13h_real": meio.groupby("dia").mw.mean(),
}).round(0)
t["razao_9h13h"] = (t.media_9h13h_real / t.media_9h13h_prev).round(2)
print(t.to_string())
print("\nRazão 9h-13h: mediana", t.razao_9h13h.median(), "| dias com razão < 0,85:", int((t.razao_9h13h < 0.85).sum()), "| dias com razão >= 0,95:", int((t.razao_9h13h >= 0.95).sum()))
print("\nHoje na tabela (SIN, 9h-14h):")
hoje = real[real.valido_para.dt.normalize() == pd.Timestamp.today().normalize()]
print(hoje[(hoje.valido_para.dt.hour >= 9) & (hoje.valido_para.dt.hour <= 14)][["valido_para", "mw"]].to_string(index=False))

# ---------------------------------------------------------------- gráficos
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

out = Path(__file__).resolve().parent / "output" / "validacao_solar"
out.mkdir(parents=True, exist_ok=True)

# 1) série contínua: realizado x previsto D+1, hora a hora
fig, ax = plt.subplots(figsize=(18, 5))
ax.plot(c.valido_para, c.mw, color="black", lw=1.4, label="realizado (fac_ons_geracao_solar)")
ax.plot(c.valido_para, c.prev, color="tab:orange", lw=1.1, label="previsto D+1 (deck ONS)")
ax.fill_between(c.valido_para, c.mw, c.prev, where=c.prev > c.mw, color="tab:red", alpha=.25, label="previsto > realizado")
ax.fill_between(c.valido_para, c.mw, c.prev, where=c.prev <= c.mw, color="tab:green", alpha=.25, label="realizado > previsto")
ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend(loc="upper left", ncol=4)
ax.set_title("Solar SIN, hora a hora: realizado x previsto D+1")
fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out / "solar_serie_completa.png", dpi=120); plt.close(fig)

# 2) um painel por dia
dias = sorted(c.dia.dropna().unique())
ncol = 6; nrow = -(-len(dias) // ncol)
fig, axs = plt.subplots(nrow, ncol, figsize=(3.2 * ncol, 2.6 * nrow), sharex=True, sharey=True)
for ax, d in zip(axs.flat, dias):
    x = c[c.dia == d]; h = x.valido_para.dt.hour + x.valido_para.dt.minute / 60
    ax.plot(h, x.prev, color="tab:orange", lw=1.2); ax.plot(h, x.mw, color="black", lw=1.4)
    ax.fill_between(h, x.mw, x.prev, where=x.prev > x.mw, color="tab:red", alpha=.25)
    r = t.loc[d, "razao_9h13h"] if d in t.index else float("nan")
    ax.set_title(f"{pd.Timestamp(d).strftime('%d/%m %a')}  razão {r:.2f}", fontsize=9)
    ax.set_xticks([6, 12, 18]); ax.grid(alpha=.3)
for ax in axs.flat[len(dias):]:
    ax.axis("off")
fig.suptitle("Solar SIN por dia: preto = realizado, laranja = previsto D+1, vermelho = previsto acima do realizado", fontsize=11)
fig.tight_layout(); fig.savefig(out / "solar_por_dia.png", dpi=110)
print(f"\nGráficos: {out / 'solar_serie_completa.png'} e {out / 'solar_por_dia.png'}")
