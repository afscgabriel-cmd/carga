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

Cenários (Fase 3, método padronizado da carga + faixa de vento): centro e largura da época (±60 dias, todos os anos),
formato das caudas por faixa de vento (histórico todo), período D+1..D+9 pelo caminho condicional. Escolha feita pela
calibração de 13/10/2026 (testes 2025 e 2026): ±60 d e faixa tiveram o menor pinball; o nível recente não ajudou
(fica opcional). Baixo = P5 (o deck caiu abaixo do P10 em 12-13 % dos dias, acima do P90 em só 7-9 %). Aplicados à rodada mais recente: tabela e gráfico em GW (SIN e NE).
Calibração (Fase 4): cada ano testado com o modelo estimado só com os anos anteriores; compara empírico, padronizado,
+faixa, +recente e +faixa+recente (cobertura P10-P90, caudas, pinball, cobertura por faixa).

Uso:
    python erro_eolica.py                       # banco, desde INICIO (terminal enxuto: SIN e NE)
    python erro_eolica.py --sem-faixa           # cenários sem a faixa de vento
    python erro_eolica.py --com-nivel-recente   # cenários com o nível recente (últimos 365 dias)
    python erro_eolica.py --testes              # sem banco: sensibilidade da janela da época e do nível recente
    python erro_eolica.py --sem-calibracao      # mais rápido
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
    cenarios_dia.csv         quantis P5..P95 de r na época da rodada (por métrica, subsistema, h e faixa)
    cenarios_periodo.csv     período D+1..D+9: r médio do período e caminho dia a dia
    cenarios_aplicados_*.csv cenários aplicados à rodada mais recente, em MW
    calibracao_*.csv         calibração por antecedência, ano e faixa
    *.png                    horizonte, mes_horizonte, estabilidade, cenarios_gw_<SIN|NE>_<rodada>
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
SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
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


def _ref_recente(ofi):
    """Média diária do deck nos JANELA_FAIXA dias ANTES de cada data (por subsistema): o 'vento normal recente'."""
    od = ofi.assign(dia=ofi.valido_para.dt.normalize()).groupby(["subsistema", "dia"]).mw.mean()
    ref = od.groupby(level=0).apply(lambda s: s.droplevel(0).asfreq("D").rolling(JANELA_FAIXA, min_periods=JANELA_FAIXA // 2)
                                    .mean().shift(1))
    return ref.rename("ref_recente").reset_index().rename(columns={"dia": "rodada"})


def cortes_faixa(d):
    """Limites fixos da faixa (tercis do histórico da razão previsão / média recente), por subsistema."""
    return d.dropna(subset=["razao_recente"]).groupby("subsistema").razao_recente.quantile([1 / 3, 2 / 3]).unstack()


def classificar_faixa(d, cortes):
    c = cortes.reindex(d.subsistema.values)
    f = np.where(d.razao_recente.values <= c[1 / 3].values, "fraco",
                 np.where(d.razao_recente.values <= c[2 / 3].values, "normal", "forte"))
    return pd.Series(np.where(d.razao_recente.isna().values | np.isnan(c[1 / 3].values), "sem ref", f), index=d.index)


def _faixa(d, ofi):
    """Faixa de vento prevista: previsão média do dia / média do deck nos JANELA_FAIXA dias antes da rodada.
    Tercis do histórico por subsistema -> fraco, normal, forte (tudo conhecido na hora da rodada)."""
    d = d.merge(_ref_recente(ofi), on=["subsistema", "rodada"], how="left")
    d["razao_recente"] = d.prev_media / d.ref_recente
    d["faixa"] = classificar_faixa(d, cortes_faixa(d))
    return d


def _calendario(d):
    d["mes"] = d.dia.dt.month.astype("int64")
    d["ano"] = d.dia.dt.year.astype("int64")
    d["semana"] = ((d.dia - pd.Timestamp("1970-01-05")).dt.days // 7).astype("int64")     # bloco do bootstrap
    return d


def longo_diario(d):
    """Uma linha por rodada x dia x subsistema x métrica: erro em MW (deck - previsão) e r em %."""
    base = ["rodada", "dia", "subsistema", "h", "mes", "ano", "semana", "faixa", "razao_recente"]
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
    log("pares D+1 por subsistema: " + "; ".join(f"{s} {r['min']:%m/%Y} a {r['max']:%m/%Y} ({r['nunique']:,} dias)"
                                                    for s, r in sub.reindex([x for x in SUBS if x in sub.index]).iterrows())
        + f"  (SIN = {' + '.join(SUBS_SIN)})")
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
    print(f"[lista completa em piores_dias.csv; dado ruim -> {DIAS_EXCLUIDOS_CSV.name}]")


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

    piores_dias(longo, saida, n=3)
    for s in (SUBS if DETALHADO else ["SIN"]):
        imprimir_quadro(res_h, s)
    if DETALHADO:
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


# ------------------------------------------------------------------ cenários (método padronizado, como na carga)
# r = 100 x (deck / previsão - 1); geração do cenário = previsão x (1 + r/100). Positivo = deck acima da previsão.
# P90/P95 = cenários altos, P10/P5 = baixos.
#   centro e largura da época   mediana e IQR/1,349 de r nas rodadas a ±JANELA_EPOCA dias da data (todos os anos);
#                               D+2..D+9 suavizados em h (centro linear, largura a + b·h); D+1 fica como observado
#   formato das caudas          quantis de z = (r - centro)/largura, todo o histórico, por faixa de vento
#                               (fraco / normal / forte) e por grupo (D+1 | D+2..D+9)
#   nível recente               deslocamento e escala de z nos últimos JANELA_RECENTE dias (todas as épocas)
#   período                     W = r médio de D+1..D+9 (SIN, média diária) e caminho r_h = centro_h + beta_h (W - centro_W)
QS = [0.05, 0.10, 0.50, 0.90, 0.95]
PCOLS = [f"p{int(round(100 * q)):02d}" for q in QS]
ZCOLS = [f"z{c[1:]}" for c in PCOLS]
ROT_Q = {0.95: "estresse alto", 0.90: "alto", 0.50: "central", 0.10: "baixo", 0.05: "estresse baixo"}
JANELA_EPOCA = 60                 # ± dias em torno da data (calibração: ±60 melhor que ±15/30/45)
JANELA_RECENTE = 365
MIN_RECENTE = 60
PASSO_CENTRO = 7
CENTROS = [int(c) for c in np.arange(1, 366, PASSO_CENTRO) + PASSO_CENTRO // 2]    # int puro (Windows: int32 x int64)
CHAVE = ["metrica", "subsistema", "h"]


def _na_janela(datas, ref, janela):
    d = (datas.dt.dayofyear - ref.dayofyear).abs()
    return np.minimum(d, 365 - d) <= janela


def _centro_doy(datas, passo=PASSO_CENTRO):
    return (((datas.dt.dayofyear - 1) // passo) * passo + 1 + passo // 2).astype("int64")


def _data_centro(c):
    return pd.Timestamp(2001, 1, 1) + pd.Timedelta(days=int(c) - 1)


def _mais_proximo(c, disponiveis):
    disp = np.asarray(sorted(set(int(v) for v in disponiveis)))
    d = np.abs(disp - c)
    return int(disp[np.argmin(np.minimum(d, 365 - d))])


def _escala(v):
    q25, q75 = np.nanpercentile(v, [25, 75])
    return (q75 - q25) / 1.349


def _grupo(h):
    return np.where(np.asarray(h) == 1, "D+1", "D+2..")


def base_cenarios(longo):
    x = longo.copy()
    x["h"] = x.h.astype("int64")
    x["centro"] = _centro_doy(x.rodada)
    x["grupo"] = _grupo(x.h)
    return x


def _suavizar(e):
    partes = []
    for _, g in e.groupby(["centro", "metrica", "subsistema"]):
        g = g.sort_values("h").copy()
        g["mu"], g["sigma"] = g.mu_bruto, g.sigma_bruto
        m = (g.h >= 2) & g.sigma_bruto.notna()
        if m.sum() >= 3:
            h = g.h[m].to_numpy(float)
            cm = np.linalg.lstsq(np.c_[np.ones_like(h), h], g.mu_bruto[m].to_numpy(), rcond=None)[0]
            cs = np.linalg.lstsq(np.c_[np.ones_like(h), h], g.sigma_bruto[m].to_numpy(), rcond=None)[0]
            if cs[1] < 0:                              # a largura tem de crescer com h
                cs = [g.sigma_bruto[m].mean(), 0.0]
            g.loc[m, "mu"] = cm[0] + cm[1] * h
            g.loc[m, "sigma"] = np.maximum(cs[0] + cs[1] * h, 1e-3)
        g["sigma"] = g.sigma.clip(lower=1e-3)
        partes.append(g)
    return pd.concat(partes, ignore_index=True)


def tabela_epoca(x, janela=JANELA_EPOCA):
    partes = []
    for c in CENTROS:
        t = x[_na_janela(x.rodada, _data_centro(c), janela)]
        if t.empty:
            continue
        g = t.groupby(CHAVE).r
        q = g.quantile([0.25, 0.50, 0.75]).unstack()
        partes.append(pd.DataFrame({"mu_bruto": q[0.50], "sigma_bruto": (q[0.75] - q[0.25]) / 1.349,
                                    "n": g.size()}).reset_index().assign(centro=int(c)))
    ep = _suavizar(pd.concat(partes, ignore_index=True))
    return ep.astype({"centro": "int64", "h": "int64"})


def _nivel(df, col, chaves, recente_desde):
    linhas = []
    for k, g in df.groupby(chaves):
        rec = g[g.dia > recente_desde][col]
        ok = len(rec) >= MIN_RECENTE
        linhas.append(dict(zip(chaves, k), m_hist=g[col].median(), s_hist=_escala(g[col]),
                           m_rec=rec.median() if ok else np.nan, s_rec=_escala(rec) if ok else np.nan, n_rec=len(rec)))
    t = pd.DataFrame(linhas)
    t["m_rec"] = t.m_rec.fillna(t.m_hist)
    t["s_rec"] = t.s_rec.fillna(t.s_hist)
    return t


def forma_caudas(x, ep, faixa=True):
    """Quantis de z por métrica x subsistema x grupo (D+1 | D+2..), por faixa de vento e 'todas'."""
    y = x.merge(ep[["centro"] + CHAVE + ["mu", "sigma"]], on=["centro"] + CHAVE)
    y["z"] = (y.r - y.mu) / y.sigma
    partes = []
    for fx, yy in [("todas", y)] + ([(f, y[y.faixa == f]) for f in FAIXAS] if faixa else []):
        g = yy.groupby(["metrica", "subsistema", "grupo"]).z
        f = g.quantile(QS).unstack()
        f.columns = ZCOLS
        f["n_forma"] = g.size()
        partes.append(f.reset_index().assign(faixa=fx))
    forma = pd.concat(partes, ignore_index=True)
    forma = forma[forma.n_forma >= 30]
    nivel = _nivel(y, "z", CHAVE, y.dia.max() - pd.Timedelta(days=JANELA_RECENTE))
    return forma, nivel


def semanas(x):
    """W por rodada = r médio de D+1..D+9 (SIN, média diária), só períodos completos."""
    s = x[(x.metrica == "media") & (x.subsistema == "SIN")].pivot_table(index="rodada", columns="h", values="r")
    s = s.reindex(columns=HORIZONTES).dropna()
    w = s.mean(axis=1).rename("W").reset_index()
    w["centro"] = _centro_doy(w.rodada)
    return w


def modelo_semana(x, ep, janela=JANELA_EPOCA):
    w = semanas(x)
    partes = []
    for c in CENTROS:
        t = w[_na_janela(w.rodada, _data_centro(c), janela)]
        if len(t) >= 10:
            q25, q50, q75 = np.percentile(t.W, [25, 50, 75])
            partes.append({"centro": int(c), "muW": q50, "sigmaW": max((q75 - q25) / 1.349, 1e-3), "nW": len(t)})
    ew = pd.DataFrame(partes)
    if ew.empty:
        return ew, None, None, 0, None
    w = w.merge(ew, on="centro")
    w["z"] = (w.W - w.muW) / w.sigmaW
    zW = np.quantile(w.z, QS)
    rec = w[w.rodada > w.rodada.max() - pd.Timedelta(days=JANELA_RECENTE)].z
    ok = len(rec) >= MIN_RECENTE
    nivelW = {"m_hist": w.z.median(), "s_hist": _escala(w.z),
              "m_rec": rec.median() if ok else w.z.median(), "s_rec": _escala(rec) if ok else _escala(w.z)}
    y = (x[x.rodada.isin(w.rodada)].merge(ep[["centro"] + CHAVE + ["mu"]], on=["centro"] + CHAVE)
         .merge(w[["rodada", "W", "muW"]], on="rodada"))
    y["d"], y["dW"] = y.r - y.mu, y.W - y.muW
    beta = y.groupby(CHAVE).apply(lambda g: (g.d * g.dW).sum() / (g.dW ** 2).sum(),
                                  include_groups=False).rename("beta").reset_index()
    return ew, zW, beta, len(w), nivelW


def ajustar_modelo(x, janela=JANELA_EPOCA, recente=False, faixa=True, semana=True):
    ep = tabela_epoca(x, janela)
    forma, nivel = forma_caudas(x, ep, faixa)
    ew, zW, beta, n_sem, nivelW = modelo_semana(x, ep, janela) if semana else (None, None, None, 0, None)
    if not recente:
        nivel = nivel.assign(m_rec=nivel.m_hist, s_rec=nivel.s_hist)
        if nivelW:
            nivelW = dict(nivelW, m_rec=nivelW["m_hist"], s_rec=nivelW["s_hist"])
    elif recente == "centro":                     # só o centro vem do recente; a largura fica a do histórico
        nivel = nivel.assign(s_rec=nivel.s_hist)
        if nivelW:
            nivelW = dict(nivelW, s_rec=nivelW["s_hist"])
    nivel["k"] = nivel.s_rec / nivel.s_hist
    return {"epoca": ep, "forma": forma, "nivel": nivel, "semana": ew, "zW": zW, "beta": beta, "nivelW": nivelW,
            "n_semanas": n_sem, "recente": recente, "faixa": faixa}


def quantis_dia(mod):
    """Quantis de r (%) por centro de época x métrica x subsistema x h x faixa ('todas' = sem faixa)."""
    ep = mod["epoca"].assign(grupo=lambda e: _grupo(e.h))
    t = (ep.merge(mod["forma"], on=["metrica", "subsistema", "grupo"])
         .merge(mod["nivel"][CHAVE + ["m_hist", "m_rec", "k"]], on=CHAVE, how="left"))
    for p, z in zip(PCOLS, ZCOLS):          # nível recente: z' = m_rec + k (z - m_hist)  (neutro se desligado)
        t[p] = t.mu + t.sigma * (t.m_rec + t.k * (t[z] - t.m_hist))
    return t[["centro"] + CHAVE + ["faixa", "n", "mu", "sigma"] + PCOLS].astype({"centro": "int64", "h": "int64"})


def aplicar_quantis(rows, Q):
    """Junta a cada linha (centro, métrica, subsistema, h, faixa) os quantis do modelo; sem a faixa -> 'todas'."""
    rows = rows.copy()
    tem = set(Q.faixa.unique())
    rows["faixa_q"] = np.where(rows.faixa.isin(tem), rows.faixa, "todas")
    return rows.astype({"centro": "int64", "h": "int64"}).merge(
        Q.rename(columns={"faixa": "faixa_q"}).drop(columns=["n", "mu", "sigma"]), on=["centro"] + CHAVE + ["faixa_q"])


def cenarios_semana(mod, c):
    ew = mod["semana"]
    if ew is None or ew.empty:
        return pd.DataFrame()
    z = ew[ew.centro == _mais_proximo(c, ew.centro)].iloc[0]
    nw = mod["nivelW"]
    kW = nw["s_rec"] / nw["s_hist"]
    muW = z.muW + z.sigmaW * (nw["m_rec"] - nw["m_hist"])
    ep = mod["epoca"][mod["epoca"].centro == _mais_proximo(c, mod["epoca"].centro)][CHAVE + ["mu", "sigma"]]
    t = ep.merge(mod["beta"], on=CHAVE).merge(mod["nivel"][CHAVE + ["m_rec", "m_hist"]], on=CHAVE, how="left")
    t["mu_aj"] = t.mu + t.sigma * (t.m_rec - t.m_hist)
    partes = []
    for q, zq in zip(QS, mod["zW"]):
        Wq = z.muW + z.sigmaW * (nw["m_rec"] + kW * (zq - nw["m_hist"]))
        partes.append(t.assign(cenario=ROT_Q[q], quantil=q, W=Wq, r=t.mu_aj + t.beta * (Wq - muW)))
    return pd.concat(partes, ignore_index=True)


def ultima_rodada(prev, ofi):
    """Média e ponta diárias da rodada mais recente da previsão (D+1..D+9) e a razão para a faixa de vento."""
    r = prev.rodada.max()
    p = prev[prev.rodada == r].copy()
    p["dia"] = p.valido_para.dt.normalize()
    p["h"] = (p.dia - r).dt.days.astype("int64")
    p["hora"] = p.valido_para.dt.hour
    p = p[p.h.isin(HORIZONTES)]
    g = p.groupby(["dia", "h", "subsistema"]).mw
    d = pd.DataFrame({"n": g.size(), "media": g.mean()}).reset_index()
    pt = p[p.hora.isin(HORAS_PONTA)].groupby(["dia", "h", "subsistema"]).mw.mean().rename("ponta").reset_index()
    d = d.merge(pt, on=["dia", "h", "subsistema"])
    d = d[d.n == 24].drop(columns="n")
    d.insert(0, "rodada", r)
    d = d.merge(_ref_recente(ofi), on=["subsistema", "rodada"], how="left")
    d["razao_recente"] = d.media / d.ref_recente
    return d.melt(id_vars=["rodada", "dia", "h", "subsistema", "ref_recente", "razao_recente"],
                  value_vars=["media", "ponta"], var_name="metrica", value_name="prev_mw")


def nome_metodo(recente, faixa):
    return "padronizado" + ("+faixa" if faixa else "") + ("+centro recente" if recente == "centro" else
                                                          "+recente" if recente else "")


def cenarios(longo, diario, saida, ult=None, ref=None, janela=JANELA_EPOCA, recente=False, faixa=True):
    arq_ult = saida / "ultima_rodada.csv"
    if ult is not None:
        _salvar(ult, arq_ult.name, saida)
    elif arq_ult.exists():
        ult = pd.read_csv(arq_ult, sep=";", decimal=",", encoding="utf-8-sig", parse_dates=["rodada", "dia"])
    if ref is None:
        ref = ult.rodada.iloc[0] if ult is not None and len(ult) else pd.Timestamp.today().normalize()
    x = base_cenarios(longo)
    mod = ajustar_modelo(x, janela, recente, faixa)
    Q = quantis_dia(mod)
    c = _mais_proximo(int(_centro_doy(pd.Series([ref])).iloc[0]), Q.centro)
    _salvar(Q[Q.centro == c].drop(columns="centro"), "cenarios_dia.csv", saida)
    _salvar(Q, "cenarios_dia_todas_epocas.csv", saida)
    sem = cenarios_semana(mod, c)
    _salvar(sem, "cenarios_periodo.csv", saida)

    print("\n" + "=" * 100)
    print(f"CENÁRIOS (método {nome_metodo(recente, faixa)})  -  época de {ref:%d/%m}: rodadas a ±{janela} dias, todos os anos")
    print("  r = ajuste sobre a previsão Tempo OK para chegar ao deck; geração do cenário = previsão x (1 + r/100); "
          "positivo = deck acima")
    if recente:
        nv = mod["nivel"][mod["nivel"].subsistema == "SIN"].groupby("metrica")[["k", "m_rec", "m_hist"]].mean()
        print(f"  nível recente (últimos {JANELA_RECENTE} dias x histórico): " + "; ".join(
            f"{m} largura x{nv.loc[m].k:.2f}, centro {nv.loc[m].m_rec - nv.loc[m].m_hist:+.2f} larguras"
            for m in ["media", "ponta"] if m in nv.index))
    if ult is None or ult.empty:
        print("  (sem a rodada mais recente: rode com o banco para aplicar os cenários)")
        return mod
    cortes = cortes_faixa(diario)
    u = ult.copy()
    u["faixa"] = classificar_faixa(u, cortes) if faixa else "todas"
    u["centro"] = c
    ap = aplicar_quantis(u, Q)
    for p in PCOLS:
        ap[f"ger_{p}_mw"] = ap.prev_mw * (1 + ap[p] / 100)
    _salvar(ap, "cenarios_aplicados_dia.csv", saida)
    if not sem.empty:
        aps = ult.merge(sem, on=CHAVE, how="left")
        aps["ger_mw"] = aps.prev_mw * (1 + aps.r / 100)
        _salvar(aps, "cenarios_aplicados_periodo.csv", saida)
    for s in SUBS_TERMINAL:
        imprimir_cenarios_gw(ap, s)
        grafico_cenarios_gw(ap, saida, s)
    if not sem.empty:
        imprimir_periodo(sem, ult, mod["n_semanas"])
    return mod


def imprimir_cenarios_gw(ap, sub):
    r0 = ap.rodada.iloc[0]
    t = ap[ap.subsistema == sub]
    if t.empty:
        return
    print(f"\n{sub} - eólica em GW, rodada Tempo OK de {r0:%d/%m/%Y} corrigida pelos cenários  "
          f"(baixo = P5, central = P50, alto = P90)")
    pon = f"PONTA {HORAS_PONTA[0]}-{HORAS_PONTA[-1] + 1} h"
    print(f"{'':>5}{'dia':>11}{'faixa':>8}   {'MÉDIA:':<7}{'prev':>6}{'baixo':>7}{'central':>8}{'alto':>7}{'ajuste':>8}"
          f"   {pon + ':':<15}{'prev':>6}{'baixo':>7}{'central':>8}{'alto':>7}")
    pv = t.pivot_table(index=["h", "dia", "faixa"], columns="metrica",
                       values=["prev_mw", "ger_p05_mw", "ger_p50_mw", "ger_p90_mw", "p50"], aggfunc="first")
    for (h, d, fx), z in pv.iterrows():
        g = lambda c, m: z[(c, m)] / 1000
        print(f"{'D+' + str(h):>5}  {SEMANA[d.dayofweek]} {d:%d/%m}{fx:>8}   {'':<7}{g('prev_mw', 'media'):>6.1f}{g('ger_p05_mw', 'media'):>7.1f}"
              f"{g('ger_p50_mw', 'media'):>8.1f}{g('ger_p90_mw', 'media'):>7.1f}{z[('p50', 'media')]:>+7.1f}%"
              f"   {'':<15}{g('prev_mw', 'ponta'):>6.1f}{g('ger_p05_mw', 'ponta'):>7.1f}{g('ger_p50_mw', 'ponta'):>8.1f}"
              f"{g('ger_p90_mw', 'ponta'):>7.1f}")


def imprimir_periodo(sem, ult, n):
    s = sem[(sem.subsistema == "SIN") & (sem.metrica == "media")]
    u = ult[(ult.subsistema == "SIN") & (ult.metrica == "media")].set_index("h").prev_mw
    print(f"\nSIN - período D+1..D+9 inteiro (energia média do período e caminho dia a dia; {n} períodos no histórico)")
    print(f"{'cenário':<22}{'r período':>10}{'GW período':>11}" + "".join(f"{'D+' + str(h):>7}" for h in HORIZONTES))
    for q in sorted(QS, reverse=True):
        z = s[s.quantil == q].sort_values("h")
        gw = (u.reindex(z.h).values * (1 + z.r.values / 100)).mean() / 1000 if len(u) else np.nan
        print(f"{ROT_Q[q] + f' (P{int(round(100 * q))})':<22}{z.W.iloc[0]:>+9.1f}%{gw:>11.1f}" +
              "".join(f"{v:>+7.1f}" for v in z.r))
    print("Para o período inteiro use estes cenários: aplicar o P90 de cada dia em todos os dias exagera"
          " (os dias não erram todos juntos).")


def grafico_cenarios_gw(ap, saida, sub="SIN"):
    """Previsão da rodada mais recente corrigida (P50) com o valor em cada ponto, faixa P10-P90 e previsão original."""
    r0 = ap.rodada.iloc[0]
    fig, ax = plt.subplots(figsize=(12, 5.4))
    for m, nome, cor, mk, dy in [("ponta", f"Ponta ({HORAS_PONTA[0]}-{HORAS_PONTA[-1] + 1} h)", "#e34948", "o", 9),
                                 ("media", "Média do dia", "#2a78d6", "s", -15)]:
        t = ap[(ap.metrica == m) & (ap.subsistema == sub)].sort_values("dia")
        if t.empty:
            continue
        x = t.dia.to_numpy()
        gw = lambda c: (t[c] / 1000).to_numpy()
        ax.fill_between(x, gw("ger_p05_mw"), gw("ger_p90_mw"), color=cor, alpha=0.13, lw=0)
        ax.plot(x, gw("prev_mw"), color=cor, lw=1, ls="--", alpha=0.7)
        per = f" (período: {gw('ger_p50_mw').mean():.1f} GW)" if m == "media" else ""
        ax.plot(x, gw("ger_p50_mw"), color=cor, lw=2, marker=mk, ms=6, label=f"{nome} corrigida{per}")
        for d, v in zip(x, gw("ger_p50_mw")):
            ax.annotate(f"{v:.1f}", (d, v), textcoords="offset points", xytext=(0, dy), ha="center",
                        fontsize=8.5, fontweight="bold", color=cor)
        for c, rot in [("ger_p90_mw", "alto"), ("ger_p05_mw", "baixo")]:
            ax.annotate(f"{rot} {gw(c)[-1]:.1f}", (x[-1], gw(c)[-1]), textcoords="offset points", xytext=(22, 0),
                        ha="left", va="center", fontsize=7.5, color=cor)
    ax.plot([], [], color="#52514e", lw=1, ls="--", label="previsão original Tempo OK")
    ax.fill_between([], [], [], color="#52514e", alpha=0.13, lw=0, label="faixa baixo (P5) a alto (P90)")
    t = ap[(ap.metrica == "media") & (ap.subsistema == sub)].sort_values("dia")
    ax.set_xticks(t.dia, [f"{d:%d/%m}\n{SEMANA[d.dayofweek]} D+{h}\n{fx}" for d, h, fx in zip(t.dia, t.h, t.faixa)], fontsize=8)
    ax.set_xlim(t.dia.min() - pd.Timedelta(hours=14), t.dia.max() + pd.Timedelta(hours=34))
    ax.set_ylabel("GW", fontsize=9)
    ax.margins(y=0.15)
    _estilo(ax)
    ax.legend(fontsize=8, frameon=False, loc="upper left", ncol=2)
    ax.set_title(f"Eólica do {sub} por dia (GW)  -  previsão Tempo OK de {r0:%d/%m/%Y} corrigida pelos cenários "
                 f"(referência: deck DESSEM)", fontsize=10.5, loc="left")
    fig.tight_layout()
    fig.savefig(saida / f"cenarios_gw_{sub}_{r0:%Y%m%d}.png", dpi=130)
    plt.close(fig)


# ------------------------------------------------------------------ calibração fora da amostra
def _quantis_empiricos(tr, centros, janela):
    partes = []
    for c in centros:
        t = tr[_na_janela(tr.rodada, _data_centro(c), janela)]
        if t.empty:
            continue
        q = t.groupby(CHAVE).r.quantile(QS).unstack()
        q.columns = PCOLS
        partes.append(q.reset_index().assign(centro=int(c), faixa="todas", n=0, mu=np.nan, sigma=np.nan))
    return pd.concat(partes, ignore_index=True).astype({"centro": "int64", "h": "int64"})


def _pinball(y, qv, q):
    d = y - qv
    return np.where(d >= 0, q * d, (q - 1) * d)


METODOS = [("empírico ±15d", None), ("padronizado", (False, False)), ("padronizado+faixa", (False, True)),
           ("padronizado+recente", (True, False)), ("padronizado+faixa+recente", (True, True)),
           ("padronizado+centro recente", ("centro", False)), ("padronizado+faixa+centro recente", ("centro", True))]


def _rodar_calibracao(longo, janela=JANELA_EPOCA, metodos=METODOS, janela_emp=15, com_periodo=True):
    """Cada ano de teste Y é previsto com modelos estimados SÓ com dias anteriores a 01/01/Y."""
    x = base_cenarios(longo)
    partes, sem = [], []
    for Y in sorted(x.rodada.dt.year.unique()):
        corte = pd.Timestamp(Y, 1, 1)
        tr, te = x[x.dia < corte], x[x.rodada.dt.year == Y]
        if te.empty or tr.empty or (tr.dia.max() - tr.dia.min()).days < 300:
            continue
        cortes = cortes_faixa(tr.drop_duplicates(["rodada", "dia", "subsistema"]))   # faixa só com o passado
        tr = tr.assign(faixa=classificar_faixa(tr, cortes))
        te = te.assign(faixa=classificar_faixa(te, cortes))
        for nome, cfg in metodos:
            if cfg is None:
                Q = _quantis_empiricos(tr, sorted(te.centro.unique()), janela_emp)
            else:
                mod = ajustar_modelo(tr, janela, recente=cfg[0], faixa=cfg[1],
                                     semana=com_periodo and cfg in ((False, True), ("centro", True)))
                Q = quantis_dia(mod)
                if mod["semana"] is not None and not mod["semana"].empty:
                    w = semanas(x)
                    w = w[w.rodada.dt.year == Y].copy()
                    ew, nw = mod["semana"], mod["nivelW"]
                    kW = nw["s_rec"] / nw["s_hist"]
                    w["c"] = w.centro.map(lambda c: _mais_proximo(c, ew.centro))
                    w = w.merge(ew, left_on="c", right_on="centro", suffixes=("", "_ep"))
                    for nq, zq in [("q10", mod["zW"][1]), ("q90", mod["zW"][3])]:
                        w[nq] = w.muW + w.sigmaW * (nw["m_rec"] + kW * (zq - nw["m_hist"]))
                    sem.append(w.assign(ano_teste=Y, metodo=nome))
            m = aplicar_quantis(te, Q)
            partes.append(m.assign(metodo=nome, ano_teste=Y, treino=f"{tr.dia.min():%m/%Y}-{tr.dia.max():%m/%Y}"))
    if not partes:
        return None, []
    m = pd.concat(partes, ignore_index=True)
    m["dentro_80"] = m.r.between(m.p10, m.p90)
    m["dentro_90"] = m.r.between(m.p05, m.p95)
    m["acima_alto"] = m.r > m.p90
    m["abaixo_baixo"] = m.r < m.p10
    m["pinball"] = np.mean([_pinball(m.r, m[p], q) for p, q in zip(PCOLS, QS)], axis=0)
    return m, sem


def calibracao(longo, saida, janela=JANELA_EPOCA, janela_emp=15, em_uso="padronizado+faixa"):
    """Esperado: 80 % dentro de P10-P90, 10 % acima do alto, 10 % abaixo do baixo. Pinball: menor = melhor."""
    m, sem = _rodar_calibracao(longo, janela, METODOS, janela_emp)
    if m is None:
        print("\ncalibração: histórico curto demais (precisa de cerca de 1 ano antes do ano de teste)")
        return

    def resumo(chaves):
        g = m.groupby(chaves)
        return pd.DataFrame({"n": g.size(), "dentro_P10_P90_pct": 100 * g.dentro_80.mean(),
                             "dentro_P5_P95_pct": 100 * g.dentro_90.mean(), "acima_do_alto_pct": 100 * g.acima_alto.mean(),
                             "abaixo_do_baixo_pct": 100 * g.abaixo_baixo.mean(), "pinball": g.pinball.mean()}).reset_index()
    cal_h = resumo(["metodo", "metrica", "subsistema", "h"])
    cal_ano = resumo(["metodo", "ano_teste", "treino", "metrica", "subsistema"])
    cal_fx = resumo(["metodo", "metrica", "subsistema", "faixa"])
    for df, nome in [(cal_h, "calibracao_horizonte.csv"), (cal_ano, "calibracao_ano.csv"), (cal_fx, "calibracao_faixa.csv")]:
        _salvar(df, nome, saida)
    sp = pd.concat(sem, ignore_index=True) if sem else pd.DataFrame()
    if len(sp):
        _salvar(sp, "calibracao_periodos.csv", saida)

    nomes = [n for n, _ in METODOS]
    if not DETALHADO:
        calibracao_curta(m, sp, em_uso)
        return
    for sub in SUBS_TERMINAL:
        x_ = m[m.subsistema == sub]
        g = x_.groupby(["metodo", "metrica"])
        r = pd.DataFrame({"dentro": 100 * g.dentro_80.mean(), "acima": 100 * g.acima_alto.mean(),
                          "abaixo": 100 * g.abaixo_baixo.mean(), "pinball": g.pinball.mean()})
        anos = ", ".join(str(int(a)) for a in sorted(x_.ano_teste.unique()))
        print("\n" + "=" * 100)
        print(f"CALIBRAÇÃO FORA DA AMOSTRA  -  {sub}, D+1..D+9, anos de teste {anos} (cada um estimado só com os anteriores)")
        print("  esperado: dentro P10-P90 = 80 % | acima do alto = 10 % | abaixo do baixo = 10 % | pinball: menor = melhor")
        fx = x_[x_.metrica == "media"].groupby(["metodo", "faixa"]).dentro_80.mean().mul(100).unstack()
        print(f"{'método':<34}{'métrica':<8}{'dentro':>8}{'acima':>7}{'abaixo':>8}{'pinball':>9}"
              f"{'dentro por faixa (média): fraco / normal / forte':>52}{'períodos':>10}")
        for n in nomes:
            for mt in ["media", "ponta"]:
                if (n, mt) not in r.index:
                    continue
                z = r.loc[(n, mt)]
                extra = ""
                if mt == "media" and n in fx.index:
                    extra = " / ".join(f"{fx.loc[n].get(f, np.nan):.0f} %" for f in FAIXAS)
                pw = ""
                if mt == "media" and sub == "SIN" and len(sp) and n in set(sp.metodo):
                    w = sp[sp.metodo == n]
                    pw = f"{100 * w.W.between(w.q10, w.q90).mean():.0f} %"
                print(f"{n:<34}{mt:<8}{z.dentro:>7.0f}%{z.acima:>6.0f}%{z.abaixo:>7.0f}%{z.pinball:>9.3f}{extra:>52}{pw:>10}")
        rr = x_.groupby("metodo").pinball.mean()
        print(f"-> menor pinball: {rr.idxmin()}   [por antecedência, ano e faixa em calibracao_*.csv]")
        imprimir_calibracao_ano(x_, sub)
    if DETALHADO:
        for sub in SUBS_TERMINAL:
            t = cal_h[(cal_h.subsistema == sub) & (cal_h.metrica == "media")]
            print(f"\n{sub} - média diária: dentro P10-P90 (%) por antecedência e método")
            print(t.pivot(index="h", columns="metodo", values="dentro_P10_P90_pct").reindex(columns=nomes).round(0).to_string())
            print(f"\n{sub} - média diária: pinball por antecedência e método")
            print(t.pivot(index="h", columns="metodo", values="pinball").reindex(columns=nomes).round(3).to_string())
            t = cal_ano[(cal_ano.subsistema == sub)]
            print(f"\n{sub} - por ano de teste")
            print(t[["metodo", "ano_teste", "treino", "metrica", "n", "dentro_P10_P90_pct", "acima_do_alto_pct",
                     "abaixo_do_baixo_pct", "pinball"]].round(2).to_string(index=False))



def calibracao_curta(m, sp, em_uso):
    """Terminal enxuto: o método em uso contra o padronizado simples e o empírico, SIN e NE, média diária."""
    metodos = [em_uso] + [n for n in ("padronizado", "empírico ±15d") if n != em_uso]
    anos = sorted(int(a) for a in m.ano_teste.unique())
    print("\n" + "=" * 100)
    print(f"CALIBRAÇÃO (fora da amostra, testes {', '.join(map(str, anos))}; média diária, D+1..D+9)  "
          f"meta: 80 % dentro de P10-P90, 10 % acima, 10 % abaixo")
    print(f"{'':<5}{'método':<36}{'dentro':>7}{'acima':>7}{'abaixo':>8}{'pinball':>9}"
          + "".join(f"{'dentro ' + str(a):>13}" for a in anos))
    for sub in SUBS_TERMINAL:
        for i, n in enumerate(metodos):
            g = m[(m.subsistema == sub) & (m.metrica == "media") & (m.metodo == n)]
            if g.empty:
                continue
            rot = n + ("  (em uso)" if n == em_uso else "")
            por_ano = "".join(f"{100 * g[g.ano_teste == a].dentro_80.mean():>12.0f}%" for a in anos)
            print(f"{sub if i == 0 else '':<5}{rot:<36}{100 * g.dentro_80.mean():>6.0f}%{100 * g.acima_alto.mean():>6.0f}%"
                  f"{100 * g.abaixo_baixo.mean():>7.0f}%{g.pinball.mean():>9.3f}{por_ano}")
    if len(sp) and em_uso in set(sp.metodo):
        w = sp[sp.metodo == em_uso]
        print(f"período D+1..D+9 (SIN): {100 * w.W.between(w.q10, w.q90).mean():.0f} % dentro de P10-P90")
    print("[todos os métodos, por antecedência, ano e faixa: --detalhado e calibracao_*.csv]")


GRUPOS_H = [("D+1", [1]), ("D+2..4", [2, 3, 4]), ("D+5..9", [5, 6, 7, 8, 9])]


def imprimir_calibracao_ano(x_, sub, metodos=("padronizado", "padronizado+faixa", "padronizado+centro recente",
                                              "padronizado+faixa+centro recente")):
    """Calibração separada por ano de teste (média diária): as faixas estão largas ou estreitas para o modelo de hoje?
    Largura = P90 - P10 em pontos percentuais de r (média dos dias)."""
    x = x_[(x_.metrica == "media") & x_.metodo.isin(metodos)].copy()
    if x.empty:
        return
    x["largura"] = x.p90 - x.p10
    print(f"\n{sub} - POR ANO DE TESTE (média diária). Faixa larga demais: dentro > 85 % e acima/abaixo < 7 %; "
          f"estreita demais: dentro < 75 %")
    print(f"{'método':<34}{'ano':>5}{'dias':>7}{'dentro':>8}{'acima':>7}{'abaixo':>8}{'largura P10-P90':>17}"
          + "".join(f"{'dentro ' + g:>15}" for g, _ in GRUPOS_H) + f"{'viés real':>11}{'centro P50':>12}")
    for met in metodos:
        for ano, g in x[x.metodo == met].groupby("ano_teste"):
            grupos = "".join(f"{100 * g[g.h.isin(hs)].dentro_80.mean():>14.0f}%" for _, hs in GRUPOS_H)
            print(f"{met:<34}{int(ano):>5}{g.dia.nunique():>7}{100 * g.dentro_80.mean():>7.0f}%{100 * g.acima_alto.mean():>6.0f}%"
                  f"{100 * g.abaixo_baixo.mean():>7.0f}%{g.largura.mean():>15.1f} pp{grupos}"
                  f"{g.r.median():>+10.1f}%{g.p50.median():>+11.1f}%")
    print("  viés real = mediana do r observado no ano; centro P50 = mediana do cenário central usado. Se o centro ficar"
          " acima do viés real, o cenário está alto demais naquele ano.")


def testes_sensibilidade(longo, saida):
    """Calibração fora da amostra de variações do método: janela da época e janela do nível recente."""
    global JANELA_RECENTE
    base_rec = JANELA_RECENTE
    metodos = [("padronizado", (False, False)), ("padronizado+recente", (True, False)), ("padronizado+faixa", (False, True))]
    configs = [("época ±60 d (padrão)", 60, 365), ("época ±15 d", 15, 365), ("época ±30 d", 30, 365),
               ("época ±45 d", 45, 365), ("época ±90 d", 90, 365), ("época ±60 d, recente 180 d", 60, 180),
               ("época ±60 d, recente 730 d", 60, 730)]
    linhas = []
    for rot, jan, rec in configs:
        JANELA_RECENTE = rec
        m, _ = _rodar_calibracao(longo, jan, metodos, com_periodo=False)
        if m is None:
            continue
        for (met, sub, mt), g in m.groupby(["metodo", "subsistema", "metrica"]):
            if sub not in SUBS_TERMINAL:
                continue
            if "recente" not in met and rec != 365:          # a janela do nível recente não muda os métodos sem ele
                continue
            fx = g.groupby("faixa").dentro_80.mean().mul(100)
            linhas.append({"teste": rot, "metodo": met, "subsistema": sub, "metrica": mt, "n": len(g),
                           "dentro": 100 * g.dentro_80.mean(), "acima": 100 * g.acima_alto.mean(),
                           "abaixo": 100 * g.abaixo_baixo.mean(), "pinball": g.pinball.mean(),
                           **{f"dentro_{f}": fx.get(f, np.nan) for f in FAIXAS}})
        print(f"  teste concluído: {rot}", flush=True)
    JANELA_RECENTE = base_rec
    t = pd.DataFrame(linhas)
    _salvar(t, "testes_sensibilidade.csv", saida)
    for sub in SUBS_TERMINAL:
        print("\n" + "=" * 100)
        print(f"TESTES DE SENSIBILIDADE  -  {sub}, D+1..D+9, calibração fora da amostra (esperado: 80 / 10 / 10 %; "
              f"pinball menor = melhor)")
        print(f"{'teste':<36}{'método':<27}{'métrica':<7}{'dentro':>7}{'acima':>7}{'abaixo':>7}{'pinball':>9}"
              f"{'fraco':>7}{'normal':>7}{'forte':>7}")
        for _, z in t[t.subsistema == sub].iterrows():
            print(f"{z.teste:<36}{z.metodo:<27}{z.metrica:<7}{z.dentro:>6.0f}%{z.acima:>6.0f}%{z.abaixo:>6.0f}%"
                  f"{z.pinball:>9.3f}{z.dentro_fraco:>6.0f}%{z.dentro_normal:>6.0f}%{z.dentro_forte:>6.0f}%")
        b = t[t.subsistema == sub].groupby(["teste", "metodo"]).pinball.mean()
        print(f"-> menor pinball (média e ponta): {b.idxmin()[0]} | {b.idxmin()[1]}")
    print("[testes_sensibilidade.csv]")


def main():
    global DETALHADO, JANELA_RECENTE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ini", default=INICIO)
    ap.add_argument("--fim", default=None, help="padrão: hoje")
    ap.add_argument("--prev-csv", help="teste: CSV com rodada_dia;valido_para;mnemonico_subsistema;geracao")
    ap.add_argument("--oficial-csv", help="teste: CSV com rodada_dia;valido_para;submercado;previsao;rev")
    ap.add_argument("--saida", default=str(OUTPUT_DIR))
    ap.add_argument("--detalhado", action="store_true", help="terminal completo")
    ap.add_argument("--so-relatorio", action="store_true", help="sem banco: refaz tudo a partir do erro_diario.csv")
    ap.add_argument("--sem-faixa", dest="faixa", action="store_false", help="cenários sem a faixa de vento")
    ap.add_argument("--com-nivel-recente", dest="recente", action="store_const", const=True, default=False,
                    help="cenários com o nível recente no centro e na largura (não melhorou a calibração)")
    ap.add_argument("--centro-recente", dest="recente", action="store_const", const="centro",
                    help="cenários com só o centro ajustado pelo viés dos últimos 365 dias (largura do histórico)")
    ap.add_argument("--janela", type=int, default=JANELA_EPOCA, help="± dias em torno da data para a época (padrão 60)")
    ap.add_argument("--dias-recentes", type=int, default=JANELA_RECENTE, help="janela do nível recente (padrão 365)")
    ap.add_argument("--data-ref", default=None, help="data de referência da época (padrão: rodada mais recente)")
    ap.add_argument("--sem-calibracao", dest="calibrar", action="store_false", help="pula a calibração (mais rápido)")
    ap.add_argument("--testes", action="store_true",
                    help="só os testes de sensibilidade (janela da época e do nível recente), a partir do erro_diario.csv")
    a = ap.parse_args()
    DETALHADO = a.detalhado
    JANELA_RECENTE = a.dias_recentes

    ini = pd.Timestamp(a.ini)
    fim = pd.Timestamp(a.fim) if a.fim else pd.Timestamp.today().normalize()
    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    ref = pd.Timestamp(a.data_ref) if a.data_ref else None
    ult = None
    if a.testes:
        diario = pd.read_csv(saida / "erro_diario.csv", sep=";", decimal=",", encoding="utf-8-sig",
                             parse_dates=["rodada", "dia"])
        testes_sensibilidade(longo_diario(excluir_dias(diario)), saida)
        return
    if a.so_relatorio:
        diario = pd.read_csv(saida / "erro_diario.csv", sep=";", decimal=",", encoding="utf-8-sig",
                             parse_dates=["rodada", "dia"])
        diario = excluir_dias(diario)
        longo = longo_diario(diario)
        relatorio(longo, saida)
    else:
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
        longo = longo_diario(diario)
        relatorio(longo, saida, erro_perfil(pares))
        ult = ultima_rodada(prev, ofi)
    cenarios(longo, diario, saida, ult, ref, a.janela, a.recente, a.faixa)
    if a.calibrar:
        calibracao(longo, saida, a.janela, em_uso=nome_metodo(a.recente, a.faixa))
    print(f"\nsaídas em {saida}", flush=True)


if __name__ == "__main__":
    main()
