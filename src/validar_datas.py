# -*- coding: utf-8 -*-
"""Validação de datas, rodadas e horários das quatro fontes da carga líquida.

Imprime, para cada fonte: rodada usada, primeiro/último horário, pontos por dia (esperado 48),
horários faltantes ou duplicados, e se existe o ponto 00:00 (define a convenção de rótulo).
Depois mostra o trecho 17:00-19:30 do primeiro dia comum, lado a lado, para conferir o alinhamento.

Uso:  python validar_datas.py            (mesmos parâmetros --carga-csv/--eolica-csv/--renov-csv/--deck do carga_liquida.py)
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import carga_liquida as cl
import gerar_renovaveis as gr

pd.set_option("display.width", 200)


def checar(nome, s, rodada=None):
    print(f"\n=== {nome} ===")
    if rodada is not None:
        print(f"rodada: {rodada}")
    print(f"período: {s.valido_para.min()}  ->  {s.valido_para.max()}")
    print(f"subsistemas: {sorted(s.subsistema.unique())}")
    primeiro = s.valido_para.min().strftime('%H:%M')
    ultimo = s.valido_para.max().strftime('%H:%M')
    if primeiro == "00:30" and ultimo == "00:00":
        conv = "rótulo de FIM (00:30..24:00): o script desloca -30 min"
    elif primeiro == "00:00" and ultimo == "23:30":
        conv = "rótulo de INÍCIO (00:00..23:30): sem deslocamento"
    else:
        conv = "não conclusivo, verificar"
    print(f"primeiro horário: {primeiro} | último horário: {ultimo}  -> {conv}")
    passo = s.valido_para.drop_duplicates().sort_values().diff().dropna().mode()
    print(f"passo mais comum: {passo.iloc[0] if len(passo) else 'n/d'}")
    por_dia = s.groupby([s.valido_para.dt.date, "subsistema"]).valido_para.nunique().unstack()
    print("pontos por dia e subsistema (esperado 48):")
    print(por_dia.to_string())
    dup = s.duplicated(["valido_para", "subsistema"]).sum()
    if dup:
        print(f"ATENÇÃO: {dup} horários duplicados (mesmo horário/subsistema mais de uma vez)")
    grade = pd.date_range(s.valido_para.min(), s.valido_para.max(), freq="30min")
    falt = grade.difference(s.valido_para.unique())
    if len(falt):
        print(f"ATENÇÃO: {len(falt)} horários faltando na grade de 30 min, ex.: {[str(x) for x in falt[:5]]}")
    else:
        print("grade de 30 min completa, sem buracos")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck"); ap.add_argument("--carga-csv"); ap.add_argument("--eolica-csv"); ap.add_argument("--renov-csv")
    a = ap.parse_args()
    eng = None if (a.carga_csv and a.eolica_csv and a.renov_csv) else gr.engine_banco()

    carga_bruta, rod = cl.ler_carga(eng, a.carga_csv, deslocar=False)
    checar("CARGA (como está no banco, sem deslocamento)", carga_bruta, rod.date())
    carga = carga_bruta.copy()
    if cl.CARGA_ROTULO_FIM:
        carga["valido_para"] -= pd.Timedelta(minutes=30)
        print(f"\n-> CARGA_ROTULO_FIM = True: carga deslocada -30 min, agora {carga.valido_para.min()} -> {carga.valido_para.max()}")

    eol = cl.ler_eolica(eng, a.eolica_csv)
    checar("EÓLICA (já em MW)", eol)
    print(f"média por subsistema (MW): {eol.groupby('subsistema').mw.mean().round(0).to_dict()}")

    sol = cl.ler_solar(a.deck)
    checar("SOLAR (deck ONS)", sol)

    ren = cl.ler_renovaveis(eng, a.renov_csv, 10)
    for c in ["UTE", "PCH", "CGH", "UHE", "MGD"]:
        checar(f"RENOVÁVEIS {c}", ren[ren.componente == c])

    # alinhamento: trecho 17:00-19:30 do primeiro dia comum, SIN
    w = cl.montar([carga, eol, sol, ren])
    sin = w.xs("SIN", level=1)
    dia0 = sin.index.min().normalize() + pd.Timedelta(days=1)
    trecho = sin[(sin.index >= dia0 + pd.Timedelta(hours=16)) & (sin.index <= dia0 + pd.Timedelta(hours=20))]
    print(f"\n=== ALINHAMENTO: {dia0.date()}, 16:00-20:00, SIN (MW) ===")
    print(trecho[["carga", "eolica", "solar", "MGD", "carga_liquida"]].round(0).to_string())
    print("\nO que olhar: a solar e a MGD devem cair a zero por volta de 18:00-18:30; a carga deve subir até ~18:30-19:00.")
    print("Se a carga líquida der um 'degrau' ou uma ponta isolada num único ponto, a carga está defasada 30 min.")

    # horários zero-hora: renováveis vs carga (primeiro dia comum)
    print(f"\n=== PRIMEIRO E ÚLTIMO PONTO DE CADA FONTE NO DIA {dia0.date()} ===")
    for nome, s in [("carga", carga), ("eolica", eol), ("solar", sol), ("UTE", ren[ren.componente == "UTE"])]:
        x = s[s.valido_para.dt.normalize() == dia0]
        print(f"{nome:8s} {x.valido_para.min().strftime('%H:%M')} -> {x.valido_para.max().strftime('%H:%M')}  ({x.valido_para.nunique()} pontos)")


if __name__ == "__main__":
    main()
