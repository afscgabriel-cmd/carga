# -*- coding: utf-8 -*-
"""Inspeção da fac_ons_dessem_pdo_eolica (PDO do DESSEM, por usina/conjunto): o DESSEM programa o corte?

A tabela tem, por usina e meia hora, 'geracao' (previsão disponível) e 'geracao_operada' (o que o DESSEM
programou). Este script agrega por submercado e por tipo (sufixo do nome: CJE, CJFV, MMGD...) num período curto e
compara com a eólica do deck (fac_ons_renovaveis UEE, delta 0) e com o realizado (fac_ons_geracao_eolica). Só leitura.

Responde:
  1. Quais tipos (sufixos) existem e quais formam a eólica do deck (UEE).
  2. Se 'geracao_operada' fica abaixo de 'geracao' nas horas de sol (o DESSEM programa o corte) e se acompanha o
     realizado.
  3. Revisões e antecedências da PDO.

Uso:
    python inspecionar_pdo_eolica.py                              # 07/09 a 20/09/2026
    python inspecionar_pdo_eolica.py --ini 2026-09-07 --fim 2026-09-20
Saídas em output/inspecao_eolica/.
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
T_PDO = "fac_ons_dessem_pdo_eolica"
SUBS = ["SE", "S", "NE", "N"]

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 60); pd.set_option("display.max_rows", 200)
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:5.0f}s] {msg}", flush=True)


def secao(titulo):
    print(f"\n{'=' * 100}\n{titulo}\n{'=' * 100}", flush=True)


def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def sql(eng, q, timeout="900s", **params):
    from sqlalchemy import text
    with eng.connect() as con:
        con.execute(text(f"SET statement_timeout = '{timeout}'"))
        return pd.read_sql(text(q), con, params=params)


# ------------------------------------------------------------------ leitura
def ler_pdo(eng, ini, fim):
    """Uma consulta só para o período: a tabela (~140 milhões de linhas) não tem índice útil em rodada_dia, então
    cada consulta varre a tabela inteira (~60 s); consultar dia a dia multiplica esse tempo."""
    p = sql(eng, f"""
        SELECT rodada_dia, valido_para, BTRIM(submerc) AS subsistema,
               BTRIM(SPLIT_PART(nome, '_', 2)) AS tipo, rev,
               SUM(geracao) AS geracao, SUM(geracao_operada) AS operada,
               COUNT(*) AS n_linhas, COUNT(DISTINCT BTRIM(nome)) AS n_usinas,
               MAX(potencia) AS pot_max, MAX(fatcap) AS fatcap_max
        FROM {T_PDO}
        WHERE rodada_dia >= :a AND rodada_dia <= :b
        GROUP BY rodada_dia, valido_para, BTRIM(submerc), BTRIM(SPLIT_PART(nome, '_', 2)), rev""",
            timeout="1800s", a=ini.date(), b=fim.date())
    log(f"  PDO: {len(p):,} linhas agregadas, rodadas {p.rodada_dia.nunique()}")
    p = p.copy()
    p["rodada_dia"] = pd.to_datetime(p.rodada_dia); p["valido_para"] = pd.to_datetime(p.valido_para)
    for c in ("geracao", "operada"):
        p[c] = pd.to_numeric(p[c], errors="coerce")
    p["tipo"] = p.tipo.replace("", "(sem sufixo)")
    return p


def ler_uee(eng, ini, fim):
    d = sql(eng, """SELECT rodada_dia, valido_para, submercado AS subsistema, SUM(previsao) AS mw
                    FROM fac_ons_renovaveis
                    WHERE tipo_fonte_energia = 'UEE' AND rodada_dia >= :a AND rodada_dia <= :b
                      AND valido_para_dia = rodada_dia
                    GROUP BY rodada_dia, valido_para, submercado""", a=ini.date(), b=fim.date())
    d["valido_para"] = pd.to_datetime(d.valido_para)
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    return d[["valido_para", "subsistema", "mw"]]


def ler_real(eng, ini, fim):
    d = sql(eng, "SELECT subsistema, dia, hora, carga FROM fac_ons_geracao_eolica WHERE dia >= :a AND dia <= :b",
            a=ini.date(), b=fim.date())
    d["valido_para"] = pd.to_datetime(d.dia.astype(str)) + pd.to_timedelta(d.hora.astype(str))
    d["mw"] = pd.to_numeric(d.carga, errors="coerce")
    d["subsistema"] = d.subsistema.astype(str).str.strip()
    return d.loc[d.mw <= 60000, ["valido_para", "subsistema", "mw"]]


# ------------------------------------------------------------------ análise
def ultima_rev(p):
    r = pd.to_numeric(p.rev, errors="coerce")
    ult = r.groupby(p.rodada_dia).transform("max")
    return p[r == ult]


def estrutura(p):
    secao("1. ESTRUTURA DA PDO: revisões, antecedências, rótulo")
    revs = p.groupby("rodada_dia").rev.nunique()
    print(f"Revisões por rodada: {revs.value_counts().sort_index().to_dict()} | valores de rev: "
          f"{p.rev.value_counts().sort_index().to_dict()}")
    delta = (p.valido_para.dt.normalize() - p.rodada_dia).dt.days.astype("int64")
    print(f"Antecedência (dia de valido_para − rodada_dia): {delta.value_counts().sort_index().to_dict()}")
    u = p[p.rodada_dia == p.rodada_dia.max()]
    print(f"Rodada {u.rodada_dia.max().date()}: valido_para de {u.valido_para.min()} a {u.valido_para.max()}, "
          f"{u.valido_para.nunique()} horários")
    print(f"Submercados: {sorted(p.subsistema.unique())}")
    print(f"potencia (máx. por grupo): {p.pot_max.value_counts().head(5).to_dict()} | "
          f"fatcap (máx.): {p.fatcap_max.value_counts().head(5).to_dict()}  (9999 e 1 = sem informação)")


def por_tipo(p, uee):
    secao("2. TIPOS (sufixo do nome) por submercado: médias no período, MW; correlação com a eólica do deck (UEE)")
    g = p.groupby(["subsistema", "tipo", "valido_para"], as_index=False)[["geracao", "operada"]].sum()
    us = p.groupby(["subsistema", "tipo"]).n_usinas.max()
    t = g.groupby(["subsistema", "tipo"]).agg(geracao=("geracao", "mean"), operada=("operada", "mean"))
    t["n_usinas"] = us
    t["% cortado"] = 100 * (1 - t.operada / t.geracao.replace(0, np.nan))
    cor = []
    for (s, tp), x in g.groupby(["subsistema", "tipo"]):
        m = x.merge(uee[uee.subsistema == s], on="valido_para")
        cor.append(((s, tp), m.geracao.corr(m.mw) if len(m) > 10 else np.nan))
    t["corr c/ UEE"] = pd.Series(dict(cor))
    t = t.sort_values(["subsistema", "geracao"], ascending=[True, False])
    print(t.round(2).to_string())
    print("\nUEE do deck (fac_ons_renovaveis, delta 0), média no período, MW: "
          f"{uee.groupby('subsistema').mw.mean().round(0).to_dict()}")
    print("O que olhar: os tipos eólicos têm correlação alta com a UEE e, somados, devem dar a média da UEE.")
    return g, t


def candidatos(t, uee, limiar=0.8):
    """Tipos com correlação >= limiar com a UEE do mesmo submercado."""
    c = t[t["corr c/ UEE"] >= limiar].reset_index()
    return {s: sorted(x.tipo) for s, x in c.groupby("subsistema")}


def comparar_soma(g, cand, uee):
    secao("3. SOMA DOS TIPOS EÓLICOS (corr >= 0,8) x UEE do deck: é a mesma coisa?")
    linhas = []
    for s, tipos in cand.items():
        x = g[(g.subsistema == s) & g.tipo.isin(tipos)].groupby("valido_para", as_index=False)[["geracao", "operada"]].sum()
        m = x.merge(uee[uee.subsistema == s], on="valido_para")
        e = m.geracao - m.mw
        linhas.append({"subsistema": s, "tipos": ", ".join(tipos), "n": len(m), "PDO geracao": m.geracao.mean(),
                       "UEE": m.mw.mean(), "MAE": e.abs().mean(), "% iguais (<1 MW)": 100 * (e.abs() < 1).mean()})
    print(pd.DataFrame(linhas).round(1).to_string(index=False))
    print("O que olhar: % iguais alto = a UEE do deck é exatamente a 'geracao' (disponível) desses tipos na PDO.")


def perfil(g, cand, uee, real, sub="NE"):
    secao(f"4. O DESSEM PROGRAMA O CORTE? Perfil horário médio {sub} (MW): disponível x operada x realizado")
    tipos = cand.get(sub, [])
    if not tipos:
        print("Sem tipos candidatos."); return
    x = g[(g.subsistema == sub) & g.tipo.isin(tipos)].groupby("valido_para", as_index=False)[["geracao", "operada"]].sum()
    x["hora"] = x.valido_para.dt.floor("h")
    x = x.groupby("hora", as_index=False)[["geracao", "operada"]].mean().rename(columns={"hora": "valido_para"})
    u = uee[uee.subsistema == sub].copy(); u["valido_para"] = u.valido_para.dt.floor("h")
    u = u.groupby("valido_para", as_index=False).mw.mean().rename(columns={"mw": "UEE"})
    r = real[real.subsistema == sub][["valido_para", "mw"]].rename(columns={"mw": "real"})
    m = x.merge(u, on="valido_para", how="outer").merge(r, on="valido_para", how="outer").sort_values("valido_para")
    m = m[m.valido_para.dt.normalize().isin(x.valido_para.dt.normalize().unique())]
    m["hora"] = m.valido_para.dt.hour
    m["tipo_dia"] = np.where(m.valido_para.dt.dayofweek < 5, "útil", "fds")
    p = m.groupby(["tipo_dia", "hora"])[["UEE", "geracao", "operada", "real"]].mean().unstack(0).round(0)
    print(p.to_string())
    dia = m.groupby(m.valido_para.dt.normalize())[["UEE", "geracao", "operada", "real"]].mean().round(0)
    dia.index = [f"{d:%d/%m} {['seg','ter','qua','qui','sex','sáb','dom'][d.dayofweek]}" for d in dia.index]
    print(f"\nMédia diária {sub} (MW):\n{dia.to_string()}")
    print("O que olhar: se 'operada' afunda nas horas de sol como o realizado, o DESSEM já programa o corte e a"
          " 'verdade do deck' é a operada. Se 'operada' = 'geracao', o corte só acontece na operação.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    m.to_csv(OUTPUT_DIR / f"pdo_{sub}.csv", sep=";", decimal=",", index=False, encoding="utf-8-sig")
    fig, ax = plt.subplots(figsize=(16, 5))
    ax.plot(m.valido_para, m.real / 1000, color="black", lw=1.4, label="realizado")
    ax.plot(m.valido_para, m.geracao / 1000, color="tab:blue", lw=1.1, label="PDO disponível (geracao)")
    ax.plot(m.valido_para, m.operada / 1000, color="tab:red", lw=1.1, label="PDO operada (geracao_operada)")
    ax.plot(m.valido_para, m.UEE / 1000, color="tab:cyan", lw=.8, ls=":", label="deck UEE")
    ax.set_ylabel("GW"); ax.grid(alpha=.3); ax.legend(loc="upper left")
    ax.set_title(f"Eólica {sub}: disponível x operada (PDO DESSEM) x realizado")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(OUTPUT_DIR / f"pdo_{sub}.png", dpi=120); plt.close(fig)
    print(f"Gráfico: {OUTPUT_DIR / f'pdo_{sub}.png'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ini", default="2026-09-07"); ap.add_argument("--fim", default="2026-09-20")
    a = ap.parse_args()
    ini, fim = pd.Timestamp(a.ini), pd.Timestamp(a.fim)
    eng = engine_banco()
    log(f"lendo PDO de {ini.date()} a {fim.date()} (uma consulta só; 1 a 3 min)...")
    p = ler_pdo(eng, ini, fim)
    uee = ler_uee(eng, ini, fim)
    real = ler_real(eng, ini, fim + pd.Timedelta(days=1))
    log(f"PDO {len(p):,} | UEE {len(uee):,} | realizado {len(real):,}")

    for f, args in ((estrutura, (p,)),):
        try:
            f(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    p = ultima_rev(p)
    p = p[p.valido_para.dt.normalize() == p.rodada_dia]
    try:
        g, t = por_tipo(p, uee)
        cand = candidatos(t, uee)
        print(f"\nTipos eólicos candidatos (corr >= 0,8): {cand}")
        comparar_soma(g, cand, uee)
        perfil(g, cand, uee, real, "NE")
        perfil(g, cand, uee, real, "S")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    log("fim")


if __name__ == "__main__":
    main()
