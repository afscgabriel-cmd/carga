# -*- coding: utf-8 -*-
"""Relatório de apresentação: médias diárias e ponta da carga líquida numa janela de ~2 meses,
com o passado (D+0 de cada rodada) e, no fim, a previsão atual.

Três linhas por variável:
    Previsão   (vermelho) carga: fac_sintegre_prev_carga_dessem | eólica: fac_tempook_geracao_eolica_hourly |
                          solar: deck ONS | UTE/PCH/CGH/UHE/MGD: DESSEM estendido por perfil
    Programado (azul)     o deck DESSEM do próprio dia (D+0): carga de fac_sintegre_carga_dessem_hourly (delta 0),
                          eólica (UEE), solar (UFV) e demais fontes de fac_ons_renovaveis. Dia sem D+0 ainda
                          carregado: rodada mais recente anterior.
    Realizado  (verde)    fac_ons_carga, fac_ons_geracao_eolica, fac_ons_geracao_solar (tempo real ONS);
                          UTE/PCH/CGH/UHE/MGD não têm realizado por fonte: entram com o programado.
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
TABELA_CARGA_PROGRAMADA = "fac_sintegre_carga_dessem_hourly"   # carga do deck DESSEM (delta = antecedência em dias)
TABELA_SOLAR_REALIZADA = "fac_ons_geracao_solar"
TABELA_EOLICA_REALIZADA = "fac_ons_geracao_eolica"
TABELA_PLD = "fac_ccee_pld_hourly"          # PLD horário CCEE (sist: SE, SU, NE, N); None = sem PLD no gráfico
PLD_AGREGACAO = "media"                       # "media" = média diária das 24h; "ponta" = PLD na hora da ponta da carga líquida
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


def carga_programada(engine, ini, fim, csv=None):
    """Carga do deck DESSEM (fac_sintegre_carga_dessem_hourly), rodada mais recente <= dia (delta 0 quando existe)."""
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text(f"""SELECT rodada, valido_para, subsystem, demanda FROM {TABELA_CARGA_PROGRAMADA}
                                     WHERE dia >= :ini AND dia <= :fim"""), con, params={"ini": ini.date(), "fim": fim.date()})
    d["rodada"] = pd.to_datetime(d.rodada).dt.normalize(); d["valido_para"] = pd.to_datetime(d.valido_para)
    d["subsistema"] = d.subsystem.astype(str).str.strip()
    d = _mais_recente_por_dia(d, "rodada", "carga programada", fim)
    return cl._serie(d, "valido_para", "subsistema", "demanda", "carga")


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


def renovaveis_programadas(engine, dias, csv=None, fontes=("UTE", "PCH", "CGH", "UHE", "MGD", "UEE", "UFV")):
    """Deck DESSEM do próprio dia (D+0) para cada fonte; se não existir, a rodada mais recente anterior.
    UEE vira 'eolica' e UFV vira 'solar' (programado do DESSEM para essas duas)."""
    if csv:
        d0, _ = gr.ler_csv(csv, fontes=list(fontes))
        d = d0
    else:
        _, _, d = gr.ler_banco(engine, dias=dias, completo=True, fontes=list(fontes))
    d = d.copy()
    d["dia"] = d.valido_para.dt.normalize()
    d = d[(d.rodada_dia <= d.dia) & (d.dia <= d.rodada_dia.max() + pd.Timedelta(days=7))]
    melhor = d.groupby(["dia", "tipo_fonte_energia", "submercado"]).rodada_dia.transform("max")
    d = d[d.rodada_dia == melhor]
    usados = d.groupby("dia").rodada_dia.max()
    atras = usados[usados < usados.index]
    if len(atras):
        print(f"DESSEM: {len(atras)} dia(s) sem D+0 preenchidos com a rodada anterior: "
              f"{ {k.date().isoformat(): v.date().isoformat() for k, v in atras.items()} }", flush=True)
    s = d.rename(columns={"tipo_fonte_energia": "componente", "submercado": "subsistema", "previsao": "mw"})
    s["componente"] = s.componente.replace({"UEE": "eolica", "UFV": "solar"})
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


def ler_pld(engine, ini, fim, csv=None):
    """PLD horário por subsistema -> série diária = média dos subsistemas (R$/MWh). Devolve DataFrame dia x [media, por hora]."""
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '300s'"))
            d = pd.read_sql(text(f"""SELECT valido_para, sist, pld FROM {TABELA_PLD}
                                     WHERE valido_para_dia >= :ini AND valido_para_dia <= :fim"""), con, params={"ini": ini.date(), "fim": fim.date()})
    d["valido_para"] = pd.to_datetime(d.valido_para); d["pld"] = pd.to_numeric(d.pld, errors="coerce")
    d["sist"] = d.sist.astype(str).str.strip().replace({"SU": "S"})
    h = d.groupby("valido_para").pld.mean()          # média dos subsistemas, por hora
    h = h[h.index.map(lambda t: d[d.valido_para == t].sist.nunique()) >= 3] if len(h) < 2000 else h
    print(f"PLD ({TABELA_PLD}): {h.index.min()} a {h.index.max()}, subsistemas {sorted(d.sist.unique())}", flush=True)
    return h


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
        # "horas com sol": pontos acima de 5% do máximo do dia (o realizado nunca é zero exato à noite)
        lim = 0.05 * v[col].max()
        d = v.loc[v[col] > lim, col]
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


def grafico_enxuto(x: pd.DataFrame, col: str, titulo: str, fonte: str, rod, arq: Path, pld: pd.Series | None = None):
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
    hs, ls = [h[i] for i in ordem], [l[i] for i in ordem]
    if pld is not None and not pld.empty:
        ax2 = ax.twinx()
        ax2.bar(pld.index, pld.values, width=0.55, color="#7f7f7f", alpha=.28, label="PLD (média dos subsistemas)", zorder=0)
        ax2.set_ylabel("PLD (R$/MWh)", color="#555"); ax2.tick_params(axis="y", colors="#555")
        ax2.set_ylim(0, max(pld.max() * 2.4, 1))   # barras na metade inferior, sem cobrir as linhas
        for d, v in pld.items():
            ax2.annotate(f"{v:.0f}", (d, v), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=7.5, color="#555")
        ax.set_zorder(ax2.get_zorder() + 1); ax.patch.set_visible(False)
        h2, l2 = ax2.get_legend_handles_labels(); hs += h2; ls += l2
    ax.legend(hs, ls, loc="upper left", frameon=True)
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
    ap.add_argument("--carga-prog-csv", help="teste: CSV no formato de fac_sintegre_carga_dessem_hourly")
    ap.add_argument("--carga-real-csv", help="teste: CSV no formato de fac_ons_carga")
    ap.add_argument("--pld-csv", help="teste: CSV no formato de fac_ccee_pld_hourly")
    ap.add_argument("--realizado-csv"); ap.add_argument("--realizado-eolica-csv")
    a = ap.parse_args()
    t0 = time.time()
    offline = a.carga_csv and a.eolica_csv and a.renov_csv and a.realizado_csv and a.realizado_eolica_csv and a.carga_prog_csv
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

    # ---- passado = PROGRAMADO: deck DESSEM do próprio dia (D+0) para todas as variáveis
    fim_pass = hoje - pd.Timedelta(days=1)
    print("lendo programado (DESSEM D+0)...", flush=True)
    c0 = carga_programada(eng, ini, fim_pass, a.carga_prog_csv)
    r0 = renovaveis_programadas(eng, (fim_pass - ini).days + 2, a.renov_csv)   # inclui eólica (UEE) e solar (UFV)
    pas = pd.concat([c0, r0]); pas = pas[(pas.valido_para >= ini) & (pas.valido_para < hoje)]
    faltam = set(cl.COMPONENTES) - set(pas.componente.unique())
    if faltam:
        print(f"AVISO: programado sem as fontes {sorted(faltam)} no DESSEM lido", flush=True)
    rotulo = "programado: deck DESSEM D+0"
    w_pas = cl.montar([pas]) if not pas.empty else None

    # ---- realizado ONS no passado (linha extra): solar, eólica e, se houver tabela, carga e carga líquida
    try:
        sol_r = realizado_30min(eng, TABELA_SOLAR_REALIZADA, "solar", ini, fim_pass, a.realizado_csv)
        eol_r = realizado_30min(eng, TABELA_EOLICA_REALIZADA, "eolica", ini, fim_pass, a.realizado_eolica_csv)
        comps = [sol_r, eol_r]
        if TABELA_CARGA_REALIZADA:
            comps.append(realizado_30min(eng, TABELA_CARGA_REALIZADA, "carga", ini, fim_pass, a.carga_real_csv))
            comps.append(r0[~r0.componente.isin(["eolica", "solar"])])   # flats não têm realizado: entram com o programado
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
        sd_real["solar_media_diurna_real"] = gr_.apply(
            lambda v: v.loc[v.solar > 0.05 * v.solar.max(), "solar"].mean() if (v.solar > 0).any() else 0.0, include_groups=False)
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

    # ---- PLD diário (média dos subsistemas) para o eixo auxiliar da ponta
    pld_dia = None
    if TABELA_PLD and a.subsistema == "SIN":
        try:
            pld_h = ler_pld(eng, ini, fim_prev, a.pld_csv)
            if PLD_AGREGACAO == "ponta":
                hp = sd[sd.subsistema == "SIN"].set_index("dia").hora_ponta
                pld_dia = pd.Series({d: pld_h.get(pd.Timestamp(d) + pd.to_timedelta(hp[d] + ":00").floor("h"), float("nan"))
                                     for d in hp.index}).dropna()
            else:
                pld_dia = pld_h.groupby(pld_h.index.normalize()).mean()
            pld_dia = pld_dia[pld_dia.index >= ini]
            print(f"PLD diário: {len(pld_dia)} dias ({pld_dia.index.min().date()} a {pld_dia.index.max().date()})", flush=True)
        except Exception as e:
            print(f"PLD indisponível ({e}); gráfico da ponta sem PLD", flush=True)

    # ---- gráficos enxutos: um por variável, estilo padrão da equipe
    x = sd[sd.subsistema == a.subsistema].set_index("dia")
    graficos = [
        ("carga_liquida_ponta", "Carga líquida  -  ponta diária (máximo)", "previsão: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM | programado: deck DESSEM D+0 | realizado: fac_ons_carga, geracao_eolica, geracao_solar"),
        ("carga_liquida_media", "Carga líquida  -  média diária", "previsão: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM | programado: deck DESSEM D+0 | realizado: fac_ons_carga, geracao_eolica, geracao_solar"),
        ("carga_liquida_min", "Carga líquida  -  mínimo diário", "previsão: prev_carga_dessem, TEMPO OK, deck ONS, DESSEM | programado: deck DESSEM D+0 | realizado: fac_ons_carga, geracao_eolica, geracao_solar"),
        ("carga_media", "Carga  -  média diária", "previsão: prev_carga_dessem | programado: carga_dessem_hourly | realizado: fac_ons_carga"),
        ("eolica_media", "Geração eólica  -  média diária", "previsão: TEMPO OK | programado: DESSEM (UEE) | realizado: fac_ons_geracao_eolica"),
        ("solar_media", "Geração solar (UFV)  -  média diária (24h)", "previsão: deck ONS | programado: DESSEM (UFV) | realizado: fac_ons_geracao_solar"),
        ("solar_media_diurna", "Geração solar (UFV)  -  média diurna (horas com sol, > 5% do máximo)", "previsão: deck ONS | programado: DESSEM (UFV) | realizado: fac_ons_geracao_solar"),
        ("solar_max", "Geração solar (UFV)  -  máximo diário", "previsão: deck ONS | programado: DESSEM (UFV) | realizado: fac_ons_geracao_solar"),
        ("MGD_media", "MMGD  -  média diária (24h)", "previsão: DESSEM estendido | programado: DESSEM D+0"),
        ("MGD_media_diurna", "MMGD  -  média diurna (horas com sol, > 5% do máximo)", "previsão: DESSEM estendido | programado: DESSEM D+0"),
        ("UTE_media", "UTE biomassa  -  média diária", "previsão: DESSEM estendido | programado: DESSEM D+0"),
        ("hidro_pequenas_media", "PCH + CGH + UHE pequenas  -  média diária", "previsão: DESSEM estendido | programado: DESSEM D+0"),
    ]
    x["hidro_pequenas_media"] = x.PCH_media + x.CGH_media + x.UHE_media
    sufixo = "" if a.subsistema == "SIN" else f"_{a.subsistema}"
    for col, titulo, fonte in graficos:
        grafico_enxuto(x, col, f"{titulo}  -  últimos {a.dias} dias" + ("" if a.subsistema == "SIN" else f"  -  {a.subsistema}"),
                       fonte + (" | PLD: fac_ccee_pld_hourly" if col == "carga_liquida_ponta" and pld_dia is not None else ""),
                       rod, OUTPUT_DIR / f"{col}{sufixo}_{tag}.png", pld=pld_dia if col == "carga_liquida_ponta" else None)

    print(f"\n{len(x)} dias na série SIN ({x.index.min().date()} a {x.index.max().date()}) em {time.time()-t0:.0f}s")
    print(x[["carga_media", "carga_liquida_media", "carga_liquida_ponta", "hora_ponta", "origem"]].tail(14).round(0).to_string())
    print(f"\nArquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
