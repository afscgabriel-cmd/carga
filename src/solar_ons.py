"""Previsão de geração solar (UFV) do deck oficial do ONS -> base horária + gráficos.

Lê os arquivos `Previsoes_<REGIAO>_<DATA_DECK>_<DATA_PREVISAO>.txt` (CSV com ';')
da pasta "Previsao combinada", soma as usinas em cada horário (MW) e calcula a
média por hora.

Uso:
    python src/solar_ons.py data/Deck_Previsao_20260929.zip
    python src/solar_ons.py data/Deck_Previsao_20260929      # pasta já extraída
    python src/solar_ons.py <zip|pasta> --saida output
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

PADRAO_ARQ = re.compile(r"Previsoes_(?P<regiao>[A-Z]+)_(?P<deck>\d{8})_(?P<dia>\d{8})\.txt$", re.I)
PASTA = "Previsao combinada"


def _abrir_fontes(origem: Path):
    """Gera (nome_arquivo, bytes) de cada previsão, seja de zip ou de pasta."""
    if origem.is_file() and origem.suffix.lower() == ".zip":
        with zipfile.ZipFile(origem) as z:
            for nome in z.namelist():
                if PASTA in nome and PADRAO_ARQ.search(nome):
                    yield Path(nome).name, z.read(nome)
    else:
        for p in sorted(origem.rglob("*.txt")):
            if PASTA in p.parts and PADRAO_ARQ.search(p.name):
                yield p.name, p.read_bytes()


def ler_previsoes(origem: Path) -> pd.DataFrame:
    """DataFrame longo em 30 min: regiao, usina, datahora, mw."""
    partes = []
    for nome, conteudo in _abrir_fontes(origem):
        m = PADRAO_ARQ.search(nome)
        dia = pd.to_datetime(m["dia"], format="%Y%m%d")
        d = pd.read_csv(io.BytesIO(conteudo), sep=";", index_col=0)
        d.index.name = "usina"
        d.columns = [str(c).strip() for c in d.columns]
        d = d.apply(pd.to_numeric, errors="coerce")
        longo = d.stack().rename("mw").reset_index()
        longo.columns = ["usina", "hora", "mw"]
        longo["datahora"] = dia + pd.to_timedelta(longo["hora"] + ":00")
        longo["regiao"] = m["regiao"].upper()
        partes.append(longo[["regiao", "usina", "datahora", "mw"]])
    if not partes:
        raise SystemExit(f"Nenhum arquivo '{PASTA}/Previsoes_*.txt' encontrado em {origem}")
    df = pd.concat(partes, ignore_index=True)
    dup = df.duplicated(["usina", "datahora"]).sum()
    if dup:
        print(f"AVISO: {dup} registros repetidos (mesma usina/horário em mais de um arquivo).")
    return df


def somar_meia_hora(df: pd.DataFrame) -> pd.DataFrame:
    """Soma das usinas por região a cada 30 min, com grade completa (00:00-23:30).

    Horários sem dado no arquivo (madrugada/noite) são tratados como 0 MW.
    Coluna SIN = soma de todas as regiões.
    """
    s = df.pivot_table(index="datahora", columns="regiao", values="mw", aggfunc="sum")
    grade = pd.date_range(s.index.min().normalize(), s.index.max().normalize() + pd.Timedelta("23h30min"), freq="30min")
    s = s.reindex(grade).fillna(0.0)
    s["SIN"] = s.sum(axis=1)
    s.index.name = "datahora"
    return s


def media_horaria(meia_hora: pd.DataFrame) -> pd.DataFrame:
    """Média por hora (janela [h:00, h+1:00): pontos :00 e :30), em MW médios."""
    return meia_hora.resample("h").mean()


def grafico_linha_do_tempo(h: pd.DataFrame, arq: Path, deck: str):
    fig, ax = plt.subplots(figsize=(15, 5.5))
    regs = [c for c in h.columns if c != "SIN"]
    ax.plot(h.index, h["SIN"], color="black", lw=2, label="Total (soma das regiões)")
    for c in regs:
        ax.plot(h.index, h[c], lw=1.3, label=f"Arquivo {c}")
    ax.set_title(f"Previsão de geração solar (UFV) – média horária – deck ONS {deck}")
    ax.set_ylabel("MW médios")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(arq, dpi=130)
    plt.close(fig)


def grafico_perfis_diarios(h: pd.DataFrame, arq: Path, deck: str):
    fig, ax = plt.subplots(figsize=(11, 5.5))
    dias = sorted(set(h.index.date))
    cores = plt.cm.viridis(range(0, 256, max(1, 256 // len(dias))))
    for d, cor in zip(dias, cores):
        x = h[h.index.date == d]
        ax.plot(x.index.hour, x["SIN"], color=cor, lw=1.6, label=d.strftime("%d/%m"))
    ax.set_xticks(range(0, 24))
    ax.set_xlabel("Hora do dia")
    ax.set_ylabel("MW médios")
    ax.set_title(f"Perfil diário da previsão solar (total) – deck ONS {deck}")
    ax.grid(alpha=0.3)
    ax.legend(ncol=2, title="Dia", fontsize=8)
    fig.tight_layout()
    fig.savefig(arq, dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origem", type=Path, help="zip do deck ou pasta já extraída")
    ap.add_argument("--saida", type=Path, default=Path("output"))
    a = ap.parse_args()

    df = ler_previsoes(a.origem)
    deck = re.search(r"(\d{8})", " ".join(n for n, _ in _abrir_fontes(a.origem)))[1]
    meia = somar_meia_hora(df)
    hor = media_horaria(meia)

    a.saida.mkdir(parents=True, exist_ok=True)
    meia.round(3).to_csv(a.saida / f"solar_meia_hora_{deck}.csv", sep=";", decimal=",")
    hor.round(3).to_csv(a.saida / f"solar_horaria_{deck}.csv", sep=";", decimal=",")
    grafico_linha_do_tempo(hor, a.saida / f"solar_horaria_{deck}.png", deck)
    grafico_perfis_diarios(hor, a.saida / f"solar_perfis_diarios_{deck}.png", deck)

    print(f"Usinas por região: {df.groupby('regiao').usina.nunique().to_dict()} | "
          f"dias: {meia.index.normalize().nunique()} | período: {hor.index.min()} a {hor.index.max()}")
    print(hor.groupby(hor.index.date)["SIN"].agg(pico_MW="max", energia_MWh="sum").round(0))
    print(f"Arquivos gerados em {a.saida}/")


if __name__ == "__main__":
    main()
