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
