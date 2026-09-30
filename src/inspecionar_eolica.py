# -*- coding: utf-8 -*-
"""Inspeciona fac_tempook_geracao_eolica_hourly: todas as colunas, e se a tabela guarda
dados consolidados (realizado) além da previsão. Só leitura, consultas leves."""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gerar_renovaveis as gr

pd.set_option("display.width", 220); pd.set_option("display.max_columns", 40)
T = "fac_tempook_geracao_eolica_hourly"

with gr.engine_banco().connect() as con:
    con.execute(text("SET statement_timeout = '300s'"))
    print("=== TODAS AS COLUNAS ===")
    print(pd.read_sql(text("""SELECT column_name, data_type FROM information_schema.columns
                              WHERE table_name = :t ORDER BY ordinal_position"""), con, params={"t": T}).to_string(index=False))

    d = pd.read_sql(text(f"SELECT * FROM {T} WHERE valido_para_dia >= CURRENT_DATE - 20 ORDER BY valido_para_dia DESC"), con)
    d["valido_para_dia"] = pd.to_datetime(d.valido_para_dia); d["rodada_dia"] = pd.to_datetime(d.rodada_dia)
    d["antecedencia"] = (d.valido_para_dia - d.rodada_dia).dt.days
    print(f"\n{len(d):,} linhas com valido_para_dia nos últimos 20 dias | rodadas: {d.rodada_dia.nunique()}")

    print("\n=== ANTECEDÊNCIA (valido_para_dia − rodada_dia) EM DIAS: contagem de linhas ===")
    print(d.antecedencia.value_counts().sort_index().to_string())
    print("  antecedência negativa = a rodada traz dias já passados (provável consolidado)")

    outras = [c for c in d.columns if c not in ["valido_para_dia", "valido_para", "valido_para_hora", "rodada_dia", "geracao",
                                                 "cd_subsistema", "mnemonico_subsistema", "nome_subsistema", "antecedencia"]]
    for c in outras:
        print(f"\n=== VALORES DISTINTOS DE '{c}' ===")
        print(d[c].astype(str).value_counts().head(15).to_string())

    print("\n=== PARA UM MESMO DIA (NE), VALORES DAS VÁRIAS RODADAS QUE O PREVIRAM ===")
    dia = d.valido_para_dia.max() - pd.Timedelta(days=5)
    x = d[(d.valido_para_dia == dia) & (d.mnemonico_subsistema == "NE") & (d.valido_para_hora.astype(str) == "12:00:00")]
    print(x.sort_values("rodada_dia").drop(columns=["cd_subsistema", "nome_subsistema"]).to_string(index=False))
    print("  se a rodada de dias DEPOIS desse dia tiver valor diferente das anteriores, é o consolidado")
