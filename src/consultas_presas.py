# -*- coding: utf-8 -*-
"""Lista as consultas do seu usuário que ainda estão rodando no PostgreSQL e,
se você confirmar, cancela as que estão presas (sobras de execuções interrompidas com Ctrl+C).

Uso:  python consultas_presas.py
"""
import json
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, text

CONFIG_PATH = Path(r"C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json")

cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
eng = create_engine(
    f"postgresql+psycopg2://{quote_plus(cfg['POSTGRES_USERNAME'])}:{quote_plus(cfg['POSTGRES_PASSWORD'])}@"
    f"{cfg['postgres_host']}:{cfg['postgres_port']}/{cfg['postgres_database']}"
)

with eng.connect() as con:
    ativas = pd.read_sql(text("""
        SELECT pid, state, now() - query_start AS rodando_ha, left(query, 70) AS consulta
        FROM pg_stat_activity
        WHERE usename = current_user AND state = 'active' AND pid <> pg_backend_pid()
        ORDER BY query_start
    """), con)

    if ativas.empty:
        print("Nenhuma consulta presa do seu usuário. O servidor está limpo.")
    else:
        print(f"{len(ativas)} consulta(s) ainda rodando no servidor:\n")
        print(ativas.to_string(index=False))
        resp = input("\nCancelar TODAS elas? (s/n): ").strip().lower()
        if resp == "s":
            for pid in ativas.pid:
                ok = con.execute(text("SELECT pg_cancel_backend(:pid)"), {"pid": int(pid)}).scalar()
                print(f"  pid {pid}: {'cancelada' if ok else 'não foi possível cancelar'}")
        else:
            print("Nada cancelado.")
