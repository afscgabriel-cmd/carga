# -*- coding: utf-8 -*-
"""Inspeciona a tabela fac_ons_geracao_solar (geração solar realizada): colunas, amostra,
resolução temporal, subsistemas/usinas e período coberto. Só leitura, consultas leves."""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gerar_renovaveis as gr

pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
T = "fac_ons_geracao_solar"

with gr.engine_banco().connect() as con:
    con.execute(text("SET statement_timeout = '300s'"))
    print("=== COLUNAS ===")
    print(pd.read_sql(text("""SELECT column_name, data_type FROM information_schema.columns
                              WHERE table_name = :t ORDER BY ordinal_position"""), con, params={"t": T}).to_string(index=False))

    print("\n=== 30 LINHAS MAIS RECENTES (ORDER BY dia DESC) ===")
    amostra = pd.read_sql(text(f"SELECT * FROM {T} ORDER BY dia DESC LIMIT 30"), con)
    print(amostra.to_string(index=False))

    print("\n=== 30 LINHAS DE UM DIA INTEIRO, ORDENADAS POR HORÁRIO ===")
    ultimo_dia = amostra["dia"].max()
    dia_full = pd.read_sql(text(f"SELECT * FROM {T} WHERE dia = :d ORDER BY 2, 3 LIMIT 5000"), con, params={"d": ultimo_dia})
    print(f"dia {ultimo_dia}: {len(dia_full)} linhas")
    print(dia_full.head(30).to_string(index=False))

    cols = [c.lower() for c in dia_full.columns]
    for cand in ["mnemonico_subsistema", "subsistema", "id_subsistema", "nom_subsistema", "cd_subsistema"]:
        if cand in cols:
            print(f"\nvalores distintos de {cand} no dia: {sorted(dia_full[cand].astype(str).unique())[:20]}")
    for cand in ["usina", "nom_usina", "id_usina", "ceg", "cod_usina", "nome_usina"]:
        if cand in cols:
            print(f"usinas distintas no dia ({cand}): {dia_full[cand].nunique()}")
    for cand in ["hora", "valido_para_hora", "din_instante", "instante", "valido_para", "data_hora"]:
        if cand in cols:
            print(f"horários distintos no dia ({cand}): {dia_full[cand].nunique()}  ex.: {sorted(dia_full[cand].astype(str).unique())[:6]}")

    print("\n=== PERÍODO COBERTO (últimos 400 dias) ===")
    print(pd.read_sql(text(f"""SELECT MIN(dia) ini, MAX(dia) fim, COUNT(DISTINCT dia) dias
                               FROM {T} WHERE dia >= CURRENT_DATE - 400"""), con).to_string(index=False))
