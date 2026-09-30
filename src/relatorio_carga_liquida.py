# -*- coding: utf-8 -*-
"""Relatório de apresentação: médias diárias e ponta da carga líquida numa janela de ~2 meses,
com o passado (D+0 de cada rodada) e, no fim, a previsão atual.

Passado (dias já ocorridos):
    solar        REALIZADO: fac_ons_geracao_solar (horário -> repetido nas duas meias-horas)
    eólica       REALIZADO: fac_ons_geracao_eolica (idem)
    carga        D+0 de fac_sintegre_prev_carga_dessem (não há tabela de carga realizada)
    renováveis   D+0 de fac_ons_renovaveis (idem)
Futuro: a previsão atual do carga_liquida.py.

Saídas em output/relatorio/:
    serie_diaria_<rodada>.csv          por dia e subsistema: média de cada variável, ponta e hora da ponta
    relatorio_SIN_<rodada>.png         SIN: carga/carga líquida (média e ponta) + componentes
    relatorio_subsistemas_<rodada>.png ponta e média da carga líquida por subsistema

Uso:
    python relatorio_carga_liquida.py                 # banco + decks
    python relatorio_carga_liquida.py --dias 60
    (opções --carga-csv/--eolica-csv/--renov-csv/--realizado-csv/--realizado-eolica-csv/--deck para teste sem banco)
"""
import argparse
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carga_liquida as cl
import gerar_renovaveis as gr
import solar_ons as so
import validar_solar as vs

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "relatorio"
SUBS = ["SE", "S", "NE", "N", "SIN"]


# ------------------------------------------------------------------ passado (D+0 de cada rodada)
def carga_d0(engine, ini, csv=None):
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""SELECT datarodada, valido_para, val_previsaocarga, mnemonico_subsistema
                                    FROM fac_sintegre_prev_carga_dessem WHERE datarodada >= :ini"""), con, params={"ini": ini.date()})
    d["datarodada"] = pd.to_datetime(d.datarodada); d["valido_para"] = pd.to_datetime(d.valido_para)
    if cl.CARGA_ROTULO_FIM:
        d["valido_para"] -= pd.Timedelta(minutes=30)
    d = d[d.valido_para.dt.normalize() == d.datarodada.dt.normalize()]
    s = cl._serie(d, "valido_para", "mnemonico_subsistema", "val_previsaocarga", "carga")
    return s


def eolica_d0(engine, ini, csv=None):
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""SELECT rodada_dia, valido_para, geracao, mnemonico_subsistema
                                    FROM fac_tempook_geracao_eolica_hourly WHERE valido_para_dia >= :ini"""), con, params={"ini": ini.date()})
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia); d["valido_para"] = pd.to_datetime(d.valido_para)
    d = d[d.valido_para.dt.normalize() == d.rodada_dia]
    s = cl._serie(d, "valido_para", "mnemonico_subsistema", "geracao", "eolica")
    if cl.EOLICA_EM_GW:
        s["mw"] *= 1000
    return s


def renovaveis_d0(engine, dias, csv=None):
    if csv:
        d0, _ = gr.ler_csv(csv)
    else:
        d0, _ = gr.ler_banco(engine, dias=dias)
    s = d0.rename(columns={"tipo_fonte_energia": "componente", "submercado": "subsistema", "previsao": "mw"})
    return s[["valido_para", "subsistema", "componente", "mw"]]


def realizado_30min(engine, tabela, componente, ini, fim, csv=None):
    """Realizado horário (subsistema, dia, hora, carga) repetido nas duas meias-horas, sem a linha SIN."""
    real = vs.ler_realizado(engine, csv, ini.date(), fim.date(), tabela=tabela)
    real = real[~real.subsistema.eq("SIN")]
    r2 = real.copy(); r2["valido_para"] += pd.Timedelta(minutes=30)
    s = pd.concat([real, r2])[["valido_para", "subsistema", "mw"]]
    s["componente"] = componente
    print(f"{componente} realizado ({tabela}): {s.valido_para.min()} a {s.valido_para.max()}, "
          f"média por subsistema (MW) {s.groupby('subsistema').mw.mean().round(0).to_dict()}", flush=True)
    return s


# ------------------------------------------------------------------ montagem
def serie_diaria(w: pd.DataFrame, origem: str) -> pd.DataFrame:
    x = w.reset_index(); x["dia"] = x.valido_para.dt.normalize()
    g = x.groupby(["dia", "subsistema"])
    medias = g[cl.COMPONENTES + ["carga_liquida"]].mean()
    medias.columns = [f"{c}_media" for c in medias.columns]
    ponta = pd.DataFrame({
        "carga_liquida_ponta": g.carga_liquida.max(),
        "hora_ponta": g.apply(lambda v: v.loc[v.carga_liquida.idxmax(), "valido_para"].strftime("%H:%M"), include_groups=False),
        "carga_liquida_min": g.carga_liquida.min(),
        "carga_ponta": g.carga.max(),
        "n_pontos": g.size(),
    })
    out = medias.join(ponta); out["origem"] = origem
    return out.reset_index()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=60, help="tamanho da janela (passado + previsão)")
    ap.add_argument("--horizonte", type=int, default=10)
    ap.add_argument("--deck"); ap.add_argument("--carga-csv"); ap.add_argument("--eolica-csv"); ap.add_argument("--renov-csv")
    ap.add_argument("--realizado-csv"); ap.add_argument("--realizado-eolica-csv")
    a = ap.parse_args()
    t0 = time.time()
    offline = a.carga_csv and a.eolica_csv and a.renov_csv and a.realizado_csv and a.realizado_eolica_csv
    eng = None if offline else gr.engine_banco()

    # ---- previsão atual (igual ao carga_liquida.py)
    carga, rod = cl.ler_carga(eng, a.carga_csv)
    eol = cl.ler_eolica(eng, a.eolica_csv)
    sol = cl.ler_solar(a.deck)
    ren = cl.ler_renovaveis(eng, a.renov_csv, a.horizonte)
    w_prev = cl.montar([carga, eol, sol, ren])
    fim_prev = w_prev.index.get_level_values(0).max().normalize()
    ini = fim_prev - pd.Timedelta(days=a.dias)
    print(f"janela: {ini.date()} a {fim_prev.date()} | rodada {rod.date()}", flush=True)

    # ---- passado: realizado (solar, eólica) e D+0 (carga, renováveis), até o dia anterior à rodada atual
    fim_pass = rod - pd.Timedelta(days=1)
    print("lendo passado...", flush=True)
    c0 = carga_d0(eng, ini, a.carga_csv)
    r0 = renovaveis_d0(eng, (fim_pass - ini).days + 2, a.renov_csv)
    s0 = realizado_30min(eng, "fac_ons_geracao_solar", "solar", ini, fim_pass, a.realizado_csv)
    e0 = realizado_30min(eng, "fac_ons_geracao_eolica", "eolica", ini, fim_pass, a.realizado_eolica_csv)
    pas = pd.concat([c0, e0, r0, s0]); pas = pas[(pas.valido_para >= ini) & (pas.valido_para < rod)]
    w_pas = cl.montar([pas]) if not pas.empty else None

    # ---- série diária
    partes = []
    if w_pas is not None and not w_pas.empty:
        partes.append(serie_diaria(w_pas, "passado: solar/eólica realizadas, carga/renováveis D+0"))
    partes.append(serie_diaria(w_prev, "previsão"))
    sd = pd.concat(partes, ignore_index=True).sort_values(["subsistema", "dia"])
    sd = sd[sd.n_pontos >= 40]   # só dias completos
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = rod.strftime("%Y%m%d")
    sd.round(1).to_csv(OUTPUT_DIR / f"serie_diaria_{tag}.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")

    # ---- gráfico SIN
    x = sd[sd.subsistema == "SIN"].set_index("dia")
    fig, axs = plt.subplots(3, 1, figsize=(16, 13), sharex=True, gridspec_kw={"height_ratios": [1.3, 1, 1]})
    ax = axs[0]
    ax.plot(x.index, x.carga_media, color="black", lw=1.8, label="Carga (média diária)")
    ax.plot(x.index, x.carga_liquida_media, color="tab:red", lw=1.8, label="Carga líquida (média diária)")
    ax.plot(x.index, x.carga_liquida_ponta, color="tab:red", lw=1.4, ls="--", marker="o", ms=3, label="Carga líquida (ponta do dia)")
    ax.plot(x.index, x.carga_liquida_min, color="tab:red", lw=1, ls=":", label="Carga líquida (mínimo do dia)")
    ax.set_ylabel("MW"); ax.set_title(f"SIN: carga e carga líquida, média e ponta diárias. Rodada {rod.date()}")
    ax = axs[1]
    for c, cor in [("eolica", "tab:blue"), ("solar", "goldenrod"), ("MGD", "orange"), ("UTE", "tab:green")]:
        ax.plot(x.index, x[f"{c}_media"], lw=1.6, color=cor, label=c)
    ax.plot(x.index, x.PCH_media + x.CGH_media + x.UHE_media, lw=1.6, color="tab:cyan", label="PCH+CGH+UHE")
    ax.set_ylabel("MW médios"); ax.set_title("Componentes (média diária)")
    ax = axs[2]
    ax.bar(x.index, x.carga_liquida_ponta - x.carga_liquida_min, width=0.8, color="tab:purple", alpha=.6, label="ponta − mínimo da carga líquida")
    ax.set_ylabel("MW"); ax.set_title("Amplitude diária da carga líquida (ponta − mínimo)")
    for ax in axs:
        ax.axvspan(rod - pd.Timedelta(hours=12), fim_prev + pd.Timedelta(hours=12), color="gray", alpha=.12)
        ax.axvline(rod - pd.Timedelta(hours=12), color="gray", lw=1, ls="--")
        ax.grid(alpha=.3); ax.legend(loc="upper left", fontsize=9, ncol=3)
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0)); ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axs[0].annotate("previsão →", xy=(rod, axs[0].get_ylim()[1] * 0.97), fontsize=10, color="gray")
    axs[0].annotate("← realizado (solar, eólica) / D+0 (carga, renováveis)", xy=(rod - pd.Timedelta(days=1), axs[0].get_ylim()[1] * 0.97), fontsize=10, color="gray", ha="right")
    fig.tight_layout(); fig.savefig(OUTPUT_DIR / f"relatorio_SIN_{tag}.png", dpi=120); plt.close(fig)

    # ---- gráfico por subsistema
    fig, axs = plt.subplots(2, 2, figsize=(16, 9), sharex=True)
    for ax, sb in zip(axs.flat, ["SE", "S", "NE", "N"]):
        y = sd[sd.subsistema == sb].set_index("dia")
        ax.plot(y.index, y.carga_media, color="black", lw=1.4, label="carga média")
        ax.plot(y.index, y.carga_liquida_media, color="tab:red", lw=1.4, label="carga líq. média")
        ax.plot(y.index, y.carga_liquida_ponta, color="tab:red", lw=1.2, ls="--", label="carga líq. ponta")
        ax.axvspan(rod - pd.Timedelta(hours=12), fim_prev + pd.Timedelta(hours=12), color="gray", alpha=.12)
        ax.set_title(sb); ax.set_ylabel("MW"); ax.grid(alpha=.3)
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0)); ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    axs[0, 0].legend(fontsize=8)
    fig.suptitle(f"Carga e carga líquida por subsistema (média e ponta diárias). Rodada {rod.date()}"); fig.tight_layout()
    fig.savefig(OUTPUT_DIR / f"relatorio_subsistemas_{tag}.png", dpi=120); plt.close(fig)

    print(f"\n{len(x)} dias na série SIN ({x.index.min().date()} a {x.index.max().date()}) em {time.time()-t0:.0f}s")
    print(x[["carga_media", "carga_liquida_media", "carga_liquida_ponta", "hora_ponta", "origem"]].tail(14).round(0).to_string())
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
