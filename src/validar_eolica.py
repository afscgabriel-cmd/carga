# -*- coding: utf-8 -*-
"""Validação da previsão eólica (fac_tempook_geracao_eolica_hourly) contra o realizado (fac_ons_geracao_eolica).

Previsão: todas as rodadas dos últimos N dias (D+0..D+9), 30 min, GW -> MW, agregada para hora.
Realizado: subsistema, dia, hora, carga (MW), horário, rótulo de início da hora.
Métricas por antecedência, por hora do dia e por subsistema (NE, S, N, SE, SIN), mais os
diagnósticos de deslocamento e de razão realizado/previsto por hora (assinatura de corte).

Uso:
    python validar_eolica.py             # últimos 35 dias de rodadas
    python validar_eolica.py --dias 60
Saídas em output/validacao_eolica/.
"""
import argparse
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carga_liquida as cl
import gerar_renovaveis as gr
import validar_solar as vs

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "validacao_eolica"
REGIOES = ["NE", "S", "N", "SE", "SIN"]


def previsoes_horarias(engine, ini, csv=None):
    t0 = time.time()
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""SELECT rodada_dia, valido_para, geracao, mnemonico_subsistema
                                    FROM fac_tempook_geracao_eolica_hourly WHERE valido_para_dia >= :ini"""),
                            con, params={"ini": ini.date()})
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia); d["valido_para"] = pd.to_datetime(d.valido_para)
    d = d[d.rodada_dia >= ini]
    d["mw"] = pd.to_numeric(d.geracao, errors="coerce") * (1000 if cl.EOLICA_EM_GW else 1)
    d["subsistema"] = d.mnemonico_subsistema.astype(str).str.strip()
    d["hora"] = d.valido_para.dt.floor("h")
    h = d.groupby(["rodada_dia", "hora", "subsistema"], as_index=False).mw.mean()
    sin = h.groupby(["rodada_dia", "hora"], as_index=False).mw.sum(); sin["subsistema"] = "SIN"
    h = pd.concat([h, sin], ignore_index=True).rename(columns={"hora": "valido_para", "mw": "prev"})
    h["antecedencia"] = (h.valido_para.dt.normalize() - h.rodada_dia).dt.days
    print(f"previsões: {h.rodada_dia.nunique()} rodadas ({h.rodada_dia.min().date()} a {h.rodada_dia.max().date()}), "
          f"{len(h):,} horas ({time.time()-t0:.0f}s)", flush=True)
    return h


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=35)
    ap.add_argument("--prev-csv"); ap.add_argument("--realizado-csv")
    a = ap.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    eng = None if (a.prev_csv and a.realizado_csv) else gr.engine_banco()
    ini = pd.Timestamp.today().normalize() - pd.Timedelta(days=a.dias)

    prev = previsoes_horarias(eng, ini, a.prev_csv)
    real = vs.ler_realizado(eng, a.realizado_csv, ini.date(), prev.valido_para.max().date(), tabela="fac_ons_geracao_eolica")
    print(f"realizado: {real.valido_para.min()} a {real.valido_para.max()}, subsistemas {sorted(real.subsistema.unique())}, "
          f"média por subsistema (MW) {real.groupby('subsistema').mw.mean().round(0).to_dict()}", flush=True)

    c = prev.merge(real, on=["valido_para", "subsistema"], how="inner")
    c = c[c.subsistema.isin(REGIOES)]
    if c.empty:
        raise SystemExit("Sem sobreposição entre previsão e realizado.")
    c["erro"] = c.prev - c.mw
    c["hora"] = c.valido_para.dt.hour
    print(f"horas comparadas: {len(c):,} | dias: {c.valido_para.dt.normalize().nunique()}")

    def tab(g):
        return pd.DataFrame({"MAE_MW": g.erro.apply(lambda e: e.abs().mean()), "vies_MW": g.erro.mean(),
                             "MAPE_%": 100 * g.erro.apply(lambda e: e.abs().mean()) / g.mw.mean(), "n": g.size()}).round(1)

    por_ant = tab(c.groupby(["subsistema", "antecedencia"])).reset_index()
    por_hora = tab(c.groupby(["subsistema", "hora"])).reset_index()
    por_ant.to_csv(OUTPUT_DIR / "eolica_por_antecedencia.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")
    por_hora.to_csv(OUTPUT_DIR / "eolica_por_hora.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")

    print("\n=== Erro por antecedência ===")
    print(por_ant.pivot(index="antecedencia", columns="subsistema", values=["MAE_MW", "vies_MW", "MAPE_%"]).round(1).to_string())
    print("\n=== Erro por hora do dia (NE e SIN) ===")
    print(por_hora[por_hora.subsistema.isin(["NE", "SIN"])].pivot(index="hora", columns="subsistema", values=["MAE_MW", "vies_MW"]).round(0).to_string())

    base = prev[(prev.subsistema == "SIN") & (prev.antecedencia == 1)][["valido_para", "prev"]]
    rs = real[real.subsistema == "SIN"][["valido_para", "mw"]]
    print("\n=== Teste de deslocamento (SIN, D+1) ===")
    for k in [-2, -1, 0, 1, 2]:
        b = base.copy(); b["valido_para"] += pd.Timedelta(hours=k)
        m = b.merge(rs, on="valido_para")
        print(f"  prev deslocada {k:+d} h: MAE = {(m.prev - m.mw).abs().mean():8.0f} MW | viés = {(m.prev - m.mw).mean():+8.0f} MW")

    print("\n=== Perfil médio por hora (SIN, D+1): previsto, realizado, razão real/prev ===")
    m = base.merge(rs, on="valido_para"); m["hora"] = m.valido_para.dt.hour
    perfil = m.groupby("hora")[["prev", "mw"]].mean().round(0)
    perfil["razao_real_prev"] = (perfil.mw / perfil.prev).round(2)
    print(perfil.to_string())
    print("  razão constante < 1 o dia todo = corte permanente ou diferença de parque; mordida em certas horas = corte por escoamento")

    # razão diária (energia) por subsistema, D+1
    d1 = c[c.antecedencia == 1].copy(); d1["dia"] = d1.valido_para.dt.date
    e = d1.groupby(["dia", "subsistema"])[["prev", "mw"]].sum().reset_index()
    e["razao"] = e.mw / e.prev
    print("\n=== Razão de energia diária realizado/previsto (D+1), por subsistema: mediana, mín, máx ===")
    print(e.groupby("subsistema").razao.agg(["median", "min", "max"]).round(2).to_string())

    # gráficos
    fig, axs = plt.subplots(1, 2, figsize=(16, 4.5))
    x = por_ant[por_ant.subsistema == "SIN"]
    axs[0].bar(x.antecedencia, x.MAE_MW, alpha=.7, label="MAE"); axs[0].plot(x.antecedencia, x.vies_MW, color="tab:red", marker="o", label="viés")
    axs[0].axhline(0, color="k", lw=.6); axs[0].set_title("Eólica SIN: erro por antecedência"); axs[0].set_xlabel("dias"); axs[0].grid(alpha=.3); axs[0].legend()
    axs[1].plot(perfil.index, perfil.prev, marker="o", label="previsto D+1"); axs[1].plot(perfil.index, perfil.mw, marker="o", color="black", label="realizado")
    axs[1].set_xticks(range(24)); axs[1].set_title("Eólica SIN: perfil médio por hora"); axs[1].grid(alpha=.3); axs[1].legend()
    fig.tight_layout(); fig.savefig(OUTPUT_DIR / "eolica_resumo.png", dpi=120); plt.close(fig)

    s = c[c.subsistema == "SIN"]
    fig, ax = plt.subplots(figsize=(16, 5))
    r0 = s.drop_duplicates("valido_para").sort_values("valido_para")
    ax.plot(r0.valido_para, r0.mw, color="black", lw=1.5, label="realizado")
    for ant, cor in [(1, "tab:orange"), (5, "tab:green")]:
        y = s[s.antecedencia == ant].sort_values("valido_para"); ax.plot(y.valido_para, y.prev, color=cor, lw=1, label=f"previsto D+{ant}")
    ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend(); ax.set_title("Eólica SIN: previsto x realizado")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(OUTPUT_DIR / "eolica_prev_x_real.png", dpi=120)
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
