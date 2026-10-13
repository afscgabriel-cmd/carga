# -*- coding: utf-8 -*-
"""Diagnóstico do erro da previsão eólica da Tempo OK (WEOL) contra a eólica do deck DESSEM, por antecedência.

    r = 100 x (deck / previsão - 1)      em %; positivo = deck ACIMA da previsão
    erro_mw = deck - previsão            mesmo sinal de r

Previsão: fac_tempook_geracao_eolica_hourly (1 rodada por dia, D+0 a D+9, 30 min, GW).
Verdade ("oficial"): fac_ons_renovaveis, tipo_fonte_energia = 'UEE', deck com rodada_dia = D e delta 0
(valido_para_dia = rodada_dia): o deck do dia D, publicado na tarde de D-1, que forma o preço de D.
Antecedência h = dia alvo - dia da rodada da previsão, D+1 a D+9 (o D+0 fica de fora: quando a previsão do dia D
roda, o deck de D já existe).

Recortes, por subsistema (SE, S, NE, N e SIN = soma dos 4):
    media    média das 24 h
    ponta    média das horas HORAS_PONTA (padrão 18, 19 e 20 h = 18:00 às 21:00)
    perfil   erro hora a hora (MW e r mediano)

Diagnóstico (Fase 2): viés, erro médio (|r|), desvio e quantis por antecedência, mês, ano e faixa de vento
prevista (fraco / normal / forte, pela previsão do dia contra a média do deck nos 30 dias antes da rodada),
mais o IC 95 % do viés por bootstrap em blocos semanais. As tabelas por ano e por faixa testam se o erro em %
é estável (unidade dos cenários): se for, os cenários seguem o método da carga.

Uso:
    python erro_eolica.py                       # banco, desde INICIO (terminal enxuto: SIN e NE)
    python erro_eolica.py --detalhado           # terminal completo (todos os subsistemas, mês x antecedência)
    python erro_eolica.py --ini 2024-01-01
    python erro_eolica.py --so-relatorio        # sem banco: refaz o relatório a partir do erro_diario.csv
    python erro_eolica.py --prev-csv p.csv --oficial-csv o.csv   # teste sem banco

Arquivos: só este script. Dias a descartar (dado ruim): dias_excluidos_eolica.csv na mesma pasta (dia;motivo).
Saídas em output/erro_eolica/ (CSV com ; e decimal ,):
    pares_horarios.csv.gz    base hora a hora (rodada, dia, hora, subsistema, h, prev, ofi) em MW
    erro_diario.csv          rodada x dia x subsistema: média e ponta, prevista e do deck, faixa de vento
    resumo_horizonte.csv     métrica x subsistema x h
    resumo_mes_horizonte.csv métrica x subsistema x mês x h
    resumo_ano.csv           métrica x subsistema x ano x h      (estabilidade do erro em % ao longo dos anos)
    resumo_faixa.csv         métrica x subsistema x faixa x h    (o erro em % explode com pouco vento?)
    perfil_hora.csv          subsistema x h x hora
    cobertura.csv            rodadas da previsão, dias do deck e dias pareados por mês
    piores_dias.csv          maiores |r| por métrica e subsistema, com r de cada subsistema ao lado
    *.png                    horizonte.png, mes_horizonte.png, estabilidade.png
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
OUTPUT_DIR = PASTA / "output" / "erro_eolica"
CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")
DIAS_EXCLUIDOS_CSV = PASTA / "dias_excluidos_eolica.csv"   # opcional: dia;motivo (separado do da carga)
INICIO = "2023-10-01"                    # a previsão Tempo OK começa em 06/10/2023
HORIZONTES = list(range(1, 10))          # D+1 a D+9
HORAS_PONTA = [18, 19, 20]               # ponta = média das horas que começam às 18, 19 e 20 h
SUBS = ["SE", "S", "NE", "N", "SIN"]
SUBS_TERMINAL = ["SIN", "NE"]
SUBS_SIN = ["NE", "S", "N"]              # SIN = NE + S + N. O SE (~30 MW, ~0,2 % do total) fica fora: falta no deck
                                         # ou na previsão em parte do histórico e derrubava o pareamento do SIN
NOMES_SUB = {"SUDESTE": "SE", "SE/CO": "SE", "SECO": "SE", "SUL": "S", "NORDESTE": "NE", "NORTE": "N"}
JANELA_FAIXA = 30                        # faixa de vento: previsão contra a média do deck nos 30 dias antes da rodada
FAIXAS = ["fraco", "normal", "forte"]    # tercis da razão previsão / média recente, por subsistema
MIN_SEMANAS_IC = 8
N_BOOT = 500
QUANTIS = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]

PREV = dict(tabela="fac_tempook_geracao_eolica_hourly", rodada="rodada_dia", tempo="valido_para",
            sub="mnemonico_subsistema", valor="geracao")
OFICIAL = dict(tabela="fac_ons_renovaveis", rodada="rodada_dia", tempo="valido_para", sub="submercado",
               valor="previsao", fonte="UEE")

CORES = {"SE": "#2a78d6", "S": "#eb6834", "NE": "#1baf7a", "N": "#eda100", "SIN": "#52514e"}
DIVERGENTE = LinearSegmentedColormap.from_list("div", ["#1c5cab", "#86b6ef", "#f0efec", "#f0a09f", "#b8302f"])
SEQUENCIAL = LinearSegmentedColormap.from_list("seq", ["#f7f9fc", "#86b6ef", "#1c5cab"])
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

DETALHADO = False


def log(*a, **k):
    """Print só no modo --detalhado."""
    if DETALHADO:
        print(*a, **k, flush=True)


# ------------------------------------------------------------------ leitura
def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def _consulta_em_lotes(engine, sql, col_filtro, ini, fim, meses=3, **params):
    """SELECT em janelas de N meses sobre col_filtro (evita timeout). O sql já traz um WHERE."""
    from sqlalchemy import text
    partes = []
    cortes = list(pd.date_range(ini, fim, freq=f"{meses}MS")) + [fim + pd.Timedelta(days=1)]
    if cortes[0] > ini:
        cortes.insert(0, ini)
    with engine.connect() as con:
        con.execute(text("SET statement_timeout = '600s'"))
        for a, b in zip(cortes[:-1], cortes[1:]):
            partes.append(pd.read_sql(text(f"{sql} AND {col_filtro} >= :a AND {col_filtro} < :b"), con,
                                      params={"a": a.date(), "b": b.date(), **params}))
            log(f"  {a.date()} a {(b - pd.Timedelta(days=1)).date()}: {len(partes[-1]):,} linhas")
    cheias = [p for p in partes if len(p)]
    return pd.concat(cheias, ignore_index=True) if cheias else partes[0]


def _ler_csv(csv):
    return pd.read_csv(csv, sep=";", decimal=",", encoding="utf-8-sig")


def _subsistema(s):
    return s.astype(str).str.strip().str.upper().replace(NOMES_SUB)


def _horario(d, nome):
    """30 min -> hora cheia de início (média das meias-horas). Descarta (e avisa) valores negativos e repetições."""
    d["valido_para"] = pd.to_datetime(d.valido_para)
    d["mw"] = pd.to_numeric(d.mw, errors="coerce")
    d["subsistema"] = _subsistema(d.subsistema)
    d = d.dropna(subset=["mw"])
    neg = d.mw < 0
    if neg.any():
        print(f"AVISO {nome}: {int(neg.sum()):,} valores negativos descartados", flush=True)
        d = d[~neg]
    chaves = [c for c in ["rodada", "valido_para", "subsistema"] if c in d]
    dup = d.duplicated(chaves, keep=False)
    if dup.any():
        print(f"AVISO {nome}: {int(dup.sum()):,} linhas repetidas para o mesmo horário -> usada a média", flush=True)
    d["valido_para"] = d.valido_para.dt.floor("h")
    return d.groupby(chaves, as_index=False).mw.mean()


def _com_sin(d, chaves):
    """Acrescenta SIN = soma de SUBS_SIN (só onde todos existem)."""
    d = d[d.subsistema.isin(SUBS[:4])]
    x = d[d.subsistema.isin(SUBS_SIN)]
    s = x.groupby(chaves).mw.agg(["sum", "size"])
    s = s[s["size"] == len(SUBS_SIN)].reset_index().rename(columns={"sum": "mw"}).drop(columns="size")
    s["subsistema"] = "SIN"
    return pd.concat([d, s], ignore_index=True)


def ler_previsao(engine, ini, fim, csv=None):
    c = PREV
    t0 = time.time()
    if csv:
        d = _ler_csv(csv)
    else:
        d = _consulta_em_lotes(engine, f"SELECT {c['rodada']}, {c['tempo']}, {c['sub']}, {c['valor']} "
                                       f"FROM {c['tabela']} WHERE TRUE", c["rodada"], ini - pd.Timedelta(days=10), fim)
    d = d.rename(columns={c["rodada"]: "rodada", c["tempo"]: "valido_para", c["sub"]: "subsistema", c["valor"]: "mw"})
    ts = pd.to_datetime(d.rodada)
    d["rodada"] = ts.dt.normalize()
    ultima = ts.groupby(d.rodada).transform("max")
    if (ts != ultima).any():
        print(f"AVISO previsão: {d.loc[ts != ultima, 'rodada'].nunique()} dia(s) com mais de uma rodada -> usada a última",
              flush=True)
        d = d[ts == ultima]
    d["mw"] = pd.to_numeric(d.mw, errors="coerce")
    med = d.mw.abs().median()
    if med < 100:                          # Tempo OK vem em GW
        d["mw"] *= 1000
    log(f"previsão: mediana do valor bruto {med:.3f} -> {'GW (x1000)' if med < 100 else 'MW'}")
    ant = (pd.to_datetime(d.valido_para).dt.normalize() - d.rodada).dt.days
    if (ant < 0).any():
        print(f"AVISO previsão: {int((ant < 0).sum()):,} linhas com antecedência negativa (dias passados) descartadas",
              flush=True)
        d = d[ant >= 0]
    d = _horario(d[["rodada", "valido_para", "subsistema", "mw"]].copy(), "previsão")
    d = _com_sin(d, ["rodada", "valido_para"])
    print(f"previsão Tempo OK: {d.rodada.nunique():,} rodadas ({d.rodada.min().date()} a {d.rodada.max().date()}) "
          f"({time.time() - t0:.0f}s)", flush=True)
    return d


def ler_oficial(engine, ini, fim, csv=None):
    """Deck DESSEM, eólica (UEE), delta 0: para cada dia D, o deck com rodada_dia = D."""
    c = OFICIAL
    t0 = time.time()
    if csv:
        d = _ler_csv(csv)
    else:
        d = _consulta_em_lotes(engine, f"SELECT {c['rodada']}, {c['tempo']}, {c['sub']}, {c['valor']}, rev "
                                       f"FROM {c['tabela']} WHERE tipo_fonte_energia = :fonte "
                                       f"AND valido_para_dia = rodada_dia", c["rodada"], ini, fim, fonte=c["fonte"])
    d = d.rename(columns={c["rodada"]: "rodada", c["tempo"]: "valido_para", c["sub"]: "subsistema", c["valor"]: "mw"})
    d["rodada"] = pd.to_datetime(d.rodada).dt.normalize()
    d["valido_para"] = pd.to_datetime(d.valido_para)
    d = d[d.valido_para.dt.normalize() == d.rodada]           # delta 0 (também no CSV de teste)
    if "rev" in d:
        r = pd.to_numeric(d.rev, errors="coerce")
        ult = r.groupby(d.rodada).transform("max")
        if (r != ult).any():
            print(f"AVISO deck: {d.loc[r != ult, 'rodada'].nunique()} dia(s) com mais de uma revisão -> usada a última",
                  flush=True)
            d = d[r == ult]
    d = _horario(d[["valido_para", "subsistema", "mw"]].copy(), "deck")
    d = _com_sin(d, ["valido_para"])
    print(f"deck UEE (delta 0): {d.valido_para.dt.normalize().nunique():,} dias ({d.valido_para.min().date()} a "
          f"{d.valido_para.max().date()}) ({time.time() - t0:.0f}s)", flush=True)
    return d


# ------------------------------------------------------------------ pares e erros
def montar_pares(prev, ofi):
    p = prev.merge(ofi.rename(columns={"mw": "ofi"}), on=["valido_para", "subsistema"]).rename(columns={"mw": "prev"})
    p["dia"] = p.valido_para.dt.normalize()
    p["hora"] = p.valido_para.dt.hour.astype("int64")
    p["h"] = (p.dia - p.rodada).dt.days.astype("int64")
    p = p[p.h.isin(HORIZONTES)]
    return p[["rodada", "dia", "hora", "subsistema", "h", "prev", "ofi"]].sort_values(["rodada", "subsistema", "dia", "hora"])


def excluir_dias(d, col="dia"):
    if not DIAS_EXCLUIDOS_CSV.exists():
        return d
    ex = pd.read_csv(DIAS_EXCLUIDOS_CSV, sep=";", encoding="utf-8-sig")
    dias = set(pd.to_datetime(ex.dia, dayfirst=True).dt.normalize())
    fora = d[col].isin(dias)
    print(f"{DIAS_EXCLUIDOS_CSV.name}: {len(dias)} dia(s), {int(fora.sum()):,} linhas removidas", flush=True)
    return d[~fora]


def erro_diario(pares, ofi):
    g = pares.groupby(["rodada", "dia", "subsistema", "h"])
    d = g.agg(n=("prev", "size"), prev_media=("prev", "mean"), ofi_media=("ofi", "mean")).reset_index()
    pt = pares[pares.hora.isin(HORAS_PONTA)].groupby(["rodada", "dia", "subsistema", "h"])
    d = d.merge(pt.agg(prev_ponta=("prev", "mean"), ofi_ponta=("ofi", "mean")).reset_index(),
                on=["rodada", "dia", "subsistema", "h"], how="left")
    incompletos = int((d.n < 24).sum())
    if incompletos:
        log(f"{incompletos:,} dias-subsistema com menos de 24 h descartados")
    d = d[d.n == 24].drop(columns="n")
    return _calendario(_faixa(d, ofi))


def _faixa(d, ofi):
    """Faixa de vento prevista: previsão média do dia / média do deck nos JANELA_FAIXA dias antes da rodada.
    Tercis por subsistema -> fraco, normal, forte (tudo conhecido na hora da rodada)."""
    od = ofi.assign(dia=ofi.valido_para.dt.normalize()).groupby(["subsistema", "dia"]).mw.mean()
    ref = (od.groupby(level=0).apply(lambda s: s.droplevel(0).asfreq("D").rolling(JANELA_FAIXA, min_periods=JANELA_FAIXA // 2)
                                     .mean().shift(1)).rename("ref_recente").reset_index())
    d = d.merge(ref.rename(columns={"dia": "rodada"}), on=["subsistema", "rodada"], how="left")
    d["razao_recente"] = d.prev_media / d.ref_recente
    q = d.groupby("subsistema").razao_recente.transform(lambda s: s.rank(pct=True))
    d["faixa"] = pd.cut(q, [0, 1 / 3, 2 / 3, 1], labels=FAIXAS, include_lowest=True).astype(str)
    d.loc[d.razao_recente.isna(), "faixa"] = "sem ref"
    return d


def _calendario(d):
    d["mes"] = d.dia.dt.month.astype("int64")
    d["ano"] = d.dia.dt.year.astype("int64")
    d["semana"] = ((d.dia - pd.Timestamp("1970-01-05")).dt.days // 7).astype("int64")     # bloco do bootstrap
    return d


def longo_diario(d):
    """Uma linha por rodada x dia x subsistema x métrica: erro em MW (deck - previsão) e r em %."""
    base = ["rodada", "dia", "subsistema", "h", "mes", "ano", "semana", "faixa"]
    partes = []
    for m in ["media", "ponta"]:
        x = d[base].copy()
        x["metrica"] = m
        x["prev"], x["ofi"] = d[f"prev_{m}"].values, d[f"ofi_{m}"].values
        partes.append(x)
    x = pd.concat(partes, ignore_index=True).dropna(subset=["prev", "ofi"])
    baixo = x.prev <= 0
    if baixo.any():
        print(f"AVISO: {int(baixo.sum()):,} dias com previsão <= 0 fora do cálculo em %", flush=True)
        x = x[~baixo]
    x["erro_mw"] = x.ofi - x.prev
    x["r"] = 100 * (x.ofi / x.prev - 1)
    return x


def erro_perfil(pares):
    p = pares.copy()
    p["erro_mw"] = p.ofi - p.prev
    p["r"] = np.where(p.prev > 0, 100 * (p.ofi / p.prev.where(p.prev > 0) - 1), np.nan)
    return p


# ------------------------------------------------------------------ estatísticas
def _ic_bootstrap(x, rng, col="r"):
    """IC 95 % do viés reamostrando semanas inteiras (dias seguidos têm erro correlacionado)."""
    s = x.groupby("semana")[col].agg(["sum", "size"])
    if len(s) < 4:
        return np.nan, np.nan
    idx = rng.integers(0, len(s), (N_BOOT, len(s)))
    medias = s["sum"].values[idx].sum(1) / s["size"].values[idx].sum(1)
    return np.percentile(medias, 2.5), np.percentile(medias, 97.5)


def resumir(x, chaves, boot=True):
    g = x.groupby(chaves)
    r = g.agg(n=("r", "size"), n_semanas=("semana", "nunique"),
              vies_r=("r", "mean"), mediana_r=("r", "median"), desvio_r=("r", "std"),
              erro_medio_r=("r", lambda e: e.abs().mean()),
              vies_mw=("erro_mw", "mean"), erro_medio_mw=("erro_mw", lambda e: e.abs().mean()),
              prev_media_mw=("prev", "mean"))
    q = g.r.quantile(QUANTIS).unstack()
    q.columns = [f"p{int(round(100 * c)):02d}_r" for c in q.columns]
    r = r.join(q)
    if boot:
        rng = np.random.default_rng(0)
        ic = g.apply(lambda s: pd.Series(_ic_bootstrap(s, rng), index=["vies_ic95_inf_r", "vies_ic95_sup_r"]),
                     include_groups=False)
        r = r.join(ic)
        fora = (r.vies_ic95_inf_r > 0) | (r.vies_ic95_sup_r < 0)
        r["vies_real"] = np.where(r.vies_ic95_inf_r.isna(), "sem IC", np.where(fora, "sim", "não"))
        r["ic_fragil"] = r.n_semanas < MIN_SEMANAS_IC
    return r.reset_index()


def resumir_perfil(p, chaves):
    g = p.groupby(chaves)
    return g.agg(n=("erro_mw", "size"), vies_mw=("erro_mw", "mean"), erro_medio_mw=("erro_mw", lambda e: e.abs().mean()),
                 mediana_r=("r", "median"), p10_mw=("erro_mw", lambda e: e.quantile(.1)),
                 p90_mw=("erro_mw", lambda e: e.quantile(.9)), prev_media_mw=("prev", "mean")).reset_index()


# ------------------------------------------------------------------ checagens
def checar_alinhamento(prev, ofi):
    """SIN, D+1: erro médio com o deck deslocado em -1/0/+1 h e -1/0/+1 dia; o mínimo tem de estar em 0."""
    p = prev[prev.subsistema == "SIN"]
    p = p[(p.valido_para.dt.normalize() - p.rodada).dt.days == 1].set_index("valido_para").mw
    o = ofi[ofi.subsistema == "SIN"].set_index("valido_para").mw
    mae_h = {k: (p - o.shift(k, freq="h")).abs().mean() for k in (-1, 0, 1)}
    mae_d = {k: (p - o.shift(k, freq="D")).abs().mean() for k in (-1, 0, 1)}
    ok = min(mae_h, key=mae_h.get) == 0 and min(mae_d, key=mae_d.get) == 0
    if ok and not DETALHADO:
        print(f"datas: OK (menor erro sem deslocamento de hora nem de dia; D+1 SIN {mae_h[0]:,.0f} MW)", flush=True)
        return
    print("alinhamento SIN D+1, deck deslocado em horas: " + ", ".join(f"{k:+d}h {v:,.0f} MW" for k, v in mae_h.items()))
    print("alinhamento SIN D+1, deck deslocado em dias:  " + ", ".join(f"{k:+d}d {v:,.0f} MW" for k, v in mae_d.items()))
    if not ok:
        print("AVISO: o menor erro não está no deslocamento 0 -> datas ou rótulos desencontrados", flush=True)


def cobertura(prev, ofi, diario, saida):
    c = pd.DataFrame({
        "rodadas_prev": prev.groupby(prev.rodada.dt.to_period("M")).rodada.nunique(),
        "dias_deck": ofi.groupby(ofi.valido_para.dt.to_period("M")).valido_para.apply(lambda v: v.dt.normalize().nunique()),
        "dias_pareados_d1": diario[(diario.h == 1) & (diario.subsistema == "SIN")].groupby(
            diario.dia.dt.to_period("M")).dia.nunique()}).fillna(0).astype(int)
    c.index.name = "mes"
    _salvar(c.reset_index().astype({"mes": str}), "cobertura.csv", saida)
    sub = diario[diario.h == 1].groupby("subsistema").dia.agg(["min", "max", "nunique"])
    print("pares D+1 por subsistema: " + "; ".join(f"{s} {r['min']:%m/%Y} a {r['max']:%m/%Y} ({r['nunique']:,} dias)"
                                                    for s, r in sub.reindex([x for x in SUBS if x in sub.index]).iterrows())
          + f"  (SIN = {' + '.join(SUBS_SIN)})", flush=True)
    t = c.dias_pareados_d1
    t = t[t.index >= t[t > 0].index.min()]
    falhas = t.iloc[1:-1][t.iloc[1:-1] < 20]
    print(f"cobertura: {int(t.sum()):,} dias pareados (D+1, SIN), {t.index.min()} a {t.index.max()}"
          + (f"; meses com < 20 dias: {[str(m) for m in falhas.index]}" if len(falhas) else "") + "  [cobertura.csv]",
          flush=True)
    if DETALHADO:
        t2 = t.copy()
        t2.index = pd.MultiIndex.from_arrays([t2.index.year, t2.index.month], names=["ano", "mes"])
        print(t2.unstack().reindex(columns=range(1, 13)).fillna(0).astype(int)
              .rename(columns=dict(enumerate(MESES, 1))).to_string())


def _salvar(df, nome, saida):
    df.to_csv(saida / nome, sep=";", decimal=",", index=False, encoding="utf-8-sig", float_format="%.3f")


def piores_dias(longo, saida, sub="SIN", n=5):
    por_sub = longo.pivot_table(index=["metrica", "rodada", "dia"], columns="subsistema", values="r")
    por_sub.columns = [f"r_{c}" for c in por_sub.columns]
    x = longo.assign(abs_r=longo.r.abs())
    top = (x.sort_values("abs_r", ascending=False).groupby(["metrica", "subsistema"]).head(30)
           .merge(por_sub.reset_index(), on=["metrica", "rodada", "dia"], how="left"))
    top = top.assign(prev_gw=top.prev / 1000, ofi_gw=top.ofi / 1000, erro_gw=top.erro_mw / 1000)
    cols = ["metrica", "subsistema", "dia", "rodada", "h", "faixa", "prev_gw", "ofi_gw", "erro_gw", "r"] + list(por_sub.columns)
    top = top.sort_values(["metrica", "subsistema", "abs_r"], ascending=[True, True, False])[cols]
    _salvar(top, "piores_dias.csv", saida)
    t = top[(top.metrica == "media") & (top.subsistema == sub)].head(n if not DETALHADO else 15)
    print(f"\nPIORES DIAS - {sub}, média diária (r = deck/previsão - 1; r de cada subsistema à direita)")
    print(f"{'dia':>10}{'rodada':>12}{'h':>5}{'faixa':>8}{'prev GW':>9}{'deck GW':>9}{'r %':>8}  "
          + "".join(f"{c:>7}" for c in SUBS[:4]))
    for _, z in t.iterrows():
        print(f"{z.dia:%d/%m/%Y}  {z.rodada:%d/%m/%Y}{'D+' + str(z.h):>5}{z.faixa:>8}{z.prev_gw:>9.1f}{z.ofi_gw:>9.1f}"
              f"{z.r:>+8.1f}  " + "".join(f"{z.get(f'r_{c}', np.nan):>+7.1f}" for c in SUBS[:4]))
    print(f"[ponta e lista completa em piores_dias.csv; dia com dado ruim -> {DIAS_EXCLUIDOS_CSV.name}]")


# ------------------------------------------------------------------ terminal
def imprimir_quadro(res, sub):
    t = res[res.subsistema == sub].set_index(["metrica", "h"])
    print(f"\n{sub}: QUANTO A TEMPO OK ERRA CONTRA O DECK  (r = deck/previsão - 1; positivo = deck ACIMA da previsão)")
    print("  viés = r médio; real = zero fora do IC 95 % | erro médio = |r| médio | 80 % dos dias entre P10 e P90")
    pon = f"PONTA ({HORAS_PONTA[0]}-{HORAS_PONTA[-1] + 1} h)"
    cab = f"{'viés %':>8}{'real':>6}{'erro %':>8}{'80 % dos dias':>17}{'erro GW':>9}"
    print(f"{'':>5}{'MÉDIA DIÁRIA':^48}   {pon:^48}")
    print(f"{'':>5}{cab}   {cab}")
    for h in HORIZONTES:
        linha = f"{'D+' + str(h):>5}"
        for m in ["media", "ponta"]:
            if (m, h) not in t.index:
                linha += f"{'-':>48}   "
                continue
            z = t.loc[(m, h)]
            real = z.vies_real + ("*" if z.ic_fragil else "")
            linha += (f"{z.vies_r:>+8.1f}{real:>6}{z.erro_medio_r:>8.1f}{f'{z.p10_r:+.1f} a {z.p90_r:+.1f}':>17}"
                      f"{z.erro_medio_mw / 1000:>9.2f}   ")
        print(linha)
    if t.ic_fragil.any():
        print(f"* IC frágil (menos de {MIN_SEMANAS_IC} semanas)")


def imprimir_estabilidade(res_ano, res_faixa, sub, hs=(1, 3, 5, 7, 9)):
    print(f"\n{sub}: O ERRO EM % É ESTÁVEL?  (média diária; erro médio = |r| médio em %, e em GW)")
    a = res_ano[(res_ano.subsistema == sub) & (res_ano.metrica == "media") & res_ano.h.isin(hs)]
    if len(a):
        pr = a.pivot(index="ano", columns="h", values="erro_medio_r").rename(columns=lambda h: f"D+{h} %")
        gw = (a.pivot(index="ano", columns="h", values="erro_medio_mw") / 1000).rename(columns=lambda h: f"D+{h} GW")
        prev = (a[a.h == hs[0]].set_index("ano").prev_media_mw / 1000).rename("prev. média GW")
        print("por ano (se o % fica parecido enquanto o GW cresce com o parque, o % é a unidade certa):")
        print(pd.concat([prev, pr, gw], axis=1).round(2).to_string())
    f = res_faixa[(res_faixa.subsistema == sub) & (res_faixa.metrica == "media") & res_faixa.h.isin(hs)
                  & res_faixa.faixa.isin(FAIXAS)]
    if len(f):
        f = f.assign(faixa=pd.Categorical(f.faixa, FAIXAS, ordered=True))
        print("por faixa de vento prevista (fraco = previsão abaixo da média dos últimos 30 dias):")
        for h in hs:
            x = f[f.h == h].set_index("faixa").sort_index()
            if x.empty:
                continue
            s = "  ".join(f"{fx}: {x.loc[fx, 'erro_medio_r']:.1f} % [{x.loc[fx, 'p10_r']:+.1f} a {x.loc[fx, 'p90_r']:+.1f}]"
                          f" ({x.loc[fx, 'prev_media_mw'] / 1000:.1f} GW)" for fx in x.index)
            print(f"  D+{h}  {s}")
        print("  se o erro % do 'fraco' for muito maior que o do 'forte', o % explode com pouco vento"
              " e a faixa entra nos cenários")


# ------------------------------------------------------------------ gráficos
def _estilo(ax):
    ax.grid(True, color="#e4e3df", lw=0.6)
    ax.set_axisbelow(True)
    for s in ["top", "right"]:
        ax.spines[s].set_visible(False)
    for s in ["left", "bottom"]:
        ax.spines[s].set_color("#b5b4ae")
    ax.tick_params(colors="#52514e", labelsize=8)


def _mapa(ax, tab, cmap, centro_zero):
    v = tab.values.astype(float)
    lim = np.nanmax(np.abs(v)) if centro_zero else np.nanmax(v)
    im = ax.imshow(v, aspect="auto", cmap=cmap, vmin=-lim if centro_zero else 0, vmax=lim)
    for (a, b), val in np.ndenumerate(v):
        if np.isfinite(val):
            ax.text(b, a, f"{val:.1f}", ha="center", va="center", fontsize=6.5,
                    color="white" if abs(val) > 0.6 * lim else "#0b0b0b")
    ax.set_xticks(range(tab.shape[1]), [f"D+{h}" for h in tab.columns], fontsize=7)
    return im


def grafico_horizonte(res, saida):
    fig, axs = plt.subplots(2, 2, figsize=(13, 7.5), sharex=True)
    for i, m in enumerate(["media", "ponta"]):
        r = res[res.metrica == m]
        ax = axs[i, 0]
        for s in SUBS_TERMINAL:
            rs = r[r.subsistema == s].sort_values("h")
            ax.fill_between(rs.h, rs.p10_r, rs.p90_r, color=CORES[s], alpha=0.15, lw=0)
            ax.plot(rs.h, rs.vies_r, marker="o", ms=4, lw=2, color=CORES[s], label=f"{s}: viés (faixa = P10-P90)")
        ax.axhline(0, color="#52514e", lw=0.8)
        ax.set_title(f"{'Média diária' if m == 'media' else 'Ponta'}  -  r = deck/previsão - 1 (%)", fontsize=10, loc="left")
        ax.legend(fontsize=8, frameon=False, loc="upper left")
        _estilo(ax)
        ax = axs[i, 1]
        for s in ["S", "NE", "N", "SIN"]:              # SE fica de fora: ~30 MW, o % não tem sentido
            rs = r[r.subsistema == s].sort_values("h")
            ax.plot(rs.h, rs.erro_medio_r, marker="o", ms=3.5, lw=2 if s == "SIN" else 1.4,
                    ls="--" if s == "SIN" else "-", color=CORES[s], label=s)
        ax.set_title(f"{'Média diária' if m == 'media' else 'Ponta'}  -  erro médio |r| (%)", fontsize=10, loc="left")
        ax.legend(fontsize=8, frameon=False, ncol=4, loc="upper left")
        _estilo(ax)
    for ax in axs[1]:
        ax.set_xticks(HORIZONTES, [f"D+{h}" for h in HORIZONTES])
        ax.set_xlabel("antecedência", fontsize=9)
    fig.suptitle("Eólica: erro da Tempo OK contra o deck DESSEM (UEE, delta 0), por antecedência",
                 fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "horizonte.png", dpi=130)
    plt.close(fig)


def grafico_mes_horizonte(res_mh, saida):
    r = res_mh[res_mh.metrica == "media"]
    fig, axs = plt.subplots(len(SUBS_TERMINAL), 2, figsize=(11, 4.2 * len(SUBS_TERMINAL)), squeeze=False)
    for i, s in enumerate(SUBS_TERMINAL):
        rs = r[r.subsistema == s]
        for j, (col, tit, cmap, z) in enumerate([("vies_r", "viés r (%)", DIVERGENTE, True),
                                                   ("erro_medio_r", "erro médio |r| (%)", SEQUENCIAL, False)]):
            tab = rs.pivot(index="mes", columns="h", values=col).reindex(range(1, 13))
            ax = axs[i, j]
            im = _mapa(ax, tab, cmap, z)
            ax.set_yticks(range(12), MESES, fontsize=7)
            ax.set_title(f"{s}  -  {tit}", fontsize=9, loc="left")
            fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02).ax.tick_params(labelsize=7)
    fig.suptitle("Média diária por mês e antecedência (vermelho = deck acima da previsão)", fontsize=10, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(saida / "mes_horizonte.png", dpi=130)
    plt.close(fig)


def grafico_estabilidade(res_ano, res_faixa, saida, h=3):
    fig, axs = plt.subplots(1, 2, figsize=(13, 4.2))
    a = res_ano[(res_ano.metrica == "media") & (res_ano.h == h)]
    ax = axs[0]
    ax2 = ax.twinx()
    for s in SUBS_TERMINAL:
        x = a[a.subsistema == s].sort_values("ano")
        ax.plot(x.ano, x.erro_medio_r, marker="o", color=CORES[s], lw=2, label=f"{s}: erro %")
        ax2.plot(x.ano, x.erro_medio_mw / 1000, marker="s", color=CORES[s], lw=1, ls=":", label=f"{s}: erro GW")
    ax.set_xticks(sorted(a.ano.unique()))
    ax.set_ylim(0, a.erro_medio_r.max() * 1.35)
    ax2.set_ylim(0, a.erro_medio_mw.max() / 1000 * 1.35)
    ax.set_ylabel("erro médio %", fontsize=8); ax2.set_ylabel("erro médio GW", fontsize=8)
    ax.set_title(f"Por ano, D+{h}, média diária: erro % (linha cheia) x GW (pontilhada)", fontsize=9, loc="left")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    ax2.legend(fontsize=8, frameon=False, loc="upper right")
    _estilo(ax)
    f = res_faixa[(res_faixa.metrica == "media") & (res_faixa.h == h) & res_faixa.faixa.isin(FAIXAS)]
    ax = axs[1]
    w = 0.38
    for k, s in enumerate(SUBS_TERMINAL):
        x = f[f.subsistema == s].set_index("faixa").reindex(FAIXAS)
        pos = np.arange(len(FAIXAS)) + (k - 0.5) * w
        ax.bar(pos, x.erro_medio_r, width=w, color=CORES[s], alpha=0.8, label=s)
        ax.errorbar(pos, x.mediana_r, yerr=[x.mediana_r - x.p10_r, x.p90_r - x.mediana_r], fmt="none",
                    ecolor="#0b0b0b", elinewidth=1, capsize=3)
        for p, v in zip(pos, x.erro_medio_r):
            if np.isfinite(v):
                ax.annotate(f"{v:.1f}", (p, v), textcoords="offset points", xytext=(0, 3), ha="center", fontsize=7.5)
    ax.axhline(0, color="#52514e", lw=0.8)
    ax.set_xticks(range(len(FAIXAS)), FAIXAS)
    ax.set_title(f"Por faixa de vento prevista, D+{h}: barra = erro médio %, traço = P10-P90 de r", fontsize=9, loc="left")
    ax.legend(fontsize=8, frameon=False)
    _estilo(ax)
    fig.tight_layout()
    fig.savefig(saida / "estabilidade.png", dpi=130)
    plt.close(fig)


# ------------------------------------------------------------------ relatório
def relatorio(longo, saida, perfil=None):
    res_h = resumir(longo, ["metrica", "subsistema", "h"])
    res_mh = resumir(longo, ["metrica", "subsistema", "mes", "h"], boot=False)
    res_ano = resumir(longo, ["metrica", "subsistema", "ano", "h"], boot=False)
    res_faixa = resumir(longo, ["metrica", "subsistema", "faixa", "h"], boot=False)
    for df, nome in [(res_h, "resumo_horizonte.csv"), (res_mh, "resumo_mes_horizonte.csv"),
                     (res_ano, "resumo_ano.csv"), (res_faixa, "resumo_faixa.csv")]:
        _salvar(df, nome, saida)
    if perfil is not None:
        _salvar(resumir_perfil(perfil, ["subsistema", "h", "hora"]), "perfil_hora.csv", saida)

    grafico_horizonte(res_h, saida)
    grafico_mes_horizonte(res_mh, saida)
    grafico_estabilidade(res_ano, res_faixa, saida)

    piores_dias(longo, saida)
    for s in (SUBS if DETALHADO else SUBS_TERMINAL):
        imprimir_quadro(res_h, s)
    for s in SUBS_TERMINAL:
        imprimir_estabilidade(res_ano, res_faixa, s)
    if DETALHADO:
        for s in SUBS_TERMINAL:
            t = res_mh[(res_mh.metrica == "media") & (res_mh.subsistema == s)]
            for col, tit, fmt in [("vies_r", "viés r (%)", 1), ("erro_medio_r", "erro médio |r| (%)", 1),
                                  ("erro_medio_mw", "erro médio (MW)", 0), ("n", "dias na amostra", 0)]:
                print(f"\n{s}: {tit} por mês x antecedência, média diária")
                tab = t.pivot(index="mes", columns="h", values=col).rename(index=dict(enumerate(MESES, 1)))
                tab.columns = [f"D+{h}" for h in tab.columns]
                print(tab.round(fmt).to_string())
    return res_h


def main():
    global DETALHADO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ini", default=INICIO)
    ap.add_argument("--fim", default=None, help="padrão: hoje")
    ap.add_argument("--prev-csv", help="teste: CSV com rodada_dia;valido_para;mnemonico_subsistema;geracao")
    ap.add_argument("--oficial-csv", help="teste: CSV com rodada_dia;valido_para;submercado;previsao;rev")
    ap.add_argument("--saida", default=str(OUTPUT_DIR))
    ap.add_argument("--detalhado", action="store_true", help="terminal completo")
    ap.add_argument("--so-relatorio", action="store_true", help="sem banco: refaz o relatório a partir do erro_diario.csv")
    a = ap.parse_args()
    DETALHADO = a.detalhado

    ini = pd.Timestamp(a.ini)
    fim = pd.Timestamp(a.fim) if a.fim else pd.Timestamp.today().normalize()
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    if a.so_relatorio:
        d = pd.read_csv(saida / "erro_diario.csv", sep=";", decimal=",", encoding="utf-8-sig", parse_dates=["rodada", "dia"])
        relatorio(longo_diario(excluir_dias(d)), saida)
        print(f"\nsaídas em {saida}", flush=True)
        return

    eng = None if (a.prev_csv and a.oficial_csv) else engine_banco()
    prev = ler_previsao(eng, ini, fim, a.prev_csv)
    ofi = ler_oficial(eng, ini - pd.Timedelta(days=JANELA_FAIXA + 5), fim, a.oficial_csv)
    checar_alinhamento(prev, ofi)
    pares = montar_pares(prev, ofi)
    pares = excluir_dias(pares[(pares.dia >= ini) & (pares.dia <= fim)])
    diario = erro_diario(pares, ofi)
    cobertura(prev, ofi[ofi.valido_para >= ini], diario, saida)

    pares.to_csv(saida / "pares_horarios.csv.gz", sep=";", decimal=",", index=False, float_format="%.1f")
    _salvar(diario, "erro_diario.csv", saida)
    relatorio(longo_diario(diario), saida, erro_perfil(pares))
    print(f"\nsaídas em {saida}", flush=True)


if __name__ == "__main__":
    main()
