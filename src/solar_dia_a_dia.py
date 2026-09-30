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
