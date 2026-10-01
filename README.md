# Carga líquida – análise

Carga líquida = carga − eólica − solar (− MMGD, biomassa, hidráulicas *must run*… nas próximas fases).

## Renováveis do DESSEM (UTE, PCH, CGH, UHE, MGD)
`src/gerar_renovaveis.py` lê o banco (`fac_ons_renovaveis`), pega a rodada mais recente e
estende além do último dia coberto com `nível do último dia x perfil médio de 28 dias`
(regra escolhida por backtest: `src/backtest_renovaveis.py`, `backtest_30min.py`, `backtest_sazonal.py`).
Saída em `output/renovaveis/prev_renovaveis_estendida_<rodada>.csv`, formato longo igual às bases de carga/eólica.
Teste sem banco: `python src/gerar_renovaveis.py --csv data/renov_30min_2025.zip --rodada 2026-09-25`.

## Solar (UFV) – deck oficial do ONS
```
pip install -r requirements.txt
python src/solar_ons.py data/Deck_Previsao_YYYYMMDD.zip   # ou a pasta já extraída
```
Lê `Previsoes por Usinas/Previsao combinada/Previsoes_<NE|SE>_<deck>_<dia>.txt`
(CSV `;`, usinas nas linhas, horários de 30 min nas colunas, MW), soma as usinas
por horário, e calcula a média por hora (janela `[h:00, h+1:00)`).
Horários sem dado (madrugada/noite) contam como 0 MW.

Saídas em `output/`: `solar_meia_hora_*.csv`, `solar_horaria_*.csv` (colunas NE, SE, SIN),
`solar_horaria_*.png`, `solar_perfis_diarios_*.png`.

## Relatório de apresentação: de onde vem cada linha

`src/relatorio_carga_liquida.py` gera um gráfico por variável (30 dias, GW) com três linhas:

| Variável | Previsão (vermelho) | Programado (azul) | Realizado (verde) |
|---|---|---|---|
| Carga | `fac_sintegre_prev_carga_dessem` | `fac_sintegre_carga_dessem_hourly` (delta 0) | `fac_ons_carga` |
| Eólica | `fac_tempook_geracao_eolica_hourly` | `fac_ons_renovaveis`, fonte UEE, D+0 | `fac_ons_geracao_eolica` |
| Solar UFV | deck ONS (`Deck_Previsao_*.zip`) | `fac_ons_renovaveis`, fonte UFV, D+0 | `fac_ons_geracao_solar` |
| UTE, PCH, CGH, UHE, MGD | DESSEM estendido por perfil (`gerar_renovaveis.py`) | `fac_ons_renovaveis`, D+0 | sem realizado por fonte: usa o programado |
| Carga líquida | carga − soma das previsões | carga − soma dos programados | carga − eólica − solar realizadas − flats programadas |

Programado = o deck DESSEM do próprio dia (D+0). Dia cuja rodada ainda não entrou no banco: usa a rodada
mais recente anterior (o terminal avisa). A fronteira passado/previsão é a data mais recente entre as rodadas.
