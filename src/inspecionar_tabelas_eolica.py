# -*- coding: utf-8 -*-
"""Inspeção rápida das tabelas candidatas a origem do deck eólico e a capacidade instalada. Só leitura.

Para cada tabela: colunas, tamanho aproximado, período das colunas de data, 5 linhas de exemplo e valores
distintos das colunas de texto (numa amostra).

Uso:
    python inspecionar_tabelas_eolica.py
    python inspecionar_tabelas_eolica.py --tabelas fac_ons_eolica_weol pdo_eolica
"""
import argparse
import json
import traceback
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd

CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")
TABELAS = ["fac_ons_eolica_weol", "fac_ons_eolica_weol_semanal", "fac_central_eolica_weol",
           "fac_central_eolica_weol_semanal", "fac_ons_pdo_eolica", "fac_ons_dessem_pdo_eolica", "pdo_eolica",
           "fac_aneel_ralie_usinas", "dim_aneel_ralie_usinas"]

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 60); pd.set_option("display.max_colwidth", 40)


def engine_banco():
    from sqlalchemy import create_engine
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return create_engine(
        f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
        f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
    )


def sql(eng, q, timeout="120s", **params):
    from sqlalchemy import text
    with eng.connect() as con:
        con.execute(text(f"SET statement_timeout = '{timeout}'"))
        return pd.read_sql(text(q), con, params=params)


def inspecionar(eng, t):
    print(f"\n{'=' * 110}\n{t}\n{'=' * 110}", flush=True)
    c = sql(eng, """SELECT column_name, data_type FROM information_schema.columns
                    WHERE table_name = :t ORDER BY ordinal_position""", t=t)
    if c.empty:
        print("  tabela não encontrada"); return
    n = sql(eng, "SELECT reltuples::bigint AS n FROM pg_class WHERE relname = :t", t=t)
    print(f"linhas (aprox.): {int(n.n.iloc[0]):,}" if len(n) else "linhas (aprox.): ?")
    print(c.to_string(index=False))

    datas = c[c.data_type.str.contains("date|timestamp")].column_name.tolist()
    for col in datas:
        try:
            r = sql(eng, f'SELECT MIN("{col}") AS ini, MAX("{col}") AS fim FROM "{t}"')
            print(f"  período de {col}: {r.ini.iloc[0]} a {r.fim.iloc[0]}")
        except Exception as e:  # noqa: BLE001
            print(f"  período de {col}: não consegui ({str(e).splitlines()[0]})")

    ordem = f' ORDER BY "{datas[0]}" DESC' if datas else ""
    try:
        a = sql(eng, f'SELECT * FROM "{t}"{ordem} LIMIT 20000')
    except Exception:  # noqa: BLE001
        a = sql(eng, f'SELECT * FROM "{t}" LIMIT 20000')
    print(f"\n5 linhas de exemplo (das mais recentes):\n{a.head(5).to_string(index=False)}")
    texto = [x for x in a.columns if a[x].dtype == object and a[x].nunique() <= 30]
    for x in texto:
        print(f"\n'{x}' (amostra de {len(a):,} linhas): {a[x].astype(str).value_counts().head(15).to_dict()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tabelas", nargs="*", default=TABELAS)
    a = ap.parse_args()
    eng = engine_banco()
    for t in a.tabelas:
        try:
            inspecionar(eng, t)
        except Exception as e:  # noqa: BLE001
            print(f"\n!!! ERRO em {t}: {e}"); traceback.print_exc()


if __name__ == "__main__":
    main()
