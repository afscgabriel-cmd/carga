# -*- coding: utf-8 -*-
"""Carga líquida em 30 min, por subsistema e SIN.

    carga_liquida = carga - eólica - solar (UFV) - UTE - PCH - CGH - UHE - MGD

Fontes:
  carga        banco: fac_sintegre_prev_carga_dessem (rodada mais recente; MW; horário rotulado
               pelo FIM do intervalo -> deslocado -30 min para alinhar com as demais)
  eólica       banco: fac_tempook_geracao_eolica_hourly (rodada mais recente; GW -> MW)
  solar (UFV)  deck do ONS mais recente em solar_ons.PASTA_DECKS (só NE e SE; S e N = 0)
  renováveis   gerar_renovaveis (DESSEM estendido por perfil: UTE, PCH, CGH, UHE, MGD)

Uso:
    python carga_liquida.py                       # tudo do banco + deck
    python carga_liquida.py --horizonte 7
    python carga_liquida.py --carga-csv prev_carga_dessem.csv --eolica-csv geracao_eolica.csv \\
                            --renov-csv prev_renovaveis_dessem.csv   # teste sem banco

Saídas (output/carga_liquida ao lado do script):
    carga_liquida_<rodada>_longo.csv    30 min: subsistema, componente, mw
    carga_liquida_<rodada>_largo.csv    30 min: uma coluna por componente, por subsistema e SIN
    carga_liquida_<rodada>_diario.csv   por dia e subsistema: máx, mín, hora do máx, rampa máx
    carga_liquida_<rodada>.png
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
import gerar_renovaveis as gr
import solar_ons as so

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "carga_liquida"
SUBS = ["SE", "S", "NE", "N"]
CARGA_ROTULO_FIM = True          # valido_para da carga marca o fim do intervalo de 30 min
EOLICA_EM_GW = True              # fac_tempook_geracao_eolica_hourly está em GW
COMPONENTES = ["carga", "eolica", "solar", "UTE", "PCH", "CGH", "UHE", "MGD"]


# ------------------------------------------------------------------ leitura
def _serie(df, col_tempo, col_sub, col_val, nome):
    d = pd.DataFrame({"valido_para": pd.to_datetime(df[col_tempo]), "subsistema": df[col_sub].astype(str).str.strip(),
                      "mw": pd.to_numeric(df[col_val], errors="coerce"), "componente": nome})
    return d.groupby(["valido_para", "subsistema", "componente"], as_index=False).mw.sum()


def ler_carga(engine=None, csv=None):
    t0 = time.time()
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""
                SELECT datarodada, valido_para, val_previsaocarga, mnemonico_subsistema
                FROM fac_sintegre_prev_carga_dessem
                ORDER BY datarodada DESC LIMIT 20000"""), con)
    d["datarodada"] = pd.to_datetime(d.datarodada)
    rod = d.datarodada.max()
    d = d[d.datarodada == rod]
    s = _serie(d, "valido_para", "mnemonico_subsistema", "val_previsaocarga", "carga")
    if CARGA_ROTULO_FIM:
        s["valido_para"] = s.valido_para - pd.Timedelta(minutes=30)
    print(f"carga: rodada {rod.date()}, {s.valido_para.min()} a {s.valido_para.max()}, {len(s):,} pontos ({time.time()-t0:.0f}s)", flush=True)
    return s, rod.normalize()


def ler_eolica(engine=None, csv=None):
    t0 = time.time()
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""
                SELECT rodada_dia, valido_para, geracao, mnemonico_subsistema
                FROM fac_tempook_geracao_eolica_hourly
                ORDER BY valido_para_dia DESC LIMIT 200000"""), con)
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia)
    rod = d.rodada_dia.max()
    d = d[d.rodada_dia == rod]
    s = _serie(d, "valido_para", "mnemonico_subsistema", "geracao", "eolica")
    if EOLICA_EM_GW:
        s["mw"] *= 1000
    print(f"eólica: rodada {rod.date()}, {s.valido_para.min()} a {s.valido_para.max()}, {len(s):,} pontos ({time.time()-t0:.0f}s)", flush=True)
    return s


def ler_solar(deck=None):
    deck = Path(deck) if deck else so.deck_mais_recente(so.PASTA_DECKS)
    df = so.ler_previsoes(deck)
    meia = so.somar_meia_hora(df)
    regs = [c for c in meia.columns if c != "SIN"]
    s = meia[regs].stack().rename("mw").reset_index()
    s.columns = ["valido_para", "subsistema", "mw"]
    s["componente"] = "solar"
    print(f"solar: deck {deck.name}, regiões {regs}, {s.valido_para.min()} a {s.valido_para.max()}", flush=True)
    return s


def ler_renovaveis(engine=None, csv=None, horizonte=10):
    if csv:
        d0, ultima = gr.ler_csv(csv)
    else:
        d0, ultima = gr.ler_banco(engine)
    r = gr.estender(ultima, gr.perfil_intradiario(d0), horizonte)
    s = r.rename(columns={"tipo_fonte_energia": "componente", "submercado": "subsistema", "previsao": "mw"})
    return s[["valido_para", "subsistema", "componente", "mw"]]


# ------------------------------------------------------------------ cálculo
def montar(partes, horizonte_dias=None):
    d = pd.concat(partes, ignore_index=True)
    w = d.pivot_table(index=["valido_para", "subsistema"], columns="componente", values="mw", aggfunc="sum")
    w = w.reindex(columns=COMPONENTES)
    # grade completa: só onde há carga (é ela que define o horizonte)
    w = w[w.carga.notna()]
    ini_solar = d[d.componente == "solar"].valido_para.min()
    sem_solar = w.index.get_level_values(0) < ini_solar
    if sem_solar.any():
        print(f"AVISO: deck solar começa em {ini_solar.date()}; {sem_solar.sum()} pontos antes disso ficam com solar = 0.", flush=True)
    w["solar"] = w["solar"].fillna(0.0)   # deck só tem NE/SE; S e N = 0
    faltando = w.drop(columns="carga").isna().any(axis=1)
    if faltando.any():
        fim = w[~faltando].index.get_level_values(0).max()
        print(f"AVISO: componentes faltando após {fim}; carga líquida calculada até lá.", flush=True)
        w = w[~faltando]
    w["carga_liquida"] = w.carga - w.drop(columns="carga").sum(axis=1)
    sin = w.groupby(level=0).sum()
    sin["subsistema"] = "SIN"
    sin = sin.set_index("subsistema", append=True)
    return pd.concat([w, sin]).sort_index()


def resumo_diario(w):
    x = w.reset_index()
    x["dia"] = x.valido_para.dt.date
    g = x.groupby(["dia", "subsistema"])
    r = pd.DataFrame({
        "carga_liq_max_MW": g.carga_liquida.max(),
        "hora_max": g.apply(lambda v: v.loc[v.carga_liquida.idxmax(), "valido_para"].strftime("%H:%M")),
        "carga_liq_min_MW": g.carga_liquida.min(),
        "hora_min": g.apply(lambda v: v.loc[v.carga_liquida.idxmin(), "valido_para"].strftime("%H:%M")),
        "carga_liq_media_MW": g.carga_liquida.mean(),
        "rampa_max_MW_30min": g.apply(lambda v: v.carga_liquida.diff().max()),
        "carga_max_MW": g.carga.max(),
        "renov_media_MW": g.apply(lambda v: (v.carga - v.carga_liquida).mean()),
    }).round(0)
    return r.reset_index()


def grafico(w, rod, arq):
    sin = w.xs("SIN", level=1)
    fig, ax = plt.subplots(figsize=(16, 6))
    ax.plot(sin.index, sin.carga, color="black", lw=1.8, label="Carga")
    ax.plot(sin.index, sin.carga_liquida, color="tab:red", lw=1.8, label="Carga líquida")
    base = pd.Series(0.0, index=sin.index)
    cores = {"eolica": "tab:blue", "solar": "gold", "MGD": "orange", "UTE": "tab:green", "PCH": "tab:cyan", "UHE": "tab:purple", "CGH": "tab:gray"}
    for c in ["UHE", "PCH", "CGH", "UTE", "MGD", "solar", "eolica"]:
        ax.fill_between(sin.index, base, base + sin[c], color=cores[c], alpha=.55, label=c)
        base = base + sin[c]
    ax.set_ylabel("MW"); ax.grid(alpha=.3); ax.legend(ncol=5, fontsize=9)
    ax.set_title(f"Carga líquida SIN, 30 min, rodada {rod.date()}")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(arq, dpi=120); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizonte", type=int, default=10, help="dias após a rodada para as renováveis estendidas")
    ap.add_argument("--deck", help="zip do deck solar (padrão: o mais recente em solar_ons.PASTA_DECKS)")
    ap.add_argument("--carga-csv"); ap.add_argument("--eolica-csv"); ap.add_argument("--renov-csv")
    a = ap.parse_args()

    eng = None if (a.carga_csv and a.eolica_csv and a.renov_csv) else gr.engine_banco()
    carga, rod = ler_carga(eng, a.carga_csv)
    eol = ler_eolica(eng, a.eolica_csv)
    sol = ler_solar(a.deck)
    ren = ler_renovaveis(eng, a.renov_csv, a.horizonte)

    w = montar([carga, eol, sol, ren])
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = rod.strftime("%Y%m%d")
    w.round(1).to_csv(OUTPUT_DIR / f"carga_liquida_{tag}_largo.csv", sep=";", decimal=",", encoding="utf-8-sig")
    longo = w.stack().rename("mw").reset_index(); longo.columns = ["valido_para", "subsistema", "componente", "mw"]
    longo.round(1).to_csv(OUTPUT_DIR / f"carga_liquida_{tag}_longo.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")
    diario = resumo_diario(w)
    diario.to_csv(OUTPUT_DIR / f"carga_liquida_{tag}_diario.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")
    grafico(w, rod, OUTPUT_DIR / f"carga_liquida_{tag}.png")

    print(f"\nCarga líquida {w.index.get_level_values(0).min()} a {w.index.get_level_values(0).max()}")
    print("\nSIN por dia:")
    print(diario[diario.subsistema == "SIN"].drop(columns="subsistema").to_string(index=False))
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
