# -*- coding: utf-8 -*-
"""Diagnóstico do erro da previsão de carga do ONS (prev_carga_dessem) contra a carga oficial, por antecedência.

    erro = previsto - oficial      (positivo = previsão acima do oficial)

A previsão roda uma vez por dia, de manhã, com 7 dias à frente. A carga oficial do dia D é o ajuste do
operador que sai na tarde de D-1 (deck DESSEM). Antecedência h = dia alvo - dia da rodada (D+1 a D+7).
O que conta como "oficial" está em OFICIAL, logo abaixo (deck DESSEM ou realizado).

Três recortes, por subsistema (SE, S, NE, N e SIN = soma):
    media    média diária (energia do dia)
    ponta    máximo horário do dia (cada curva com a sua ponta) + diferença na hora da ponta
    perfil   erro hora a hora, e erro de formato (curva / média do dia, em p.p.)

Estatísticas por célula: n, viés (MW e %), desvio padrão, MAE, MAPE, RMSE, quantis P5..P95 e IC 95 % do viés
por bootstrap em blocos semanais (dias seguidos têm erro correlacionado).
Tabela sazonal principal (mês x antecedência) só com dias normais (útil, sábado, domingo); feriados, pontes e
dias especiais saem em tabela própria. Feriados: data/feriados_nacionais.csv (ANBIMA, 2001-2099).

Uso:
    python erro_carga.py                                  # banco, histórico desde INICIO
    python erro_carga.py --ini 2024-01-01 --oficial realizado
    python erro_carga.py --prev-csv prev.csv --oficial-csv oficial.csv   # teste sem banco
    python erro_carga.py --so-relatorio --mes 10 --dias 8-14   # só o relatório em GW, sem banco (usa erro_diario.csv)

Arquivos: só este script e feriados_nacionais.csv (mesma pasta, ou ../data). Banco: CONFIG_PATH.

Saídas em output/erro_carga/<oficial>/, ao lado do script (CSV ; e decimal ,; valores em MW):
    pares_horarios.csv.gz      base hora a hora (rodada, dia, hora, subsistema, h, prev, ofi)
    erro_diario.csv            uma linha por rodada x dia x subsistema (média, ponta, hora da ponta)
    resumo_mes_horizonte.csv   métrica x subsistema x mês x h            (dias normais)
    relatorio_gw.csv           LEITURA DIRETA EM GW: histórico, mês (--mes) e janela de dias (--dias): erro típico,
                               viés, faixa de 80 % dos dias, pior erro; + relatorio_gw_<sub>.png
    quadro_agregado.csv        TODO O HISTÓRICO por antecedência, em GW e %: viés, IC, desvio, MAPE, faixa P5-P95
                               (dias normais e todos os dias, inclusive feriados)
    resumo_horizonte.csv       métrica x subsistema x h                  (dias normais, todos os meses, em MW)
    resumo_horizonte_todos_dias.csv   idem, com feriados, pontes e dias especiais
    resumo_tipo_dia.csv        métrica x subsistema x tipo de dia x h
    resumo_ano_mes.csv         viés por ano e mês (procura de quebras: mudança de modelo, de metodologia...)
    perfil_hora.csv            subsistema x h x hora                    (erro horário e de formato)
    perfil_mes_hora.csv        subsistema x mês x h x hora
    *.png                      gráficos
"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import quote_plus

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap


PASTA = Path(__file__).resolve().parent
OUTPUT_DIR = PASTA / "output" / "erro_carga"
# feriados_nacionais.csv: na mesma pasta do script ou em ../data (estrutura do repositório)
FERIADOS_CSV = next((p for p in [PASTA / "feriados_nacionais.csv", PASTA.parent / "data" / "feriados_nacionais.csv"]
                     if p.exists()), PASTA / "feriados_nacionais.csv")
CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")   # acesso ao banco
INICIO = "2022-01-01"
HORIZONTES = range(1, 8)                 # D+1 a D+7
SUBS = ["SE", "S", "NE", "N", "SIN"]
NOMES_SUB = {"SUDESTE": "SE", "SE/CO": "SE", "SECO": "SE", "SUL": "S", "NORDESTE": "NE", "NORTE": "N"}
N_BOOT = 500                              # reamostragens do bootstrap em blocos semanais
QUANTIS = [0.05, 0.25, 0.50, 0.75, 0.95]

# Previsão (modelo do ONS, 1 rodada por dia, de manhã)
PREV = dict(tabela="fac_sintegre_prev_carga_dessem", rodada="datarodada", tempo="valido_para",
            sub="mnemonico_subsistema", valor="val_previsaocarga",
            rotulo_fim=True)              # valido_para marca o FIM do intervalo de 30 min

# ------------------------------------------------------------------ o que é o "oficial"
# "deck"      carga do deck DESSEM: ajuste do operador publicado na tarde de D-1, a que entra em vigor.
#             rodada do deck = primeiro dia que ele cobre; para cada dia usa delta = dia - rodada = DELTA
#             (0 = deck feito na véspera para o próprio dia; confirmado como o oficial). Sem esse deck, usa a rodada anterior mais próxima.
# "realizado" carga verificada (tempo real ONS), colunas subsistema, dia, hora, carga.
OFICIAL = "deck"
OFICIAIS = {
    "deck": dict(tabela="fac_sintegre_carga_dessem_hourly", rodada="rodada", tempo="valido_para",
                 sub="subsystem", valor="demanda", col_dia="dia", delta=0, rotulo_fim=False),
    "realizado": dict(tabela="fac_ons_carga", rotulo_fim=False),
}

CORES = {"SE": "#2a78d6", "S": "#eb6834", "NE": "#1baf7a", "N": "#eda100", "SIN": "#52514e"}
DIVERGENTE = LinearSegmentedColormap.from_list("div", ["#1c5cab", "#86b6ef", "#f0efec", "#f0a09f", "#b8302f"])
SEQUENCIAL = LinearSegmentedColormap.from_list("seq", ["#f7f9fc", "#86b6ef", "#1c5cab"])
TIPOS_NORMAIS = ["util", "sabado", "domingo"]
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


# ------------------------------------------------------------------ leitura
def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def ler_realizado(engine, ini, fim, tabela, csv=None):
    """Carga verificada (colunas subsistema, dia, hora, carga) -> valido_para, subsistema, mw."""
    if csv:
        d = pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8", thousands=".")
    else:
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SET statement_timeout = '600s'"))
            d = pd.read_sql(text(f"""SELECT subsistema, dia, hora, carga FROM {tabela}
                                     WHERE dia >= :ini AND dia <= :fim"""), con, params={"ini": ini, "fim": fim})
    d["valido_para"] = pd.to_datetime(d.dia.astype(str)) + pd.to_timedelta(d.hora.astype(str))
    d["mw"] = pd.to_numeric(d.carga, errors="coerce")
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    ruim = d.mw > 60000                       # valores implausíveis
    if ruim.any():
        print(f"AVISO {tabela}: {int(ruim.sum())} valores acima de 60000 MW descartados", flush=True)
        d = d[~ruim]
    return d[["valido_para", "subsistema", "mw"]]


def _ler_csv(csv):
    return pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")


def _consulta_em_lotes(engine, sql, col_filtro, ini, fim, meses=3):
    """SELECT em janelas de N meses sobre col_filtro (evita timeout em histórico longo)."""
    from sqlalchemy import text
    partes = []
    cortes = list(pd.date_range(ini, fim, freq=f"{meses}MS")) + [fim + pd.Timedelta(days=1)]
    if cortes[0] > ini:
        cortes.insert(0, ini)
    with engine.connect() as con:
        con.execute(text("SET statement_timeout = '600s'"))
        for a, b in zip(cortes[:-1], cortes[1:]):
            partes.append(pd.read_sql(text(f"{sql} WHERE {col_filtro} >= :a AND {col_filtro} < :b"), con,
                                      params={"a": a.date(), "b": b.date()}))
            print(f"  {a.date()} a {(b - pd.Timedelta(days=1)).date()}: {len(partes[-1]):,} linhas", flush=True)
    return pd.concat(partes, ignore_index=True)


def _subsistema(s):
    s = s.astype(str).str.strip().str.upper()
    return s.replace(NOMES_SUB)


def _horario(d, rotulo_fim):
    """valido_para -> hora cheia de início; média das meias-horas da mesma hora."""
    d["valido_para"] = pd.to_datetime(d.valido_para)
    if rotulo_fim:
        d["valido_para"] -= pd.Timedelta(minutes=30)
    d["valido_para"] = d.valido_para.dt.floor("h")
    d["mw"] = pd.to_numeric(d.mw, errors="coerce")
    d["subsistema"] = _subsistema(d.subsistema)
    chaves = [c for c in ["rodada", "valido_para", "subsistema"] if c in d]
    return d.dropna(subset=["mw"]).groupby(chaves, as_index=False).mw.mean()


def _com_sin(d, chaves):
    """Acrescenta SIN = soma dos 4 subsistemas (só onde os 4 existem)."""
    d = d[d.subsistema.isin(SUBS[:4])]
    s = d.groupby(chaves).mw.agg(["sum", "size"])
    s = s[s["size"] == 4].reset_index().rename(columns={"sum": "mw"}).drop(columns="size")
    s["subsistema"] = "SIN"
    return pd.concat([d, s], ignore_index=True)


def ler_previsao(engine, ini, fim, csv=None):
    c = PREV
    t0 = time.time()
    if csv:
        d = _ler_csv(csv)
    else:
        d = _consulta_em_lotes(engine, f"SELECT {c['rodada']}, {c['tempo']}, {c['sub']}, {c['valor']} FROM {c['tabela']}",
                               c["rodada"], ini - pd.Timedelta(days=7), fim)
    d = d.rename(columns={c["rodada"]: "rodada", c["tempo"]: "valido_para", c["sub"]: "subsistema", c["valor"]: "mw"})
    d["rodada"] = pd.to_datetime(d.rodada).dt.normalize()
    d = d.drop_duplicates(["rodada", "valido_para", "subsistema"], keep="last")   # 1 rodada por dia
    d = _horario(d[["rodada", "valido_para", "subsistema", "mw"]].copy(), c["rotulo_fim"])
    d = _com_sin(d, ["rodada", "valido_para"])
    print(f"previsão: {d.rodada.nunique():,} rodadas ({d.rodada.min().date()} a {d.rodada.max().date()}), "
          f"subsistemas {sorted(d.subsistema.unique())} ({time.time()-t0:.0f}s)", flush=True)
    return d


def ler_oficial(engine, ini, fim, csv=None, qual=OFICIAL):
    c = OFICIAIS[qual]
    t0 = time.time()
    if qual == "realizado":
        d = ler_realizado(engine, ini.date(), fim.date(), c["tabela"], csv)
        d = _horario(d.copy(), c["rotulo_fim"])
    else:
        if csv:
            d = _ler_csv(csv)
        else:
            d = _consulta_em_lotes(engine, f"SELECT {c['rodada']}, {c['tempo']}, {c['sub']}, {c['valor']} FROM {c['tabela']}",
                                   c["col_dia"], ini, fim)
        d = d.rename(columns={c["rodada"]: "rodada", c["tempo"]: "valido_para", c["sub"]: "subsistema", c["valor"]: "mw"})
        d["rodada"] = pd.to_datetime(d.rodada).dt.normalize()
        d = _horario(d[["rodada", "valido_para", "subsistema", "mw"]].copy(), c["rotulo_fim"])
        d["dia"] = d.valido_para.dt.normalize()
        d["delta"] = (d.dia - d.rodada).dt.days
        d = d[d.delta >= c["delta"]]
        melhor = d.groupby(["dia", "subsistema"]).delta.transform("min")
        d = d[d.delta == melhor]
        atras = d[d.delta > c["delta"]].dia.drop_duplicates()
        if len(atras):
            print(f"oficial: {len(atras)} dia(s) sem deck com delta {c['delta']}, usada a rodada anterior "
                  f"(ex.: {[x.date().isoformat() for x in atras[:5]]})", flush=True)
        d = d.drop(columns=["rodada", "dia", "delta"])
    d = _com_sin(d, ["valido_para"])
    print(f"oficial ({qual}): {d.valido_para.min().date()} a {d.valido_para.max().date()}, "
          f"subsistemas {sorted(d.subsistema.unique())} ({time.time()-t0:.0f}s)", flush=True)
    return d


# ------------------------------------------------------------------ calendário
def ler_feriados(csv=FERIADOS_CSV):
    f = pd.read_csv(csv, sep=";", encoding="utf-8")
    return pd.Series(f.feriado.values, index=pd.to_datetime(f.data))


def tipo_dia(dias, feriados):
    """util, sabado, domingo, feriado (em dia útil), ponte (útil entre feriado e fim de semana),
    especial (quarta de cinzas, 24 a 31/12). Feriado no fim de semana fica como sábado/domingo."""
    dias = pd.DatetimeIndex(pd.Series(dias).drop_duplicates().sort_values())
    fer = set(feriados.index)
    folga = lambda d: d.dayofweek >= 5 or d in fer
    cinzas = {d + pd.Timedelta(days=1) for d, n in feriados.items() if n == "Carnaval" and d.dayofweek == 1}
    out = {}
    for d in dias:
        if d.dayofweek == 5:
            t = "sabado"
        elif d.dayofweek == 6:
            t = "domingo"
        elif d in fer:
            t = "feriado"
        elif d in cinzas or (d.month == 12 and d.day >= 24):
            t = "especial"
        elif (d - pd.Timedelta(days=1) in fer and folga(d + pd.Timedelta(days=1))) or \
             (d + pd.Timedelta(days=1) in fer and folga(d - pd.Timedelta(days=1))):
            t = "ponte"
        else:
            t = "util"
        out[d] = t
    return pd.Series(out)


# ------------------------------------------------------------------ pares e erros
def montar_pares(prev, ofi):
    p = prev.merge(ofi.rename(columns={"mw": "ofi"}), on=["valido_para", "subsistema"]).rename(columns={"mw": "prev"})
    p["dia"] = p.valido_para.dt.normalize()
    p["hora"] = p.valido_para.dt.hour
    p["h"] = (p.dia - p.rodada).dt.days
    p = p[p.h.isin(HORIZONTES)].drop(columns="valido_para")
    return p[["rodada", "dia", "hora", "subsistema", "h", "prev", "ofi"]].sort_values(["rodada", "subsistema", "dia", "hora"])


def erro_diario(pares, feriados):
    g = pares.groupby(["rodada", "dia", "subsistema", "h"])
    d = g.agg(n=("prev", "size"), prev_media=("prev", "mean"), ofi_media=("ofi", "mean"),
              prev_ponta=("prev", "max"), ofi_ponta=("ofi", "max")).reset_index()
    d["hora_ponta_prev"] = pares.loc[g.prev.idxmax().values, "hora"].values
    d["hora_ponta_ofi"] = pares.loc[g.ofi.idxmax().values, "hora"].values
    incompletos = (d.n < 24).sum()
    if incompletos:
        print(f"{incompletos:,} dias-subsistema com menos de 24 h descartados", flush=True)
    d = d[d.n == 24].drop(columns="n")
    d["dif_hora_ponta"] = d.hora_ponta_prev - d.hora_ponta_ofi
    return _calendario(d, feriados)


def _calendario(d, feriados):
    tipos = tipo_dia(d.dia, feriados)
    d["tipo_dia"] = d.dia.map(tipos)
    d["mes"] = d.dia.dt.month
    d["ano"] = d.dia.dt.year
    d["dia_semana_rodada"] = d.rodada.dt.dayofweek
    d["semana"] = (d.dia - pd.Timestamp("1970-01-05")).dt.days // 7     # bloco do bootstrap
    return d


def longo_diario(d):
    """Formato longo: uma linha por rodada x dia x subsistema x métrica, com erro em MW e %."""
    base = ["rodada", "dia", "subsistema", "h", "tipo_dia", "mes", "ano", "semana"]
    partes = []
    for m in ["media", "ponta"]:
        x = d[base].copy()
        x["metrica"] = m
        x["prev"], x["ofi"] = d[f"prev_{m}"], d[f"ofi_{m}"]
        partes.append(x)
    x = pd.concat(partes, ignore_index=True)
    x["erro_mw"] = x.prev - x.ofi
    x["erro_pct"] = 100 * x.erro_mw / x.ofi
    return x


def erro_perfil(pares, diario):
    p = pares.merge(diario[["rodada", "dia", "subsistema", "prev_media", "ofi_media", "tipo_dia", "mes", "semana"]],
                    on=["rodada", "dia", "subsistema"])
    p["erro_mw"] = p.prev - p.ofi
    p["erro_pct"] = 100 * p.erro_mw / p.ofi
    p["erro_forma_pp"] = 100 * (p.prev / p.prev_media - p.ofi / p.ofi_media)   # formato, sem o nível do dia
    return p


# ------------------------------------------------------------------ estatísticas
def _ic_bootstrap(x, rng):
    """IC 95 % do viés reamostrando semanas inteiras (blocos)."""
    s = x.groupby("semana").erro_mw.agg(["sum", "size"])
    if len(s) < 4:
        return np.nan, np.nan
    idx = rng.integers(0, len(s), (N_BOOT, len(s)))
    medias = s["sum"].values[idx].sum(1) / s["size"].values[idx].sum(1)
    return np.percentile(medias, 2.5), np.percentile(medias, 97.5)


def resumir(x, chaves, boot=True, extras=("erro_pct",)):
    g = x.groupby(chaves)
    r = g.agg(n=("erro_mw", "size"), n_semanas=("semana", "nunique"),
              vies_mw=("erro_mw", "mean"), desvio_mw=("erro_mw", "std"),
              mae_mw=("erro_mw", lambda e: e.abs().mean()), rmse_mw=("erro_mw", lambda e: np.sqrt((e ** 2).mean())),
              vies_pct=("erro_pct", "mean"), desvio_pct=("erro_pct", "std"),
              mape=("erro_pct", lambda e: e.abs().mean()))
    q = g.erro_mw.quantile(QUANTIS).unstack()
    q.columns = [f"p{int(100*c):02d}_mw" for c in q.columns]
    qp = g.erro_pct.quantile(QUANTIS).unstack()
    qp.columns = [f"p{int(100*c):02d}_pct" for c in qp.columns]
    r = r.join(q).join(qp)
    for e in extras:
        if e != "erro_pct":
            r[f"vies_{e}"], r[f"desvio_{e}"] = g[e].mean(), g[e].std()
    if boot:
        rng = np.random.default_rng(0)
        ic = g.apply(lambda s: pd.Series(_ic_bootstrap(s, rng), index=["vies_ic95_inf_mw", "vies_ic95_sup_mw"]),
                     include_groups=False)
        r = r.join(ic)
    return r.reset_index()


# ------------------------------------------------------------------ gráficos
def _estilo(ax):
    ax.grid(True, color="#e4e3df", lw=0.6)
    ax.set_axisbelow(True)
    for s in ["top", "right"]:
        ax.spines[s].set_visible(False)
    for s in ["left", "bottom"]:
        ax.spines[s].set_color("#b5b4ae")
    ax.tick_params(colors="#52514e", labelsize=8)


def grafico_horizonte(res, saida):
    fig, axs = plt.subplots(2, 3, figsize=(15, 7.5), sharex=True)
    for i, m in enumerate(["media", "ponta"]):
        r = res[res.metrica == m]
        for j, (col, tit) in enumerate([("vies_pct", "viés (%)"), ("desvio_pct", "desvio padrão (%)"), ("mape", "MAPE (%)")]):
            ax = axs[i, j]
            for s in SUBS:
                rs = r[r.subsistema == s].sort_values("h")
                ax.plot(rs.h, rs[col], marker="o", ms=4, lw=2 if s == "SIN" else 1.5,
                        ls="--" if s == "SIN" else "-", color=CORES[s], label=s)
            if col == "vies_pct":
                ax.axhline(0, color="#52514e", lw=0.8)
            ax.set_title(f"{'Média diária' if m == 'media' else 'Ponta diária'}  -  {tit}", fontsize=10, loc="left")
            _estilo(ax)
    for ax in axs[1]:
        ax.set_xlabel("antecedência (dias)", fontsize=9)
        ax.set_xticks(list(HORIZONTES), [f"D+{h}" for h in HORIZONTES])
    axs[0, 0].legend(fontsize=8, frameon=False, ncol=5, loc="upper left")
    fig.suptitle("Erro da previsão (prev_carga_dessem) contra o oficial, por antecedência  -  dias normais",
                 fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "horizonte.png", dpi=130)
    plt.close(fig)


def _mapa(ax, tab, cmap, centro_zero):
    v = tab.values
    lim = np.nanmax(np.abs(v)) if centro_zero else np.nanmax(v)
    fmt = "{:.2f}" if lim < 1 else "{:.1f}"
    im = ax.imshow(v, aspect="auto", cmap=cmap, vmin=-lim if centro_zero else 0, vmax=lim)
    for (a, b), val in np.ndenumerate(v):
        if np.isfinite(val):
            escuro = abs(val) > 0.6 * lim
            txt = fmt.format(val).replace("-", "") if float(fmt.format(val)) == 0 else fmt.format(val)
            ax.text(b, a, txt, ha="center", va="center", fontsize=6.5, color="white" if escuro else "#0b0b0b")
    ax.set_xticks(range(tab.shape[1]), [f"D+{h}" for h in tab.columns], fontsize=7)
    return im


def grafico_mes_horizonte(res, saida):
    for m in ["media", "ponta"]:
        r = res[res.metrica == m]
        fig, axs = plt.subplots(len(SUBS), 2, figsize=(9, 3.1 * len(SUBS)))
        for i, s in enumerate(SUBS):
            rs = r[r.subsistema == s]
            for j, (col, tit, cmap, z) in enumerate([("vies_pct", "viés (%)", DIVERGENTE, True),
                                                       ("desvio_pct", "desvio padrão (%)", SEQUENCIAL, False)]):
                tab = rs.pivot(index="mes", columns="h", values=col).reindex(range(1, 13))
                ax = axs[i, j]
                im = _mapa(ax, tab, cmap, z)
                ax.set_yticks(range(12), MESES, fontsize=7)
                ax.set_title(f"{s}  -  {tit}", fontsize=9, loc="left")
                fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02).ax.tick_params(labelsize=7)
        fig.suptitle(f"{'Média' if m == 'media' else 'Ponta'} diária: erro por mês e antecedência  "
                     f"(vermelho = previsão acima do oficial)", fontsize=10, x=0.01, ha="left")
        fig.tight_layout()
        fig.savefig(saida / f"mes_horizonte_{m}.png", dpi=130)
        plt.close(fig)


def grafico_leque(res, saida):
    fig, axs = plt.subplots(2, len(SUBS), figsize=(3.4 * len(SUBS), 6.5), sharex=True)
    for i, m in enumerate(["media", "ponta"]):
        r = res[res.metrica == m]
        for j, s in enumerate(SUBS):
            rs = r[r.subsistema == s].sort_values("h")
            ax = axs[i, j]
            c = CORES[s]
            ax.fill_between(rs.h, rs.p05_mw, rs.p95_mw, color=c, alpha=0.18, lw=0, label="P5-P95")
            ax.fill_between(rs.h, rs.p25_mw, rs.p75_mw, color=c, alpha=0.38, lw=0, label="P25-P75")
            ax.plot(rs.h, rs.p50_mw, color=c, lw=2, label="mediana")
            ax.plot(rs.h, rs.vies_mw, color="#0b0b0b", lw=1, ls=":", label="média")
            ax.axhline(0, color="#52514e", lw=0.8)
            ax.set_title(f"{s}  -  {'média' if m == 'media' else 'ponta'}", fontsize=9, loc="left")
            ax.set_xticks(list(HORIZONTES), [f"D+{h}" for h in HORIZONTES], fontsize=7)
            _estilo(ax)
        axs[i, 0].set_ylabel("erro (MW)", fontsize=8)
    axs[0, 0].legend(fontsize=7, frameon=False, loc="upper left")
    fig.suptitle("Distribuição do erro por antecedência (MW)  -  dias normais", fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "leque.png", dpi=130)
    plt.close(fig)


def grafico_perfil(perf, saida):
    fig, axs = plt.subplots(2, len(SUBS), figsize=(3.4 * len(SUBS), 8), sharey=True)
    for j, s in enumerate(SUBS):
        rs = perf[perf.subsistema == s]
        for i, (col, tit) in enumerate([("vies_pct", "viés horário (%)"), ("vies_erro_forma_pp", "viés de formato (p.p.)")]):
            tab = rs.pivot(index="hora", columns="h", values=col)
            ax = axs[i, j]
            im = _mapa(ax, tab, DIVERGENTE, True)
            ax.set_yticks(range(24), [f"{h:02d}h" for h in range(24)], fontsize=6.5)
            ax.set_title(f"{s}  -  {tit}", fontsize=9, loc="left")
            fig.colorbar(im, ax=ax, fraction=0.05, pad=0.02).ax.tick_params(labelsize=7)
    fig.suptitle("Perfil: erro por hora do dia e antecedência  -  dias normais  "
                 "(formato = curva/média do dia; separa o erro de forma do erro de nível)", fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "perfil_hora_horizonte.png", dpi=130)
    plt.close(fig)


def grafico_serie(anual, saida):
    r = anual[anual.metrica == "media"].copy()
    r["data"] = pd.to_datetime(dict(year=r.ano, month=r.mes, day=1))
    fig, axs = plt.subplots(len(SUBS), 1, figsize=(12, 2.2 * len(SUBS)), sharex=True)
    for ax, s in zip(axs, SUBS):
        for h, ls in [(1, "-"), (4, "--"), (7, ":")]:
            rs = r[(r.subsistema == s) & (r.h == h)].sort_values("data")
            ax.plot(rs.data, rs.vies_pct, ls=ls, color=CORES[s], lw=1.6, label=f"D+{h}")
        ax.axhline(0, color="#52514e", lw=0.8)
        ax.set_title(f"{s}  -  viés mensal da média diária (%)", fontsize=9, loc="left")
        _estilo(ax)
    axs[0].legend(fontsize=8, frameon=False, ncol=3, loc="upper left")
    fig.suptitle("Viés ao longo do tempo: degraus indicam quebra (mudança de modelo ou de metodologia da carga)",
                 fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "serie_vies_mensal.png", dpi=130)
    plt.close(fig)


# ------------------------------------------------------------------ checagens
def checar_alinhamento(pares_d1):
    """MAE de D+1 deslocando o oficial em -1, 0 e +1 h: se o mínimo não for 0, a convenção de rótulo está trocada."""
    p = pares_d1[pares_d1.subsistema == "SIN"]
    p = p.assign(t=p.dia + pd.to_timedelta(p.hora, unit="h"))
    ofi = p.drop_duplicates("t").set_index("t").ofi
    prev = p.drop_duplicates("t").set_index("t").prev
    mae = {k: (prev - ofi.shift(k, freq="h")).abs().mean() for k in (-1, 0, 1)}
    print("alinhamento (MAE SIN, D+1, oficial deslocado em h): " + ", ".join(f"{k:+d}h {v:,.0f} MW" for k, v in mae.items()), flush=True)
    if min(mae, key=mae.get) != 0:
        print("AVISO: o menor erro não está no deslocamento 0 -> conferir rotulo_fim em PREV/OFICIAIS", flush=True)


def _salvar(df, nome, saida):
    df.to_csv(saida / nome, sep=";", decimal=",", index=False, encoding="utf-8-sig", float_format="%.3f")


def quadro_agregado(res_normais, res_todos):
    """Todo o histórico, por antecedência, em GW e %: a resposta direta 'em D+x erra y GW, com tal dispersão'."""
    partes = []
    for dias, r in [("normais", res_normais), ("todos", res_todos)]:
        q = r[["metrica", "subsistema", "h", "n"]].copy()
        q.insert(0, "dias", dias)
        q["vies_gw"] = r.vies_mw / 1000
        q["vies_ic95_inf_gw"], q["vies_ic95_sup_gw"] = r.vies_ic95_inf_mw / 1000, r.vies_ic95_sup_mw / 1000
        q["vies_pct"] = r.vies_pct
        q["desvio_gw"], q["desvio_pct"] = r.desvio_mw / 1000, r.desvio_pct
        q["mae_gw"], q["mape"] = r.mae_mw / 1000, r.mape
        for c in ["p05", "p25", "p50", "p75", "p95"]:
            q[f"{c}_gw"] = r[f"{c}_mw"] / 1000
        q["p05_pct"], q["p95_pct"] = r.p05_pct, r.p95_pct
        partes.append(q)
    q = pd.concat(partes, ignore_index=True)
    q["subsistema"] = pd.Categorical(q.subsistema, SUBS, ordered=True)
    return q.sort_values(["dias", "metrica", "subsistema", "h"])


def imprimir_resumo(quadro, dias="normais"):
    print(f"\nTodo o histórico, dias {dias} (viés = previsto - oficial; faixa = P5 a P95):")
    for m in ["media", "ponta"]:
        for s in SUBS:
            r = quadro[(quadro.dias == dias) & (quadro.metrica == m) & (quadro.subsistema == s)]
            print(f"\n{s} - {m}")
            print(f"{'h':>5}{'n':>6}{'viés GW':>9}{'viés %':>8}{'desvio GW':>11}{'desvio %':>10}{'MAPE':>7}{'faixa GW':>18}")
            for _, x in r.iterrows():
                print(f"{'D+'+str(x.h):>5}{x.n:>6}{x.vies_gw:>9.2f}{x.vies_pct:>8.2f}{x.desvio_gw:>11.2f}{x.desvio_pct:>10.2f}"
                      f"{x.mape:>7.2f}{f'{x.p05_gw:+.2f} a {x.p95_gw:+.2f}':>18}")


# ------------------------------------------------------------------ relatório em GW (leitura direta)
def recortes_gw(longo, mes, dias):
    """Todo o histórico, o mês escolhido e uma janela de dias desse mês (todos os anos). Todos os tipos de dia."""
    nome_mes = MESES[mes - 1]
    r = {"historico": ("todo o histórico", longo), "mes": (f"{nome_mes} (todos os anos)", longo[longo.mes == mes])}
    if dias:
        d1, d2 = dias
        j = longo[(longo.mes == mes) & longo.dia.dt.day.between(d1, d2)]
        r["janela"] = (f"{d1} a {d2}/{nome_mes} (todos os anos)", j)
    return r


def tabela_gw(x):
    g = x.groupby(["metrica", "subsistema", "h"]).erro_mw
    t = pd.DataFrame({"n_dias": g.size(), "n_anos": x.groupby(["metrica", "subsistema", "h"]).ano.nunique(),
                      "erro_tipico_gw": g.apply(lambda e: e.abs().mean()) / 1000,
                      "vies_gw": g.mean() / 1000,
                      "p10_gw": g.quantile(0.10) / 1000, "p90_gw": g.quantile(0.90) / 1000,
                      "pior_abaixo_gw": g.min() / 1000, "pior_acima_gw": g.max() / 1000,
                      "mape": x.groupby(["metrica", "subsistema", "h"]).erro_pct.apply(lambda e: e.abs().mean())})
    return t.reset_index()


def relatorio_gw(longo, mes, dias, saida, sub="SIN"):
    recs = recortes_gw(longo, mes, dias)
    tabs = []
    for chave, (rotulo, x) in recs.items():
        if len(x):
            tabs.append(tabela_gw(x).assign(recorte=chave, descricao=rotulo))
    tab = pd.concat(tabs, ignore_index=True)
    tab = tab[["recorte", "descricao"] + [c for c in tab.columns if c not in ("recorte", "descricao")]]
    _salvar(tab, "relatorio_gw.csv", saida)

    print("\n" + "=" * 96)
    print(f"QUANTO O MODELO ERRA, EM GW  -  {sub}  (erro = previsto - oficial; negativo = previsão abaixo do oficial)")
    print("  erro típico = média do erro sem sinal (quanto erra, para cima ou para baixo)")
    print("  viés        = média com sinal (para que lado costuma errar)")
    print("  80 % dos dias = entre P10 e P90;   pior = maior erro observado para cada lado")
    for chave, (rotulo, _) in recs.items():
        t = tab[(tab.recorte == chave) & (tab.subsistema == sub)]
        if t.empty:
            continue
        for m in ["media", "ponta"]:
            tm = t[t.metrica == m]
            print(f"\n{rotulo}  -  {'média diária' if m == 'media' else 'ponta diária'}"
                  f"  ({tm.n_dias.min()}-{tm.n_dias.max()} dias, {tm.n_anos.max()} anos)")
            print(f"{'':>5}{'erro típico':>13}{'viés':>8}{'80 % dos dias':>20}{'pior abaixo':>13}{'pior acima':>12}")
            for _, z in tm.iterrows():
                print(f"{'D+'+str(z.h):>5}{z.erro_tipico_gw:>10.2f} GW{z.vies_gw:>+8.2f}"
                      f"{f'{z.p10_gw:+.2f} a {z.p90_gw:+.2f}':>20}{z.pior_abaixo_gw:>+13.2f}{z.pior_acima_gw:>+12.2f}")
    if "janela" in recs:
        rotulo, x = recs["janela"]
        x = x[x.subsistema == sub]
        if len(x):
            print(f"\n{rotulo}: ano a ano  -  viés da média diária (GW); cada ano é uma amostra pequena (~7 dias)")
            a = (x[x.metrica == "media"].groupby(["ano", "h"]).erro_mw.mean() / 1000).unstack()
            a.columns = [f"D+{h}" for h in a.columns]
            print(a.round(2).to_string())
            fer = x[(x.metrica == "media") & (x.h == 1) & ~x.tipo_dia.isin(TIPOS_NORMAIS)]
            if len(fer):
                print(f"(inclui {fer.dia.nunique()} dia(s) de feriado/ponte/especial: "
                      f"{sorted({d.strftime('%d/%m/%Y') for d in fer.dia})})")
    grafico_gw(tab, recs, saida, sub)
    return tab


def grafico_gw(tab, recs, saida, sub):
    chaves = [k for k in recs if k in set(tab.recorte)]
    fig, axs = plt.subplots(2, len(chaves), figsize=(4.6 * len(chaves), 7), sharex=True, squeeze=False)
    cor = "#2a78d6"
    for i, m in enumerate(["media", "ponta"]):
        lims = tab[(tab.metrica == m) & (tab.subsistema == sub)]
        lo, hi = lims.p10_gw.min(), lims.p90_gw.max()
        for j, k in enumerate(chaves):
            t = tab[(tab.recorte == k) & (tab.metrica == m) & (tab.subsistema == sub)].sort_values("h")
            ax = axs[i, j]
            ax.fill_between(t.h, t.p10_gw, t.p90_gw, color=cor, alpha=0.22, lw=0, label="80 % dos dias (P10-P90)")
            ax.plot(t.h, t.vies_gw, color=cor, lw=2, marker="o", ms=4, label="viés (média com sinal)")
            ax.axhline(0, color="#52514e", lw=0.8)
            for _, z in t.iterrows():
                ax.annotate(f"±{z.erro_tipico_gw:.1f}", (z.h, z.p90_gw), textcoords="offset points", xytext=(0, 4),
                            ha="center", fontsize=7.5, color="#0b0b0b")
            ax.set_ylim(lo - 0.15 * (hi - lo), hi + 0.2 * (hi - lo))
            ax.set_title(f"{recs[k][0]}\n{'média' if m == 'media' else 'ponta'} diária", fontsize=9, loc="left")
            ax.set_xticks(list(HORIZONTES), [f"D+{h}" for h in HORIZONTES], fontsize=7)
            _estilo(ax)
        axs[i, 0].set_ylabel("erro (GW)", fontsize=8)
    axs[0, 0].legend(fontsize=7, frameon=False, loc="lower left")
    fig.suptitle(f"{sub}: quanto a previsão erra em GW por antecedência  (±x = erro típico; negativo = abaixo do oficial)",
                 fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / f"relatorio_gw_{sub}.png", dpi=130)
    plt.close(fig)


def ler_erro_diario(saida):
    d = pd.read_csv(saida / "erro_diario.csv", sep=";", decimal=",", encoding="utf-8-sig", parse_dates=["rodada", "dia"])
    return d


# ------------------------------------------------------------------ principal
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ini", default=INICIO)
    ap.add_argument("--fim", default=None, help="padrão: hoje")
    ap.add_argument("--oficial", default=OFICIAL, choices=list(OFICIAIS))
    ap.add_argument("--prev-csv", help="teste: CSV no formato de fac_sintegre_prev_carga_dessem")
    ap.add_argument("--oficial-csv", help="teste: CSV no formato da tabela do oficial escolhido")
    ap.add_argument("--saida", default=str(OUTPUT_DIR))
    ap.add_argument("--mes", type=int, default=pd.Timestamp.today().month, help="mês do relatório em GW (padrão: atual)")
    ap.add_argument("--dias", default="8-14", help="janela de dias do mês no relatório em GW, ex.: 8-14 (2ª semana); "
                                                   "'' desliga")
    ap.add_argument("--sub", default="SIN", choices=SUBS, help="subsistema impresso no relatório em GW")
    ap.add_argument("--so-relatorio", action="store_true",
                    help="não consulta o banco: refaz só o relatório em GW a partir do erro_diario.csv já gerado")
    a = ap.parse_args()

    ini = pd.Timestamp(a.ini)
    fim = pd.Timestamp(a.fim) if a.fim else pd.Timestamp.today().normalize()
    saida = Path(a.saida) / a.oficial
    saida.mkdir(parents=True, exist_ok=True)
    dias = tuple(int(v) for v in a.dias.split("-")) if a.dias else None
    if a.so_relatorio:
        relatorio_gw(longo_diario(ler_erro_diario(saida)), a.mes, dias, saida, a.sub)
        print(f"\nsaídas em {saida}", flush=True)
        return
    eng = None
    if not (a.prev_csv and a.oficial_csv):
        eng = engine_banco()

    prev = ler_previsao(eng, ini, fim, a.prev_csv)
    ofi = ler_oficial(eng, ini, fim, a.oficial_csv, a.oficial)
    pares = montar_pares(prev, ofi)
    pares = pares[(pares.dia >= ini) & (pares.dia <= fim)]
    print(f"pares horários: {len(pares):,} ({pares.dia.min().date()} a {pares.dia.max().date()})", flush=True)
    checar_alinhamento(pares[pares.h == 1])

    feriados = ler_feriados()
    diario = erro_diario(pares, feriados)
    longo = longo_diario(diario)
    normais = longo[longo.tipo_dia.isin(TIPOS_NORMAIS)]
    print(f"dias por tipo (D+1, SIN): {diario[(diario.h == 1) & (diario.subsistema == 'SIN')].tipo_dia.value_counts().to_dict()}",
          flush=True)

    t0 = time.time()
    res_h = resumir(normais, ["metrica", "subsistema", "h"])
    res_h_todos = resumir(longo, ["metrica", "subsistema", "h"])
    res_mh = resumir(normais, ["metrica", "subsistema", "mes", "h"])
    res_tipo = resumir(longo, ["metrica", "subsistema", "tipo_dia", "h"])
    res_ano = resumir(normais, ["metrica", "subsistema", "ano", "mes", "h"], boot=False)
    hp = diario[diario.tipo_dia.isin(TIPOS_NORMAIS)].groupby(["subsistema", "h"]).dif_hora_ponta.agg(
        dif_hora_ponta_media="mean", acerto_hora_ponta=lambda x: 100 * (x == 0).mean(),
        acerto_hora_ponta_1h=lambda x: 100 * (x.abs() <= 1).mean()).reset_index()
    res_h = res_h.merge(hp.assign(metrica="ponta"), on=["metrica", "subsistema", "h"], how="left")

    perfil = erro_perfil(pares, diario)
    perfil_n = perfil[perfil.tipo_dia.isin(TIPOS_NORMAIS)]
    perf_h = resumir(perfil_n, ["subsistema", "h", "hora"], boot=False, extras=("erro_pct", "erro_forma_pp"))
    perf_mh = resumir(perfil_n, ["subsistema", "mes", "h", "hora"], boot=False, extras=("erro_pct", "erro_forma_pp"))
    print(f"estatísticas: {time.time()-t0:.0f}s", flush=True)

    pares.to_csv(saida / "pares_horarios.csv.gz", sep=";", decimal=",", index=False, float_format="%.1f")
    _salvar(diario, "erro_diario.csv", saida)
    quadro = quadro_agregado(res_h, res_h_todos)
    _salvar(quadro, "quadro_agregado.csv", saida)
    _salvar(res_h_todos, "resumo_horizonte_todos_dias.csv", saida)
    for df, nome in [(res_mh, "resumo_mes_horizonte.csv"), (res_h, "resumo_horizonte.csv"),
                     (res_tipo, "resumo_tipo_dia.csv"), (res_ano, "resumo_ano_mes.csv"),
                     (perf_h, "perfil_hora.csv"), (perf_mh, "perfil_mes_hora.csv")]:
        _salvar(df, nome, saida)

    grafico_horizonte(res_h, saida)
    grafico_mes_horizonte(res_mh, saida)
    grafico_leque(res_h, saida)
    grafico_perfil(perf_h, saida)
    grafico_serie(res_ano, saida)
    imprimir_resumo(quadro, "normais")
    imprimir_resumo(quadro, "todos")
    relatorio_gw(longo, a.mes, dias, saida, a.sub)
    print(f"\nsaídas em {saida}", flush=True)


if __name__ == "__main__":
    main()
