# -*- coding: utf-8 -*-
"""Estende as previsões de renováveis do DESSEM (UTE, PCH, CGH, UHE, MGD) além do
último dia coberto, usando a regra validada por backtest:

    curva(dia) = nível do último dia do DESSEM  x  perfil intradiário médio dos últimos 28 dias

Nível   = média das 48 meias-horas do último dia previsto pela rodada mais recente.
Perfil  = média, sobre os últimos 28 dias de D+0, da curva normalizada (MW / média do dia).
Sem separação por dia da semana nem correção sazonal (o backtest mostrou que pioram).

Saída: CSV longo no mesmo padrão das bases de carga/eólica:
    rodada_dia, valido_para_dia, valido_para, valido_para_hora, previsao_mw,
    cd_subsistema, mnemonico_subsistema, nome_subsistema, tipo_fonte_energia, origem
origem = "DESSEM" para os dias que a rodada cobre, "PERFIL" para os estendidos.

Uso (lê o banco via database_config.json):
    python gerar_renovaveis.py
    python gerar_renovaveis.py --horizonte 10
    python gerar_renovaveis.py --csv "C:\\caminho\\prev_renovaveis_dessem.csv"   # alternativa sem banco
"""
import argparse
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

import numpy as np
import pandas as pd

CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")
CSV_DESSEM = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\prev_renovaveis_dessem.csv")
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "renovaveis"

FONTES = ["UTE", "PCH", "CGH", "UHE", "MGD"]
DIAS_PERFIL = 28
SUBSISTEMAS = {"SE": (1, "Sudeste"), "S": (2, "Sul"), "NE": (3, "Nordeste"), "N": (4, "Norte")}


# ------------------------------------------------------------------ leitura
def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def ler_banco(engine, rodada=None, dias=None, completo=False):
    """Devolve (d0, ultima): histórico D+0 dos últimos DIAS_PERFIL+7 dias e a rodada mais recente completa.

    Mesma consulta do atualizar_renovaveis_dessem.py (faixa de rodada_dia em lotes de 7 dias),
    com limite de tempo por consulta para nunca ficar pendurado indefinidamente.
    """
    from sqlalchemy import text
    t0 = time.time()
    fim = pd.Timestamp(rodada) if rodada else pd.Timestamp.today().normalize()
    ini = fim - pd.Timedelta(days=dias or DIAS_PERFIL + 7)
    sql = text("""
        SELECT rodada_dia, valido_para_dia, valido_para, submercado, tipo_fonte_energia,
               SUM(previsao) AS previsao
        FROM fac_ons_renovaveis
        WHERE rodada_dia >= :ini AND rodada_dia < :fim
        GROUP BY rodada_dia, valido_para_dia, valido_para, submercado, tipo_fonte_energia
    """)
    print("Conectando ao banco...", flush=True)
    with engine.connect() as con:
        con.execute(text("SET statement_timeout = '900s'"))
        print(f"Lendo rodadas de {ini.date()} a {fim.date()} em lotes de 7 dias...", flush=True)
        partes = []
        for a in pd.date_range(ini, fim, freq="7D"):
            b = min(a + pd.Timedelta(days=7), fim + pd.Timedelta(days=1))
            partes.append(pd.read_sql(sql, con, params={"ini": a.date(), "fim": b.date()}))
            print(f"  {a.date()} a {(b - pd.Timedelta(days=1)).date()}: {len(partes[-1]):,} linhas ({time.time()-t0:.0f}s)", flush=True)
    partes = [x for x in partes if not x.empty]
    d = _tipar(pd.concat(partes, ignore_index=True)) if partes else pd.DataFrame(columns=["rodada_dia","valido_para_dia","valido_para","submercado","tipo_fonte_energia","previsao"])
    if d.empty:
        raise SystemExit("Nenhuma rodada nesse período.")
    rodada = d.rodada_dia.max()
    print(f"  rodada mais recente: {rodada.date()}", flush=True)
    d0 = d[pd.to_datetime(d.valido_para_dia) == d.rodada_dia]
    if completo:
        return d0, d[d.rodada_dia == rodada], d
    return d0, d[d.rodada_dia == rodada]


def ler_csv(caminho, rodada=None):
    """Lê o prev_renovaveis_dessem.csv (saída do atualizar_renovaveis_dessem.py) ou o extrato zip de teste."""
    caminho = Path(caminho)
    t0 = time.time()
    print(f"Lendo {caminho.name}...", flush=True)
    if caminho.suffix.lower() == ".zip":
        z = zipfile.ZipFile(caminho)
        d = pd.read_csv(z.open(z.namelist()[0]), sep=";", decimal=",")
    else:
        d = pd.read_csv(caminho, sep=";", decimal=",", encoding="utf-8-sig")
    if "previsao_mw" in d.columns:
        d = d.rename(columns={"previsao_mw": "previsao"})
    d = _tipar(d)
    print(f"  {len(d):,} linhas em {time.time()-t0:.0f}s", flush=True)
    fim = pd.Timestamp(rodada) if rodada else d.rodada_dia.max()
    ini = fim - pd.Timedelta(days=DIAS_PERFIL + 7)
    d = d[(d.rodada_dia >= ini) & (d.rodada_dia <= fim)]
    if d.empty:
        raise SystemExit("Nenhuma rodada nesse período.")
    rodada = d.rodada_dia.max()
    print(f"  rodada mais recente: {rodada.date()}", flush=True)
    d0 = d[d.valido_para.dt.normalize() == d.rodada_dia]
    return d0, d[d.rodada_dia == rodada]


def _tipar(d):
    d = d[d.tipo_fonte_energia.isin(FONTES)].copy()
    d["rodada_dia"] = pd.to_datetime(d.rodada_dia)
    d["valido_para"] = pd.to_datetime(d.valido_para)
    d["previsao"] = pd.to_numeric(d.previsao, errors="coerce")
    return d


# ------------------------------------------------------------------ regra
def perfil_intradiario(d0: pd.DataFrame) -> pd.DataFrame:
    """Perfil normalizado (48 slots) por fonte e submercado, média dos últimos DIAS_PERFIL dias."""
    d = d0.copy()
    d["slot"] = ((d.valido_para - d.rodada_dia).dt.total_seconds() // 1800).astype(int)
    m = d.pivot_table(index=["tipo_fonte_energia", "submercado", "rodada_dia"], columns="slot", values="previsao")
    m = m.groupby(level=[0, 1], group_keys=False).apply(lambda x: x.sort_index().tail(DIAS_PERFIL))
    norm = m.div(m.mean(axis=1).replace(0, np.nan), axis=0)
    perfil = norm.groupby(level=[0, 1]).mean().fillna(1.0)
    return perfil.reindex(columns=range(48)).interpolate(axis=1, limit_direction="both")


def estender(ultima: pd.DataFrame, perfil: pd.DataFrame, horizonte: int) -> pd.DataFrame:
    """Gera as curvas de 30 min para os dias após o último coberto pela rodada."""
    rodada = ultima.rodada_dia.iloc[0]
    # descarta dias parciais (o DESSEM termina às 00:00 do dia seguinte, que fica com 1 só ponto)
    dia = ultima.valido_para.dt.normalize()
    pontos = ultima.groupby(dia).valido_para.nunique()
    completos = pontos[pontos >= 40].index
    if len(completos) < len(pontos):
        print(f"  dias parciais descartados do DESSEM: {[d.date().isoformat() for d in pontos.index.difference(completos)]}", flush=True)
    ultima = ultima[dia.isin(completos)]
    ultimo_dia = completos.max()
    base = ultima[ultima.valido_para.dt.normalize() == ultimo_dia]
    nivel = base.groupby(["tipo_fonte_energia", "submercado"]).previsao.mean()
    dias = pd.date_range(ultimo_dia + pd.Timedelta(days=1), rodada + pd.Timedelta(days=horizonte))
    linhas = []
    for (f, s), n in nivel.items():
        p = perfil.loc[(f, s)].values if (f, s) in perfil.index else np.ones(48)
        for dia in dias:
            linhas.append(pd.DataFrame({
                "rodada_dia": rodada, "valido_para": dia + pd.to_timedelta(np.arange(48) * 30, unit="min"),
                "tipo_fonte_energia": f, "submercado": s, "previsao": n * p, "origem": "PERFIL"}))
    ext = pd.concat(linhas, ignore_index=True) if linhas else pd.DataFrame()
    dessem = ultima.assign(origem="DESSEM")
    return pd.concat([dessem, ext], ignore_index=True)


def formatar(df: pd.DataFrame) -> pd.DataFrame:
    """Padrão das bases de carga/eólica."""
    out = pd.DataFrame({
        "rodada_dia": df.rodada_dia.dt.strftime("%Y-%m-%d"),
        "valido_para_dia": df.valido_para.dt.strftime("%Y-%m-%d"),
        "valido_para": df.valido_para.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "valido_para_hora": df.valido_para.dt.strftime("%H:%M:%S"),
        "previsao_mw": df.previsao.round(3),
        "cd_subsistema": df.submercado.map(lambda s: SUBSISTEMAS[s][0]),
        "mnemonico_subsistema": df.submercado,
        "nome_subsistema": df.submercado.map(lambda s: SUBSISTEMAS[s][1]),
        "tipo_fonte_energia": df.tipo_fonte_energia,
        "origem": df.origem,
    })
    return out.sort_values(["tipo_fonte_energia", "mnemonico_subsistema", "valido_para"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizonte", type=int, default=10, help="dias após a rodada a cobrir (padrão 10, como o deck solar)")
    ap.add_argument("--rodada", help="data-limite da rodada (padrão: hoje; usa a mais recente até essa data)")
    ap.add_argument("--csv", help="em vez do banco: prev_renovaveis_dessem.csv ou extrato zip (teste)")
    a = ap.parse_args()

    if a.csv:
        d0, ultima = ler_csv(a.csv, a.rodada)
    else:
        d0, ultima = ler_banco(engine_banco(), a.rodada)
    if ultima.empty:
        raise SystemExit("Rodada sem dados.")

    perfil = perfil_intradiario(d0)
    res = formatar(estender(ultima, perfil, a.horizonte))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rod = res.rodada_dia.iloc[0].replace("-", "")
    arq = OUTPUT_DIR / f"prev_renovaveis_estendida_{rod}.csv"
    res.to_csv(arq, sep=";", decimal=",", index=False, encoding="utf-8-sig")

    resumo = res.assign(valido_para_dia=res.valido_para_dia).groupby(["valido_para_dia", "origem", "tipo_fonte_energia"]).previsao_mw.mean().unstack().round(0)
    print(f"Rodada {res.rodada_dia.iloc[0]} | dias DESSEM: {res[res.origem=='DESSEM'].valido_para_dia.nunique()} | "
          f"dias PERFIL: {res[res.origem=='PERFIL'].valido_para_dia.nunique()} | perfil com {d0.rodada_dia.nunique()} dias de histórico")
    print("\nMédia diária SIN (MW) — soma dos submercados:")
    print((resumo * 4).to_string() if False else res.groupby(["valido_para_dia", "origem", "tipo_fonte_energia"]).previsao_mw.sum().div(48).unstack().round(0).to_string())
    print(f"\nArquivo: {arq}")


if __name__ == "__main__":
    main()
