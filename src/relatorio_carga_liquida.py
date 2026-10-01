# -*- coding: utf-8 -*-
"""Relatório de apresentação: médias diárias e ponta da carga líquida numa janela de ~2 meses,
com o passado (D+0 de cada rodada) e, no fim, a previsão atual.

Passado (dias já ocorridos), na mesma base da previsão (geração DISPONÍVEL, sem corte):
    carga        D+0 de fac_sintegre_prev_carga_dessem
    eólica       D+0 de fac_tempook_geracao_eolica_hourly
    renováveis   D+0 de fac_ons_renovaveis
    solar        D+0 do deck de cada dia em solar_ons.PASTA_DECKS; dias sem deck: realizado (fac_ons_geracao_solar)
Com --passado realizado, solar e eólica usam o realizado do ONS (fac_ons_geracao_solar/eolica),
que inclui o corte de geração e por isso fica abaixo da previsão no meio do dia.
Futuro: a previsão atual do carga_liquida.py.

Saídas em output/relatorio/ (um PNG por variável, estilo padrão: azul = realizado/D+0, vermelho = previsão):
    serie_diaria_<rodada>.csv          por dia e subsistema: média de cada variável, ponta, mínimo, hora da ponta
    carga_liquida_ponta_<rodada>.png, carga_liquida_media_<rodada>.png, carga_liquida_min_<rodada>.png,
    carga_media_, eolica_media_, solar_media_ (24h), solar_media_diurna_, solar_max_,
    MGD_media_ (24h), MGD_media_diurna_, UTE_media_, hidro_pequenas_media_<rodada>.png
    (com --subsistema SE/S/NE/N, o mesmo conjunto para o subsistema escolhido)

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
# Realizado ONS (linha extra no passado). Carga: informe o nome da tabela (colunas subsistema, dia, hora, carga);
# None = sem carga realizada (aí só solar e eólica ganham a linha de realizado).
TABELA_CARGA_REALIZADA = "fac_ons_carga"
TABELA_SOLAR_REALIZADA = "fac_ons_geracao_solar"
TABELA_EOLICA_REALIZADA = "fac_ons_geracao_eolica"
SUBS = ["SE", "S", "NE", "N", "SIN"]


# ------------------------------------------------------------------ passado (D+0 de cada rodada)
def _mais_recente_por_dia(d, col_rodada, nome, fim=None):
    """Para cada dia: linhas da rodada mais recente com rodada <= dia (D+0 quando existe)."""
    d = d.copy()
    d["dia"] = d.valido_para.dt.normalize()
    d = d[d[col_rodada] <= d.dia]
    if fim is not None:
        d = d[d.dia <= fim]
    melhor = d.groupby(["dia", "subsistema"])[col_rodada].transform("max")
    d = d[d[col_rodada] == melhor]
    usados = d.groupby("dia")[col_rodada].max()
    atras = usados[usados < usados.index]
    if len(atras):
        print(f"{nome}: {len(atras)} dia(s) sem D+0 preenchidos com a rodada anterior: "
              f"{ {k.date().isoformat(): v.date().isoformat() for k, v in atras.items()} }", flush=True)
    return d


def carga_passado(engine, ini, fim, csv=None):
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""SELECT datarodada, valido_para, val_previsaocarga, mnemonico_subsistema
                                    FROM fac_sintegre_prev_carga_dessem WHERE datarodada >= :ini"""), con, params={"ini": ini.date()})
    d["datarodada"] = pd.to_datetime(d.datarodada).dt.normalize(); d["valido_para"] = pd.to_datetime(d.valido_para)
    if cl.CARGA_ROTULO_FIM:
        d["valido_para"] -= pd.Timedelta(minutes=30)
    d["subsistema"] = d.mnemonico_subsistema.astype(str).str.strip()
    d = _mais_recente_por_dia(d, "datarodada", "carga", fim)
    return cl._serie(d, "valido_para", "subsistema", "val_previsaocarga", "carga")


def eolica_passado(engine, ini, fim, csv=None):
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text("""SELECT rodada_dia, valido_para, geracao, mnemonico_subsistema
                                    FROM fac_tempook_geracao_eolica_hourly WHERE valido_para_dia >= :ini"""), con, params={"ini": ini.date()})
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia); d["valido_para"] = pd.to_datetime(d.valido_para)
    d["subsistema"] = d.mnemonico_subsistema.astype(str).str.strip()
    d = _mais_recente_por_dia(d, "rodada_dia", "eólica", fim)
    s = cl._serie(d, "valido_para", "subsistema", "geracao", "eolica")
    if cl.EOLICA_EM_GW:
        s["mw"] *= 1000
    return s


def renovaveis_passado(engine, dias, csv=None):
    """Para cada dia passado: o D+0 da rodada daquele dia; se não existir (rodada ainda não carregada),
    a previsão da rodada mais recente anterior ao dia (D+1, D+2...). Nenhum dia fica sem valor."""
    if csv:
        d0, _ = gr.ler_csv(csv)
        d = d0
    else:
        _, _, d = gr.ler_banco(engine, dias=dias, completo=True)
    d = d.copy()
    d["dia"] = d.valido_para.dt.normalize()
    d = d[(d.rodada_dia <= d.dia) & (d.dia <= d.rodada_dia.max() + pd.Timedelta(days=7))]
    melhor = d.groupby(["dia", "tipo_fonte_energia", "submercado"]).rodada_dia.transform("max")
    d = d[d.rodada_dia == melhor]
    usados = d.groupby("dia").rodada_dia.max()
    atras = usados[usados < usados.index]
    if len(atras):
        print(f"renováveis: {len(atras)} dia(s) sem D+0 preenchidos com a rodada anterior: "
              f"{ {k.date().isoformat(): v.date().isoformat() for k, v in atras.items()} }", flush=True)
    s = d.rename(columns={"tipo_fonte_energia": "componente", "submercado": "subsistema", "previsao": "mw"})
    return s[["valido_para", "subsistema", "componente", "mw"]]


def solar_passado_decks(ini, fim, pasta=None):
    """Para cada dia: o deck daquele dia (D+0); se não existir, o deck mais recente anterior.
    Devolve (série 30 min, dias cobertos)."""
    pasta = pasta or so.PASTA_DECKS
    decks = {}
    for z in sorted(pasta.glob("Deck_Previsao_*.zip")):
        m = so.re.search(r"Deck_Previsao_(\d{8})", z.name)
        if m and m[1] not in decks:
            decks[m[1]] = z
    datas = sorted(pd.to_datetime(k, format="%Y%m%d") for k in decks)
    partes, cobertos, atras = [], set(), {}
    cache = {}
    for dia in pd.date_range(ini, fim):
        cand = [d for d in datas if d <= dia and (dia - d).days <= 9]
        if not cand:
            continue
        deck = max(cand)
        if deck not in cache:
            cache[deck] = so.somar_meia_hora(so.ler_previsoes(decks[deck.strftime("%Y%m%d")]))
        meia = cache[deck]; meia = meia[meia.index.normalize() == dia]
        if meia.empty:
            continue
        x = meia.drop(columns="SIN").stack().rename("mw").reset_index(); x.columns = ["valido_para", "subsistema", "mw"]
        partes.append(x); cobertos.add(dia)
        if deck < dia:
            atras[dia.date().isoformat()] = deck.date().isoformat()
    if atras:
        print(f"solar: {len(atras)} dia(s) sem deck próprio usam o deck anterior: {atras}", flush=True)
    s = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=["valido_para", "subsistema", "mw"])
    s["componente"] = "solar"
    return s, cobertos


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
    def media_diurna(v, col):
        d = v.loc[v[col] > 0, col]
        return d.mean() if len(d) else 0.0
    ponta = pd.DataFrame({
        "solar_media_diurna": g.apply(lambda v: media_diurna(v, "solar"), include_groups=False),
        "MGD_media_diurna": g.apply(lambda v: media_diurna(v, "MGD"), include_groups=False),
        "solar_max": g.solar.max(),
        "carga_liquida_ponta": g.carga_liquida.max(),
        "hora_ponta": g.apply(lambda v: v.loc[v.carga_liquida.idxmax(), "valido_para"].strftime("%H:%M"), include_groups=False),
        "carga_liquida_min": g.carga_liquida.min(),
        "carga_ponta": g.carga.max(),
        "n_pontos": g.size(),
    })
    out = medias.join(ponta); out["origem"] = origem
    return out.reset_index()


def grafico_enxuto(x: pd.DataFrame, col: str, titulo: str, fonte: str, rod, arq: Path):
    """Um gráfico por variável: Programado (azul, D+0), Realizado (verde, verificado ONS, quando houver) e Previsão (vermelho tracejado), em GW."""
    y = x[col] / 1000.0
    pas = y[x.origem != "previsão"]
    prv = y[x.origem == "previsão"]
    if not pas.empty:   # emenda: a previsão começa no último ponto do passado
        prv = pd.concat([pas.tail(1), prv])
    fig, ax = plt.subplots(figsize=(16, 6))
    # realizado (verificado ONS), só no passado e só onde existir a coluna <col>_real
    if f"{col}_real" in x.columns and not pas.empty:
        real = (x[f"{col}_real"] / 1000.0).dropna()
        real = real[real.index <= pas.index.max()]
        if not real.empty:
            ax.plot(real.index, real.values, color="#2ca02c", lw=1.6, marker="s", ms=4, alpha=.9, label="Realizado", zorder=1)
    # previsão: linha tracejada emendada no último realizado, mas marcadores só nos dias previstos
    ax.plot(prv.index, prv.values, color="#d62728", lw=2, ls="--", zorder=2)
    so_prev = prv.iloc[1:] if not pas.empty else prv
    ax.plot(so_prev.index, so_prev.values, color="#d62728", lw=0, marker="o", ms=6, zorder=3)
    ax.plot([], [], color="#d62728", lw=2, ls="--", marker="o", ms=6, label="Previsão")
    ax.plot(pas.index, pas.values, color="#1f77b4", lw=2, marker="o", ms=6, label="Programado", zorder=4)
    # sombra e divisória entre o último realizado e o primeiro previsto
    ini_prev = (pas.index.max() + pd.Timedelta(hours=12)) if not pas.empty else prv.index.min()
    ax.axvspan(ini_prev, prv.index.max() + pd.Timedelta(hours=12), color="#d62728", alpha=.07)
    ax.axvline(ini_prev, color="gray", lw=1, ls=":")
    # rótulos fora da linha: acima quando o ponto está no alto em relação aos vizinhos, abaixo quando está no vale
    serie = pd.concat([pas, prv.iloc[1:]])
    vals = serie.values
    for i, (d, v) in enumerate(serie.items()):
        viz = [vals[j] for j in (i - 1, i + 1) if 0 <= j < len(vals)]
        acima = v >= sum(viz) / len(viz)
        cor = "#1f77b4" if d < ini_prev else "#d62728"
        ax.annotate(f"{v:.1f}".replace(".", ","), (d, v), textcoords="offset points", xytext=(0, 10 if acima else -16),
                    ha="center", fontsize=8.5, color=cor)
    ax.set_ylabel("GW"); ax.grid(alpha=.25)
    ax.set_title(titulo, loc="left", fontsize=13, pad=12)
    fig.text(0.99, 0.965, f"Rodada de {rod.strftime('%d/%m/%Y')}", ha="right", fontsize=11, color="#555")
    fig.text(0.01, 0.01, f"Fonte: {fonte}", ha="left", fontsize=9.5, color="#555")
    h, l = ax.get_legend_handles_labels()
    ordem = [l.index(k) for k in ["Programado", "Realizado", "Previsão"] if k in l]
    ax.legend([h[i] for i in ordem], [l[i] for i in ordem], loc="upper left", frameon=True)
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0)); ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=45); fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(arq, dpi=120); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=30, help="tamanho da janela (passado + previsão)")
    ap.add_argument("--hoje", help="fronteira passado/previsão (padrão: a data mais recente entre as rodadas)")
    ap.add_argument("--subsistema", default="SIN", choices=["SIN", "SE", "S", "NE", "N"], help="qual subsistema desenhar")
    ap.add_argument("--horizonte", type=int, default=10)
    ap.add_argument("--deck"); ap.add_argument("--carga-csv"); ap.add_argument("--eolica-csv"); ap.add_argument("--renov-csv")
    ap.add_argument("--passado", choices=["d0", "realizado"], default="d0",
                    help="passado de solar/eólica: d0 = mesma base da previsão (padrão); realizado = ONS, com corte")
    ap.add_argument("--realizado-csv"); ap.add_argument("--realizado-eolica-csv")
    a = ap.parse_args()
    t0 = time.time()
    offline = a.carga_csv and a.eolica_csv and a.renov_csv and a.realizado_csv and (a.realizado_eolica_csv or a.passado == "d0")
    eng = None if offline else gr.engine_banco()

    # ---- previsão atual (igual ao carga_liquida.py); fronteira = hoje (as fontes podem estar em rodadas diferentes)
    carga, rod_carga = cl.ler_carga(eng, a.carga_csv)
    eol = cl.ler_eolica(eng, a.eolica_csv)
    sol = cl.ler_solar(a.deck)
    ren = cl.ler_renovaveis(eng, a.renov_csv, a.horizonte)
    hoje = pd.Timestamp(a.hoje) if a.hoje else max(rod_carga, eol.valido_para.min().normalize(), sol.valido_para.min().normalize())
    rod = hoje
    w_prev = cl.montar([carga, eol, sol, ren])
    w_prev = w_prev[w_prev.index.get_level_values(0) >= hoje]
    fim_prev = w_prev.index.get_level_values(0).max().normalize()
    ini = fim_prev - pd.Timedelta(days=a.dias)
    print(f"janela: {ini.date()} a {fim_prev.date()} | fronteira (hoje): {hoje.date()} | rodadas: carga {rod_carga.date()}, "
          f"eólica {eol.valido_para.min().date()}, deck solar {sol.valido_para.min().date()}", flush=True)

    # ---- passado: dias < hoje, cada fonte com a informação mais recente disponível para o dia
    fim_pass = hoje - pd.Timedelta(days=1)
    print("lendo passado...", flush=True)
    c0 = carga_passado(eng, ini, fim_pass, a.carga_csv)
    r0 = renovaveis_passado(eng, (fim_pass - ini).days + 2, a.renov_csv)
    if a.passado == "realizado":
        s0 = realizado_30min(eng, TABELA_SOLAR_REALIZADA, "solar", ini, fim_pass, a.realizado_csv)
        e0 = realizado_30min(eng, TABELA_EOLICA_REALIZADA, "eolica", ini, fim_pass, a.realizado_eolica_csv)
        rotulo = "passado: solar/eólica REALIZADAS (com corte), carga/renováveis D+0"
    else:
        e0 = eolica_passado(eng, ini, fim_pass, a.eolica_csv)
        s0, cobertos = solar_passado_decks(ini, fim_pass, Path(a.deck).parent if a.deck else None)
        sem_deck = pd.date_range(ini, fim_pass).difference(pd.DatetimeIndex(sorted(cobertos)))
        if len(sem_deck):
            print(f"solar: {len(cobertos)} dias com deck; {len(sem_deck)} dias sem nenhum deck usam o realizado "
                  f"({sem_deck.min().date()} a {sem_deck.max().date()})", flush=True)
            sr = realizado_30min(eng, TABELA_SOLAR_REALIZADA, "solar", sem_deck.min(), sem_deck.max(), a.realizado_csv)
            s0 = pd.concat([s0, sr[sr.valido_para.dt.normalize().isin(sem_deck)]], ignore_index=True)
        else:
            print(f"solar: {len(cobertos)} dias com deck, nenhum dia sem deck", flush=True)
        rotulo = "passado: D+0 (mesma base da previsão)"
    pas = pd.concat([c0, e0, r0, s0]); pas = pas[(pas.valido_para >= ini) & (pas.valido_para < hoje)]
    w_pas = cl.montar([pas]) if not pas.empty else None

    # ---- realizado ONS no passado (linha extra): solar, eólica e, se houver tabela, carga e carga líquida
    try:
        sol_r = realizado_30min(eng, TABELA_SOLAR_REALIZADA, "solar", ini, fim_pass, a.realizado_csv)
        eol_r = realizado_30min(eng, TABELA_EOLICA_REALIZADA, "eolica", ini, fim_pass, a.realizado_eolica_csv)
        comps = [sol_r, eol_r]
        if TABELA_CARGA_REALIZADA:
            comps.append(realizado_30min(eng, TABELA_CARGA_REALIZADA, "carga", ini, fim_pass, None))
            comps.append(r0)   # renováveis flat não têm realizado: entram em D+0
            w_real = cl.montar(comps)
        else:
            w_real = pd.concat(comps).pivot_table(index=["valido_para", "subsistema"], columns="componente", values="mw", aggfunc="sum")
            sin_r = w_real.groupby(level=0).sum(); sin_r["subsistema"] = "SIN"; sin_r = sin_r.set_index("subsistema", append=True)
            w_real = pd.concat([w_real, sin_r]).sort_index()
            w_real = w_real[(w_real.index.get_level_values(0) >= ini) & (w_real.index.get_level_values(0) < rod)]
        xr = w_real.reset_index(); xr["dia"] = xr.valido_para.dt.normalize()
        gr_ = xr.groupby(["dia", "subsistema"])
        sd_real = gr_[[c for c in ["carga", "eolica", "solar", "carga_liquida"] if c in xr.columns]].mean()
        sd_real.columns = [f"{c}_media_real" for c in sd_real.columns]
        if "carga_liquida" in xr.columns:
            sd_real["carga_liquida_ponta_real"] = gr_.carga_liquida.max()
            sd_real["carga_liquida_min_real"] = gr_.carga_liquida.min()
        sd_real["solar_media_diurna_real"] = gr_.apply(lambda v: v.loc[v.solar > 0, "solar"].mean() if (v.solar > 0).any() else 0.0, include_groups=False)
        sd_real["solar_max_real"] = gr_.solar.max()
        sd_real = sd_real.reset_index()
        print(f"realizado ONS: {sd_real.dia.nunique()} dias, variáveis {[c.replace('_real', '') for c in sd_real.columns if c.endswith('_real')]}", flush=True)
    except SystemExit as e:
        print(f"realizado ONS indisponível ({e}); gráficos sem a linha de realizado", flush=True)
        sd_real = None

    # ---- série diária
    partes = []
    if w_pas is not None and not w_pas.empty:
        partes.append(serie_diaria(w_pas, rotulo))
    partes.append(serie_diaria(w_prev, "previsão"))
    sd = pd.concat(partes, ignore_index=True).sort_values(["subsistema", "dia"])
    sd = sd[sd.n_pontos >= 40]   # só dias completos
    if sd_real is not None:
        sd = sd.merge(sd_real, on=["dia", "subsistema"], how="left")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = rod.strftime("%Y%m%d")
    sd.round(1).to_csv(OUTPUT_DIR / f"serie_diaria_{tag}.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")

    # ---- gráficos enxutos: um por variável, estilo padrão da equipe
    x = sd[sd.subsistema == a.subsistema].set_index("dia")
    graficos = [
        ("carga_liquida_ponta", "Carga líquida  -  ponta diária (máximo)", "programado: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM; realizado: fac_ons_carga, fac_ons_geracao_eolica/solar"),
        ("carga_liquida_media", "Carga líquida  -  média diária", "programado: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM; realizado: fac_ons_carga, fac_ons_geracao_eolica/solar"),
        ("carga_liquida_min", "Carga líquida  -  mínimo diário", "programado: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM; realizado: fac_ons_carga, fac_ons_geracao_eolica/solar"),
        ("carga_media", "Carga  -  média diária", "programado: prev_carga_dessem; realizado: fac_ons_carga"),
        ("eolica_media", "Geração eólica  -  média diária", "TEMPO OK"),
        ("solar_media", "Geração solar (UFV)  -  média diária (24h)", "deck de previsão ONS"),
        ("solar_media_diurna", "Geração solar (UFV)  -  média diurna (horas com sol)", "deck de previsão ONS"),
        ("solar_max", "Geração solar (UFV)  -  máximo diário", "deck de previsão ONS"),
        ("MGD_media", "MMGD  -  média diária (24h)", "DESSEM"),
        ("MGD_media_diurna", "MMGD  -  média diurna (horas com sol)", "DESSEM"),
        ("UTE_media", "UTE biomassa  -  média diária", "DESSEM"),
        ("hidro_pequenas_media", "PCH + CGH + UHE pequenas  -  média diária", "DESSEM"),
    ]
    x["hidro_pequenas_media"] = x.PCH_media + x.CGH_media + x.UHE_media
    sufixo = "" if a.subsistema == "SIN" else f"_{a.subsistema}"
    for col, titulo, fonte in graficos:
        grafico_enxuto(x, col, f"{titulo}  -  últimos {a.dias} dias" + ("" if a.subsistema == "SIN" else f"  -  {a.subsistema}"),
                       fonte, rod, OUTPUT_DIR / f"{col}{sufixo}_{tag}.png")

    print(f"\n{len(x)} dias na série SIN ({x.index.min().date()} a {x.index.max().date()}) em {time.time()-t0:.0f}s")
    print(x[["carga_media", "carga_liquida_media", "carga_liquida_ponta", "hora_ponta", "origem"]].tail(14).round(0).to_string())
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
