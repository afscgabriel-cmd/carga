# -*- coding: utf-8 -*-
"""Constrained-off / curtailment de usinas eólicas (Dados Abertos ONS, RESTRICAO_COFF_EOLICA_AAAA_MM).

Lê um ou mais arquivos mensais (.xlsx ou .csv) e agrega por subsistema e meia hora:
    geração verificada, geração de referência (o que teria gerado sem corte) e
    geração frustrada (GNRa) separada por razão: REL (elétrica), CNF (confiabilidade), ENE (energética).

Uso:
    python coff_eolica.py data/RESTRICAO_COFF_EOLICA_2026_10.xlsx [outros arquivos...]
Saídas em output/coff/: coff_eolica_30min.csv, coff_eolica_hora.png, coff_eolica_dia.png
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUT = Path(__file__).resolve().parent / "output" / "coff"
RAZOES = {"ENE": ("Energética", "#d62728"), "CNF": ("Confiabilidade", "#ff7f0e"), "REL": ("Elétrica (constrained-off)", "#1f77b4"),
          "PAR": ("Parecer de acesso", "#9467bd")}


def ler(arquivos):
    partes = []
    for a in arquivos:
        a = Path(a)
        d = pd.read_excel(a) if a.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(a, sep=";", decimal=".")
        partes.append(d)
    d = pd.concat(partes, ignore_index=True)
    d["din_instante"] = pd.to_datetime(d.din_instante)
    d["razao"] = d.cod_razaorestricao.fillna("-")
    return d


def agregar(d):
    base = d.groupby(["din_instante", "id_subsistema"])[["val_geracao", "val_geracaoreferencia"]].sum(min_count=1)
    gnra = d.pivot_table(index=["din_instante", "id_subsistema"], columns="razao",
                         values="val_geracaonaorealizadaapurada", aggfunc="sum").fillna(0)
    gnra = gnra.drop(columns=[c for c in gnra.columns if c == "-"], errors="ignore")
    gnra.columns = [f"frustrada_{c}" for c in gnra.columns]
    w = base.join(gnra).fillna(0)
    sin = w.groupby(level=0).sum(); sin["id_subsistema"] = "SIN"
    w = pd.concat([w, sin.set_index("id_subsistema", append=True)]).sort_index()
    w["frustrada_total"] = w.filter(like="frustrada_").sum(axis=1)
    return w


def main():
    arqs = sys.argv[1:] or sorted(Path("data").glob("RESTRICAO_COFF_EOLICA_*"))
    d = ler(arqs)
    w = agregar(d)
    OUT.mkdir(parents=True, exist_ok=True)
    w.round(1).to_csv(OUT / "coff_eolica_30min.csv", sep=";", decimal=",", encoding="utf-8-sig")
    s = w.xs("SIN", level=1)
    cols = [c for c in s.columns if c.startswith("frustrada_") and c != "frustrada_total"]
    print(f"{d.din_instante.min()} a {d.din_instante.max()} | {d.id_ons.nunique()} usinas/conjuntos")
    print(f"SIN, médias (MW): verificada {s.val_geracao.mean():.0f} | referência {s.val_geracaoreferencia.mean():.0f} | "
          + " | ".join(f"frustrada {c[10:]} {s[c].mean():.0f}" for c in cols))

    # perfil por hora: verificada + frustrada empilhada por razão
    h = s.groupby(s.index.hour).mean()
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(h.index, h.val_geracao / 1000, color="black", lw=2, marker="o", label="Geração verificada")
    base = h.val_geracao / 1000
    for c in cols:
        nome, cor = RAZOES.get(c[10:], (c[10:], "gray"))
        ax.fill_between(h.index, base, base + h[c] / 1000, color=cor, alpha=.55, label=f"Frustrada: {nome}")
        base = base + h[c] / 1000
    ax.set_xticks(range(24)); ax.set_xlabel("hora"); ax.set_ylabel("GW"); ax.grid(alpha=.25); ax.legend(loc="lower left")
    ax.set_title("Eólica SIN: geração verificada + geração frustrada por razão, média por hora", loc="left", fontsize=13)
    fig.text(0.01, 0.01, f"Fonte: ONS, Restrição de operação por constrained-off de usinas eólicas ({d.din_instante.min():%d/%m} a {d.din_instante.max():%d/%m/%Y})", fontsize=9, color="#555")
    fig.tight_layout(rect=(0, 0.03, 1, 1)); fig.savefig(OUT / "coff_eolica_hora.png", dpi=120); plt.close(fig)

    # por dia
    dd = s.groupby(s.index.normalize())[cols + ["val_geracao"]].mean() / 1000
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.bar(dd.index, dd.val_geracao, color="#999", label="Geração verificada", width=.7)
    base = dd.val_geracao
    for c in cols:
        nome, cor = RAZOES.get(c[10:], (c[10:], "gray"))
        ax.bar(dd.index, dd[c], bottom=base, color=cor, alpha=.8, label=f"Frustrada: {nome}", width=.7); base = base + dd[c]
    ax.set_ylabel("GW médios"); ax.grid(alpha=.25, axis="y"); ax.legend()
    ax.set_title("Eólica SIN: geração verificada e frustrada por dia", loc="left", fontsize=13)
    fig.tight_layout(); fig.savefig(OUT / "coff_eolica_dia.png", dpi=120)
    print(f"Arquivos em {OUT}")


if __name__ == "__main__":
    main()
