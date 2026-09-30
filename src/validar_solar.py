# -*- coding: utf-8 -*-
"""Validação da previsão solar oficial (deck ONS) contra a geração realizada (fac_ons_geracao_solar).

Para cada Deck_Previsao_*.zip em solar_ons.PASTA_DECKS: soma as usinas por região em 30 min,
agrega para hora (média de h:00 e h:30) e compara com o realizado horário por subsistema.
Realizado: subsistema, dia, hora, carga (MW), rótulo de início da hora.

Métricas por antecedência (D+0..D+9), por hora do dia e por subsistema: MAE, viés, MAPE
(sobre horas com realizado > 100 MW, para não explodir à noite).

Uso:
    python validar_solar.py                       # todos os decks da pasta + realizado do banco
    python validar_solar.py --realizado-csv geracao_solar_realizada.csv   # teste sem banco
Saídas em output/validacao_solar/.
"""
import argparse
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gerar_renovaveis as gr
import solar_ons as so

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "validacao_solar"
REGIOES = ["NE", "SE", "SIN"]


def ler_realizado(engine=None, csv=None, ini=None, fim=None):
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8", thousands=".")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '300s'"))
            d = pd.read_sql(text("""SELECT subsistema, dia, hora, carga FROM fac_ons_geracao_solar
                                    WHERE dia >= :ini AND dia <= :fim"""), con, params={"ini": ini, "fim": fim})
    d["valido_para"] = pd.to_datetime(d.dia.astype(str)) + pd.to_timedelta(d.hora.astype(str))
    d["mw"] = pd.to_numeric(d.carga, errors="coerce")
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    return d[["valido_para", "subsistema", "mw"]]


def previsoes_horarias(pasta: Path) -> pd.DataFrame:
    linhas = []
    vistos = set()
    for z in sorted(pasta.glob("Deck_Previsao_*.zip")):
        m = re.search(r"Deck_Previsao_(\d{8})", z.name)
        if not m or m[1] in vistos:      # ignora cópias tipo "Deck_Previsao_20260912 (1).zip"
            continue
        vistos.add(m[1])
        deck = pd.to_datetime(m[1], format="%Y%m%d")
        meia = so.somar_meia_hora(so.ler_previsoes(z))
        hor = meia.resample("h").mean()
        x = hor.stack().rename("prev").reset_index()
        x.columns = ["valido_para", "subsistema", "prev"]
        x["deck"] = deck
        x["antecedencia"] = (x.valido_para.dt.normalize() - deck).dt.days
        linhas.append(x)
        print(f"  {z.name}: {hor.index.min().date()} a {hor.index.max().date()}", flush=True)
    if not linhas:
        raise SystemExit(f"Nenhum deck em {pasta}")
    return pd.concat(linhas, ignore_index=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pasta", type=Path, default=so.PASTA_DECKS)
    ap.add_argument("--realizado-csv")
    a = ap.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Lendo decks...", flush=True)
    prev = previsoes_horarias(a.pasta)
    ini, fim = prev.valido_para.min().date(), prev.valido_para.max().date()
    real = ler_realizado(None if a.realizado_csv else gr.engine_banco(), a.realizado_csv, ini, fim)
    print(f"Realizado: {real.valido_para.min()} a {real.valido_para.max()}, subsistemas {sorted(real.subsistema.unique())}", flush=True)

    # piso noturno do realizado (00h-03h), por subsistema
    noite = real[real.valido_para.dt.hour <= 3].groupby("subsistema").mw.median().round(0)
    print(f"Piso noturno do realizado (mediana 00h-03h, MW): {noite.to_dict()}")

    c = prev.merge(real, on=["valido_para", "subsistema"], how="inner")
    c = c[c.subsistema.isin(REGIOES)]
    if c.empty:
        raise SystemExit("Sem sobreposição entre previsão e realizado (o realizado ainda não cobre os dias dos decks?).")
    c["erro"] = c.prev - c.mw
    c["hora"] = c.valido_para.dt.hour
    dia = c[c.mw > 100]
    print(f"\nHoras comparadas: {len(c):,} | decks: {c.deck.nunique()} | dias: {c.valido_para.dt.normalize().nunique()}")

    def tab(g):
        return pd.DataFrame({"MAE_MW": g.erro.apply(lambda e: e.abs().mean()), "vies_MW": g.erro.mean(),
                             "MAPE_%": 100 * g.erro.apply(lambda e: e.abs().mean()) / g.mw.mean(), "n_horas": g.size()}).round(1)

    por_ant = tab(dia.groupby(["subsistema", "antecedencia"])).reset_index()
    por_hora = tab(c.groupby(["subsistema", "hora"])).reset_index()
    por_dia = tab(c.groupby(["subsistema", c.valido_para.dt.date.rename("dia"), "antecedencia"])).reset_index()
    for nome, t in [("por_antecedencia", por_ant), ("por_hora", por_hora), ("por_dia", por_dia)]:
        t.to_csv(OUTPUT_DIR / f"solar_{nome}.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")

    print("\n=== Erro por antecedência (horas com realizado > 100 MW) ===")
    print(por_ant.pivot(index="antecedencia", columns="subsistema", values=["MAE_MW", "vies_MW", "MAPE_%"]).round(1).to_string())
    print("\n=== Erro por hora do dia, SIN ===")
    print(por_hora[por_hora.subsistema == "SIN"].drop(columns="subsistema").to_string(index=False))

    # --- diagnóstico 1: deslocamento de hora (qual alinhamento minimiza o MAE, SIN, D+1) ---
    print("\n=== Teste de deslocamento (SIN, D+1): MAE com a previsão deslocada de k horas ===")
    base = prev[(prev.subsistema == "SIN") & (prev.antecedencia == 1)][["valido_para", "prev"]]
    rs = real[real.subsistema == "SIN"][["valido_para", "mw"]]
    for k in [-2, -1, 0, 1, 2]:
        b = base.copy(); b["valido_para"] = b.valido_para + pd.Timedelta(hours=k)
        m = b.merge(rs, on="valido_para"); m = m[m.mw > 100]
        print(f"  prev deslocada {k:+d} h: MAE = {(m.prev - m.mw).abs().mean():8.0f} MW | viés = {(m.prev - m.mw).mean():+8.0f} MW")
    print("  (se o menor MAE não for em 0, há desalinhamento de hora entre deck e realizado)")

    # --- diagnóstico 2: perfil médio previsto x realizado por hora e razão real/prev ---
    print("\n=== Perfil médio por hora (SIN, D+1): previsto, realizado e razão real/prev ===")
    m = base.merge(rs, on="valido_para"); m["hora"] = m.valido_para.dt.hour
    perfil = m.groupby("hora")[["prev", "mw"]].mean().round(0)
    perfil["razao_real_prev"] = (perfil.mw / perfil.prev.replace(0, float("nan"))).round(2)
    perfil = perfil[(perfil.prev > 100) | (perfil.mw > 100)]
    print(perfil.to_string())
    print("  razão ~1 de manhã/tarde e bem < 1 só no meio do dia = corte de geração (constrained-off);")
    print("  razão < 1 o dia todo = diferença de parque (deck inclui usinas que o realizado não mede).")
    perfil.to_csv(OUTPUT_DIR / "solar_perfil_prev_x_real.csv", sep=";", decimal=",", encoding="utf-8-sig")

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(perfil.index, perfil.prev, marker="o", label="previsto D+1"); ax.plot(perfil.index, perfil.mw, marker="o", color="black", label="realizado")
    ax.set_xticks(range(24)); ax.set_xlabel("hora"); ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend()
    ax.set_title("Solar SIN: perfil médio por hora, previsto x realizado (set/2026)"); fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "solar_perfil_hora.png", dpi=120); plt.close(fig)

    # gráficos
    fig, axs = plt.subplots(1, 3, figsize=(20, 4.5))
    for ax, r in zip(axs, REGIOES):
        x = por_ant[por_ant.subsistema == r]
        ax.bar(x.antecedencia, x.MAE_MW, color="tab:blue", alpha=.7, label="MAE")
        ax.plot(x.antecedencia, x.vies_MW, color="tab:red", marker="o", label="viés")
        ax.axhline(0, color="k", lw=.6); ax.set_title(r); ax.set_xlabel("antecedência (dias)"); ax.grid(alpha=.3)
    axs[0].set_ylabel("MW"); axs[0].legend()
    fig.suptitle("Solar: erro da previsão do deck ONS por antecedência (horas diurnas)"); fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "solar_erro_antecedencia.png", dpi=120); plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    x = por_hora[por_hora.subsistema == "SIN"]
    ax.bar(x.hora, x.MAE_MW, color="tab:blue", alpha=.7, label="MAE"); ax.plot(x.hora, x.vies_MW, color="tab:red", marker="o", label="viés")
    ax.axhline(0, color="k", lw=.6); ax.set_xticks(range(24)); ax.set_xlabel("hora"); ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend()
    ax.set_title("Solar SIN: erro por hora do dia (prev − real)"); fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "solar_erro_hora.png", dpi=120); plt.close(fig)

    # previsto x realizado, SIN, D+1 e D+5
    s = c[c.subsistema == "SIN"]
    fig, ax = plt.subplots(figsize=(16, 5))
    r0 = s.drop_duplicates("valido_para").sort_values("valido_para")
    ax.plot(r0.valido_para, r0.mw, color="black", lw=1.5, label="realizado")
    for ant, cor in [(1, "tab:orange"), (5, "tab:green")]:
        y = s[s.antecedencia == ant].sort_values("valido_para")
        ax.plot(y.valido_para, y.prev, color=cor, lw=1, label=f"previsto D+{ant}")
    ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend(); ax.set_title("Solar SIN: previsto x realizado")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(OUTPUT_DIR / "solar_prev_x_real.png", dpi=120)
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
