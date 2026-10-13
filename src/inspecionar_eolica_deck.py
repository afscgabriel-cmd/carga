# -*- coding: utf-8 -*-
"""Fase 1 da análise de erro da previsão eólica: checagens antes da metodologia.

Compara a previsão da Tempo OK (fac_tempook_geracao_eolica_hourly, modelo WEOL) com a eólica do deck DESSEM
(fac_ons_renovaveis, tipo_fonte_energia = 'UEE') e com o realizado (fac_ons_geracao_eolica). Só leitura.

Responde:
  1. Estrutura do deck UEE: o que é rodada_dia (data do deck ou de publicação), revisões, antecedências.
  2. Estrutura da TOK: horário e número de rodadas por dia, antecedências (negativas? até D+9 ou D+10?).
  3. Alinhamento: em que dia e hora o deck "casa" com a TOK e com o realizado (deslocamentos).
  4. Origem do dado: % de horas idênticas TOK x deck por antecedência (o ONS parte da TOK?) e tamanho do ajuste.
  5. Corte: o deck mostra os platôs do realizado (ex.: set/2026 no NE) ou segue a previsão de vento?
  6. Cobertura ano x mês de cada tabela e tabelas candidatas a capacidade instalada / fator de capacidade.

Uso:
    python inspecionar_eolica_deck.py                     # últimos 120 dias de rodadas, corte em set/2026
    python inspecionar_eolica_deck.py --dias 400
    python inspecionar_eolica_deck.py --corte-ini 2026-09-01 --corte-fim 2026-09-30
    python inspecionar_eolica_deck.py --sem-cobertura     # pula as consultas de cobertura (tabela inteira)
Saídas em output/inspecao_eolica/. Cole a saída do terminal na conversa.
"""
import argparse
import json
import time
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "inspecao_eolica"

T_TOK = "fac_tempook_geracao_eolica_hourly"
T_DECK = "fac_ons_renovaveis"
T_REAL = "fac_ons_geracao_eolica"
SUBS = ["SE", "S", "NE", "N"]
IGUAL_MW = 1.0          # |TOK - deck| abaixo disso conta como "idêntico" (a TOK vem em GW com 3 casas = 1 MW)
POUCO_MW = 10.0         # abaixo disso conta como "sem ajuste"
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

pd.set_option("display.width", 220); pd.set_option("display.max_columns", 40); pd.set_option("display.max_rows", 200)
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:5.0f}s] {msg}", flush=True)


def secao(titulo):
    print(f"\n{'=' * 100}\n{titulo}\n{'=' * 100}", flush=True)


def protegido(f):
    """Uma seção que falha não derruba as outras: mostra o erro e segue."""
    def g(*a, **k):
        try:
            return f(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"\n!!! ERRO em {f.__name__}: {e}")
            traceback.print_exc()
            return None
    g.__name__ = f.__name__
    return g


# ------------------------------------------------------------------ banco
def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def sql(eng, q, timeout="600s", **params):
    from sqlalchemy import text
    with eng.connect() as con:
        con.execute(text(f"SET statement_timeout = '{timeout}'"))
        return pd.read_sql(text(q), con, params=params)


def colunas(eng, tabela):
    return sql(eng, """SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_name = :t ORDER BY ordinal_position""", t=tabela)


def lotes(ini, fim, dias=30):
    a = ini
    while a <= fim:
        b = min(a + pd.Timedelta(days=dias), fim + pd.Timedelta(days=1))
        yield a, b
        a = b


def ler_tok(eng, ini, fim):
    partes = []
    for a, b in lotes(ini, fim):
        partes.append(sql(eng, f"""SELECT rodada_dia, valido_para, geracao, mnemonico_subsistema FROM {T_TOK}
                                    WHERE rodada_dia >= :a AND rodada_dia < :b""", a=a.date(), b=b.date()))
        log(f"  TOK {a.date()} a {(b - pd.Timedelta(days=1)).date()}: {len(partes[-1]):,} linhas")
    d = pd.concat(partes, ignore_index=True)
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia); d["valido_para"] = pd.to_datetime(d.valido_para)
    d["geracao"] = pd.to_numeric(d.geracao, errors="coerce")
    d["subsistema"] = d.mnemonico_subsistema.astype(str).str.strip()
    return d.drop(columns="mnemonico_subsistema")


def ler_deck(eng, ini, fim, col_rev):
    rev = f", {col_rev} AS rev" if col_rev else ""
    grp = f", {col_rev}" if col_rev else ""
    partes = []
    for a, b in lotes(ini, fim):
        partes.append(sql(eng, f"""SELECT rodada_dia, valido_para, submercado AS subsistema{rev},
                                          SUM(previsao) AS mw, COUNT(*) AS n_linhas
                                   FROM {T_DECK}
                                   WHERE tipo_fonte_energia = 'UEE' AND rodada_dia >= :a AND rodada_dia < :b
                                   GROUP BY rodada_dia, valido_para, submercado{grp}""", a=a.date(), b=b.date()))
        log(f"  deck UEE {a.date()} a {(b - pd.Timedelta(days=1)).date()}: {len(partes[-1]):,} linhas")
    d = pd.concat(partes, ignore_index=True)
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia); d["valido_para"] = pd.to_datetime(d.valido_para)
    d["mw"] = pd.to_numeric(d.mw, errors="coerce")
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    if "rev" not in d:
        d["rev"] = "-"
    return d


def ler_real(eng, ini, fim):
    d = sql(eng, f"SELECT subsistema, dia, hora, carga FROM {T_REAL} WHERE dia >= :ini AND dia <= :fim",
            ini=ini.date(), fim=fim.date())
    d["valido_para"] = pd.to_datetime(d.dia.astype(str)) + pd.to_timedelta(d.hora.astype(str))
    d["mw"] = pd.to_numeric(d.carga, errors="coerce")
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    ruim = d.mw > 60000
    if ruim.any():
        print(f"AVISO realizado: {int(ruim.sum())} valores acima de 60000 MW descartados")
    return d.loc[~ruim, ["valido_para", "subsistema", "mw"]]


# ------------------------------------------------------------------ preparo
def com_sin(d, chaves, valor="mw"):
    """Acrescenta SIN = soma dos 4 subsistemas, só onde os 4 existem."""
    x = d[d.subsistema.isin(SUBS)]
    g = x.groupby(chaves)[valor].agg(["sum", "count"]).reset_index()
    g = g[g["count"] == len(SUBS)].drop(columns="count").rename(columns={"sum": valor})
    g["subsistema"] = "SIN"
    return pd.concat([x, g], ignore_index=True)


def tok_mw(tok):
    med = tok.geracao.abs().median()
    em_gw = med < 100
    tok = tok.copy()
    tok["mw"] = tok.geracao * (1000 if em_gw else 1)
    return tok, em_gw, med


def ultima_por_dia(d, col="rodada_dia"):
    """Se houver mais de uma rodada no mesmo dia, fica a última."""
    d = d.copy()
    d["data_rodada"] = d[col].dt.normalize()
    ult = d.groupby("data_rodada")[col].transform("max")
    return d[d[col] == ult]


def ultima_rev(deck):
    deck = deck.copy()
    r = pd.to_numeric(deck.rev, errors="coerce")
    deck["_ordem"] = r if r.notna().all() else deck.rev.astype(str).rank(method="dense")
    ult = deck.groupby("rodada_dia")["_ordem"].transform("max")
    return deck[deck._ordem == ult].drop(columns="_ordem")


def horaria(d, chaves_extra=()):
    d = d.copy()
    d["hora"] = d.valido_para.dt.floor("h")
    return d.groupby(list(chaves_extra) + ["hora", "subsistema"], as_index=False).mw.mean().rename(columns={"hora": "valido_para"})


def comparar(a, b, chave="valido_para", va="mw_a", vb="mw_b"):
    m = a.merge(b, on=[chave, "subsistema"])
    e = m[va] - m[vb]
    return {"n": len(m), "MAE": e.abs().mean(), "vies": e.mean(),
            "%iguais": 100 * (e.abs() < IGUAL_MW).mean() if len(m) else np.nan,
            "%<10MW": 100 * (e.abs() < POUCO_MW).mean() if len(m) else np.nan}


# ------------------------------------------------------------------ seções
@protegido
def estrutura_tabelas(eng):
    secao("0. COLUNAS DAS TABELAS")
    out = {}
    for t in (T_TOK, T_DECK, T_REAL):
        c = colunas(eng, t)
        out[t] = c
        print(f"\n--- {t} ---")
        print(c.to_string(index=False))
    return out


@protegido
def deck_valores_distintos(eng):
    secao("1a. DECK UEE: valores distintos das colunas, rodadas dos últimos 3 dias (amostra)")
    d = sql(eng, f"""SELECT * FROM {T_DECK} WHERE tipo_fonte_energia = 'UEE' AND rodada_dia >= CURRENT_DATE - 3
                     LIMIT 300000""")
    print(f"{len(d):,} linhas brutas")
    for c in d.columns:
        if c in ("previsao", "valido_para"):
            continue
        v = d[c].astype(str).value_counts()
        print(f"\n'{c}': {len(v)} valores distintos")
        print(v.head(12).to_string())
    print("\nO que olhar: se há coluna de revisão (rev), de usina/barra (aí a soma por submercado é a do deck)"
          " ou de tipo de dado (previsto x verificado).")


@protegido
def estrutura_deck(deck):
    secao("1b. DECK UEE: o que é rodada_dia, revisões e antecedências")
    print("Horário em rodada_dia (se tudo 00:00, é só data):")
    print(deck.rodada_dia.dt.strftime("%H:%M").value_counts().head(8).to_string())
    revs = deck.groupby("rodada_dia").rev.nunique()
    print(f"\nRevisões por rodada: {revs.value_counts().sort_index().to_dict()}  (nº de revisões: nº de rodadas)")
    print(f"Valores de rev: {deck.rev.astype(str).value_counts().head(10).to_dict()}")
    print(f"Linhas somadas por (rodada, horário, submercado, rev): {deck.n_linhas.value_counts().head(8).to_dict()}"
          "  (1 = já vem agregado; >1 = por usina/barra)")
    x = deck.copy()
    x["delta"] = (x.valido_para.dt.normalize() - x.rodada_dia.dt.normalize()).dt.days.astype("int64")
    tab = x.groupby("delta").agg(rodadas=("rodada_dia", "nunique"), linhas=("mw", "size"))
    print("\nAntecedência (dia de valido_para − dia de rodada_dia):")
    print(tab.to_string())
    ult = x[x.rodada_dia == x.rodada_dia.max()]
    print(f"\nRodada mais recente {ult.rodada_dia.max().date()}: valido_para de {ult.valido_para.min()} a "
          f"{ult.valido_para.max()}; minutos presentes {sorted(int(x) for x in ult.valido_para.dt.minute.unique())}")
    print("O que olhar: se o menor delta é 0 e começa em 00:00, rodada_dia = 1º dia do deck (como na carga);"
          " se o menor delta é 1, rodada_dia é a data de publicação e o programado do dia D está no delta 1.")
    if (revs > 1).any():
        d0 = x[x.delta == 0]
        o = pd.to_numeric(d0.rev, errors="coerce")
        d0 = d0.assign(_o=o if o.notna().all() else d0.rev.astype(str).rank(method="dense"))
        pri = d0[d0._o == d0.groupby("rodada_dia")._o.transform("min")]
        ul = d0[d0._o == d0.groupby("rodada_dia")._o.transform("max")]
        m = pri.merge(ul, on=["rodada_dia", "valido_para", "subsistema"], suffixes=("_pri", "_ult"))
        m = m[m.rev_pri.astype(str) != m.rev_ult.astype(str)]
        if len(m):
            e = (m.mw_ult - m.mw_pri)
            print(f"\nPrimeira x última revisão (delta 0, todos os submercados): MAE {e.abs().mean():.0f} MW,"
                  f" % horários iguais {100 * (e.abs() < IGUAL_MW).mean():.0f}%")
    return x


@protegido
def estrutura_tok(tok, em_gw, med):
    secao("2. TEMPO OK: rodadas, horário, antecedências, resolução")
    print(f"Unidade: mediana de 'geracao' = {med:.3f} -> tratada como {'GW (x1000)' if em_gw else 'MW'}")
    print("Horário em rodada_dia (se tudo 00:00, é só data):")
    print(tok.rodada_dia.dt.strftime("%H:%M").value_counts().head(10).to_string())
    por_dia = tok.groupby(tok.rodada_dia.dt.normalize()).rodada_dia.nunique()
    print(f"Rodadas por dia: {por_dia.value_counts().sort_index().to_dict()}  (nº de rodadas no dia: nº de dias)")
    x = tok.copy()
    x["ant"] = (x.valido_para.dt.normalize() - x.rodada_dia.dt.normalize()).dt.days.astype("int64")
    tab = x.groupby("ant").agg(rodadas=("rodada_dia", "nunique"), linhas=("mw", "size"))
    print("\nAntecedência (dia de valido_para − dia da rodada):")
    print(tab.to_string())
    dup = x.duplicated(["rodada_dia", "valido_para", "subsistema"]).sum()
    print(f"\nLinhas repetidas (mesma rodada, horário e subsistema): {dup:,}")
    print(f"Subsistemas: {sorted(x.subsistema.unique())}")
    ult = x[x.rodada_dia == x.rodada_dia.max()]
    print(f"Rodada mais recente {ult.rodada_dia.max()}: valido_para de {ult.valido_para.min()} a {ult.valido_para.max()};"
          f" minutos presentes {sorted(int(x) for x in ult.valido_para.dt.minute.unique())}")
    print("O que olhar: antecedência negativa = dias já passados (consolidado?); o maior valor é o horizonte (D+9, D+10?).")


@protegido
def alinhamento(tok_d, deck_r, real):
    secao("3. ALINHAMENTO: em que dia e hora o deck casa com a TOK e com o realizado (SIN)")
    t1 = tok_d[(tok_d.ant == 1) & (tok_d.subsistema == "SIN")][["valido_para", "subsistema", "mw"]].rename(columns={"mw": "mw_a"})
    print("TOK D+1 (30 min) x deck, variando qual delta do deck é o 'programado' e deslocando o horário:")
    linhas = []
    for k in (0, 1, 2):
        dk = deck_r[(deck_r.delta == k) & (deck_r.subsistema == "SIN")][["valido_para", "subsistema", "mw"]]
        for mins in (-60, -30, 0, 30, 60):
            b = dk.rename(columns={"mw": "mw_b"}).assign(valido_para=dk.valido_para + pd.Timedelta(minutes=mins))
            r = comparar(t1, b)
            linhas.append({"delta do deck": k, "desloc. deck (min)": mins, **r})
    t = pd.DataFrame(linhas)
    print(t.round(1).to_string(index=False))
    print("O que olhar: o menor MAE e o maior % de iguais devem estar em deslocamento 0 e no delta que for o"
          " 'programado do dia'. Se estiver em ±30 min, as tabelas usam rótulos diferentes (início x fim).")

    print("\nRealizado (horário, rótulo de início) x deck delta 0 e x TOK D+1, com deslocamento em horas e dias:")
    rr = real.rename(columns={"mw": "mw_b"})
    rr = rr[rr.subsistema == "SIN"]
    d0h = horaria(deck_r[(deck_r.delta == 0) & (deck_r.subsistema == "SIN")]).rename(columns={"mw": "mw_a"})
    t1h = horaria(tok_d[(tok_d.ant == 1) & (tok_d.subsistema == "SIN")]).rename(columns={"mw": "mw_a"})
    linhas = []
    for nome, a in (("deck delta 0", d0h), ("TOK D+1", t1h)):
        for rot, desl in (("-1 dia", pd.Timedelta(days=-1)), ("-2 h", pd.Timedelta(hours=-2)), ("-1 h", pd.Timedelta(hours=-1)),
                          ("0", pd.Timedelta(0)), ("+1 h", pd.Timedelta(hours=1)), ("+2 h", pd.Timedelta(hours=2)),
                          ("+1 dia", pd.Timedelta(days=1))):
            b = rr.assign(valido_para=rr.valido_para + desl)
            r = comparar(a, b)
            linhas.append({"série": nome, "desloc. realizado": rot, "n": r["n"],
                           "MAE": r["MAE"], "vies (série − real)": r["vies"]})
    print(pd.DataFrame(linhas).round(0).to_string(index=False))
    print("O que olhar: mínimo do MAE em deslocamento 0. Com corte, o viés fica positivo (série acima do realizado).")
    return t


@protegido
def origem(tok_todas, tok_d, deck_r):
    secao("4. ORIGEM DO DADO: o deck parte da TOK? (% de horários idênticos, 30 min)")
    d0 = deck_r[deck_r.delta == 0][["valido_para", "subsistema", "mw"]].rename(columns={"mw": "mw_b"})
    linhas = []
    for (sub, ant), g in tok_d[tok_d.subsistema.isin(["SIN", "NE", "S"])].groupby(["subsistema", "ant"]):
        r = comparar(g[["valido_para", "subsistema", "mw"]].rename(columns={"mw": "mw_a"}), d0)
        if r["n"]:
            linhas.append({"subsistema": sub, "ant": ant, **r})
    t = pd.DataFrame(linhas)
    if t.empty:
        print("Sem pareamento TOK x deck delta 0."); return None
    print("Erro = TOK − deck delta 0, por antecedência da TOK (última rodada de cada dia):")
    print(t.pivot(index="ant", columns="subsistema", values=["MAE", "vies", "%iguais", "%<10MW"]).round(1).to_string())

    # qual rodada do dia o ONS usa, quando há mais de uma
    hrs = tok_todas.rodada_dia.dt.strftime("%H:%M").unique()
    if len(hrs) > 1:
        x = tok_todas[tok_todas.subsistema == "SIN"].copy()
        x["ant"] = (x.valido_para.dt.normalize() - x.rodada_dia.dt.normalize()).dt.days
        linhas = []
        for (h, ant), g in x[x.ant.isin([0, 1])].groupby([x.rodada_dia.dt.strftime("%H:%M"), "ant"]):
            r = comparar(g[["valido_para", "subsistema", "mw"]].rename(columns={"mw": "mw_a"}), d0)
            linhas.append({"hora da rodada": h, "ant": ant, **r})
        print("\nPor horário da rodada TOK (SIN): qual rodada o deck copia?")
        print(pd.DataFrame(linhas).round(1).to_string(index=False))

    # ao longo do tempo
    s = tok_d[(tok_d.ant == 1) & (tok_d.subsistema == "SIN")][["valido_para", "subsistema", "mw"]].rename(columns={"mw": "mw_a"})
    m = s.merge(d0, on=["valido_para", "subsistema"])
    m["mes"] = m.valido_para.dt.strftime("%Y-%m")
    m["e"] = m.mw_a - m.mw_b
    pm = m.groupby("mes").agg(n=("e", "size"), MAE=("e", lambda e: e.abs().mean()),
                              iguais=("e", lambda e: 100 * (e.abs() < IGUAL_MW).mean()))
    print("\nTOK D+1 x deck delta 0 por mês (SIN): MAE (MW) e % de horários idênticos")
    print(pm.round(1).to_string())
    print("O que olhar: % de iguais alto em D+1 = o ONS parte da TOK e ajusta só às vezes (igual à carga)."
          " Perto de 0 em tudo = previsões independentes (ou rótulos desalinhados: ver seção 3).")
    return t


@protegido
def ajuste(tok_d, deck_r):
    secao("4b. TAMANHO DO AJUSTE DO ONS (deck delta 0 − TOK D+1), média do dia, MW")
    a = tok_d[tok_d.ant == 1][["valido_para", "subsistema", "mw"]]
    b = deck_r[deck_r.delta == 0][["valido_para", "subsistema", "mw"]]
    m = a.merge(b, on=["valido_para", "subsistema"], suffixes=("_tok", "_deck"))
    m["dia"] = m.valido_para.dt.normalize()
    g = m.groupby(["subsistema", "dia"]).agg(tok=("mw_tok", "mean"), deck=("mw_deck", "mean"))
    dif_h = (m.mw_deck - m.mw_tok).abs().groupby([m.subsistema, m.dia]).max()
    g["dif"] = g.deck - g.tok
    g["maior dif. horária"] = dif_h
    g["ajustou"] = g["maior dif. horária"] >= POUCO_MW
    linhas = []
    for sub, x in g.groupby(level=0):
        if sub not in ("SIN", "NE", "S"):
            continue
        aj = x[x.ajustou]
        linhas.append({"subsistema": sub, "dias": len(x), "% dias com ajuste": 100 * x.ajustou.mean(),
                       "P10 dif": aj.dif.quantile(.1), "mediana dif": aj.dif.median(), "P90 dif": aj.dif.quantile(.9),
                       "mediana |dif| %": 100 * (aj.dif.abs() / aj.tok).median()})
    print(pd.DataFrame(linhas).round(1).to_string(index=False))
    print("Positivo = deck acima da TOK. Quantis só dos dias em que houve ajuste (alguma hora com |dif| >= 10 MW).")


@protegido
def corte(tok_d, deck_r, real, ini, fim, sub="NE"):
    secao(f"5. CORTE: o deck mostra os platôs do realizado? ({sub}, {ini.date()} a {fim.date()}, médias horárias)")
    sel = lambda d: d[(d.subsistema == sub) & (d.valido_para >= ini) & (d.valido_para < fim + pd.Timedelta(days=1))]
    t1 = horaria(sel(tok_d[tok_d.ant == 1])).rename(columns={"mw": "TOK_D1"})
    d0 = horaria(sel(deck_r[deck_r.delta == 0])).rename(columns={"mw": "deck"})
    rr = sel(real).rename(columns={"mw": "real"})
    m = t1.merge(d0, on=["valido_para", "subsistema"], how="outer").merge(rr, on=["valido_para", "subsistema"], how="outer")
    if m.empty:
        print("Sem dados no período."); return
    m["dia"] = m.valido_para.dt.normalize()
    dia = m.groupby("dia").agg(TOK_med=("TOK_D1", "mean"), deck_med=("deck", "mean"), real_med=("real", "mean"),
                               TOK_max=("TOK_D1", "max"), deck_max=("deck", "max"), real_max=("real", "max"))
    dia.insert(0, "sem", [DIAS_SEMANA[d.dayofweek] for d in dia.index])
    dia.index = dia.index.strftime("%d/%m")
    print("Por dia (MW): média e máximo horário")
    print(dia.round(0).to_string())

    m["hora"] = m.valido_para.dt.hour
    m["tipo"] = np.where(m.valido_para.dt.dayofweek < 5, "útil", "fds")
    p = m.groupby(["tipo", "hora"])[["TOK_D1", "deck", "real"]].mean().unstack(0).round(0)
    print("\nPerfil horário médio (MW), dias úteis e fim de semana:")
    print(p.to_string())
    print("O que olhar: se o deck acompanha o teto do realizado (platô) e os vales de fim de semana, ele já desconta"
          " o corte. Se acompanha a TOK, o deck é previsão de vento e o corte acontece só na operação.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    m.to_csv(OUTPUT_DIR / f"corte_{sub}.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")
    fig, ax = plt.subplots(figsize=(16, 5))
    s = m.sort_values("valido_para")
    ax.plot(s.valido_para, s.real / 1000, color="black", lw=1.4, label="realizado")
    ax.plot(s.valido_para, s.deck / 1000, color="tab:blue", lw=1.1, label="deck DESSEM (delta 0)")
    ax.plot(s.valido_para, s.TOK_D1 / 1000, color="tab:orange", lw=1, ls="--", label="Tempo OK D+1")
    ax.set_ylabel("GW"); ax.grid(alpha=.3); ax.legend(loc="upper left")
    ax.set_title(f"Eólica {sub}: realizado x deck x Tempo OK D+1")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(OUTPUT_DIR / f"corte_{sub}.png", dpi=120); plt.close(fig)
    print(f"Gráfico: {OUTPUT_DIR / f'corte_{sub}.png'}")


@protegido
def cobertura(eng):
    secao("6. COBERTURA: dias com dado por ano x mês (tabelas inteiras; pode demorar)")
    q = {
        "TOK (dias de rodada)": f"SELECT CAST(rodada_dia AS date) AS d FROM {T_TOK} GROUP BY 1",
        "deck UEE (dias de rodada)": f"SELECT CAST(rodada_dia AS date) AS d FROM {T_DECK} WHERE tipo_fonte_energia = 'UEE' GROUP BY 1",
        "realizado (dias)": f"SELECT CAST(dia AS date) AS d FROM {T_REAL} GROUP BY 1",
    }
    for nome, s in q.items():
        log(f"cobertura {nome}...")
        d = sql(eng, s, timeout="1200s")
        d["d"] = pd.to_datetime(d.d)
        t = d.groupby([d.d.dt.year.rename("ano"), d.d.dt.month.rename("mes")]).size().unstack("mes").fillna(0).astype(int)
        print(f"\n--- {nome}: de {d.d.min().date()} a {d.d.max().date()} ---")
        print(t.to_string())


@protegido
def capacidade(eng):
    secao("7. TABELAS CANDIDATAS A CAPACIDADE INSTALADA / FATOR DE CAPACIDADE")
    t = sql(eng, """SELECT table_schema, table_name FROM information_schema.tables
                    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                      AND (table_name ILIKE '%capac%' OR table_name ILIKE '%fator%' OR table_name ILIKE '%eol%'
                           OR table_name ILIKE '%instalad%' OR table_name ILIKE '%usina%')
                    ORDER BY 2""")
    print("Tabelas pelo nome:")
    print(t.to_string(index=False) if len(t) else "  nenhuma")
    c = sql(eng, """SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                      AND (column_name ILIKE '%capac%' OR column_name ILIKE '%fator%' OR column_name ILIKE '%potencia%')
                    ORDER BY 1, 2""")
    print("\nColunas pelo nome:")
    print(c.to_string(index=False) if len(c) else "  nenhuma")
    for nome in sorted(set(t.table_name) | set(c.table_name)):
        if any(k in nome.lower() for k in ("capac", "fator", "instalad")):
            try:
                a = sql(eng, f'SELECT * FROM "{nome}" LIMIT 5', timeout="60s")
                print(f"\n--- amostra de {nome} ---\n{a.to_string(index=False)}")
            except Exception as e:  # noqa: BLE001
                print(f"\n--- {nome}: não consegui ler ({e})")


# ------------------------------------------------------------------ principal
def preparar(tok, deck):
    """TOK: MW, última rodada de cada dia, SIN. Deck: última revisão, delta, SIN."""
    tok, em_gw, med = tok_mw(tok)
    tok_todas = com_sin(tok, ["rodada_dia", "valido_para"])
    tok_d = ultima_por_dia(tok)
    tok_d = tok_d.groupby(["data_rodada", "valido_para", "subsistema"], as_index=False).mw.mean()
    tok_d = com_sin(tok_d, ["data_rodada", "valido_para"])
    tok_d["ant"] = (tok_d.valido_para.dt.normalize() - tok_d.data_rodada).dt.days.astype("int64")

    deck_r = ultima_rev(deck)
    deck_r = com_sin(deck_r, ["rodada_dia", "valido_para"])
    deck_r["delta"] = (deck_r.valido_para.dt.normalize() - deck_r.rodada_dia.dt.normalize()).dt.days.astype("int64")
    return tok, em_gw, med, tok_todas, tok_d, deck_r


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=120, help="dias de rodadas lidos (padrão 120)")
    ap.add_argument("--corte-ini", default="2026-09-01"); ap.add_argument("--corte-fim", default="2026-09-30")
    ap.add_argument("--sem-cobertura", action="store_true", help="pula a cobertura (consultas na tabela inteira)")
    a = ap.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    eng = engine_banco()
    fim = pd.Timestamp.today().normalize()
    ini = fim - pd.Timedelta(days=a.dias)
    c_ini, c_fim = pd.Timestamp(a.corte_ini), pd.Timestamp(a.corte_fim)
    ini_leit = min(ini, c_ini - pd.Timedelta(days=2))

    cols = estrutura_tabelas(eng) or {}
    cd = cols.get(T_DECK)
    col_rev = None
    if cd is not None:
        cand = [c for c in cd.column_name if c.lower() in ("rev", "revisao", "num_rev", "revision") or c.lower().startswith("rev")]
        col_rev = cand[0] if cand else None
    print(f"\nColuna de revisão do deck: {col_rev or 'nenhuma encontrada'}")
    deck_valores_distintos(eng)

    log(f"lendo de {ini_leit.date()} a {fim.date()}...")
    tok = ler_tok(eng, ini_leit, fim)
    deck = ler_deck(eng, ini_leit, fim, col_rev)
    real = ler_real(eng, ini_leit, fim + pd.Timedelta(days=11))
    log(f"TOK {len(tok):,} | deck {len(deck):,} | realizado {len(real):,} linhas")

    tok, em_gw, med, tok_todas, tok_d, deck_r = preparar(tok, deck)
    real = com_sin(real, ["valido_para"])

    estrutura_deck(deck)
    estrutura_tok(tok, em_gw, med)
    alinhamento(tok_d, deck_r, real)
    origem(tok_todas, tok_d, deck_r)
    ajuste(tok_d, deck_r)
    corte(tok_d, deck_r, real, c_ini, c_fim, "NE")
    if not a.sem_cobertura:
        cobertura(eng)
    capacidade(eng)
    log(f"fim. Arquivos em {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
