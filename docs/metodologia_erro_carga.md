# Metodologia: erro da previsão de carga (prev_carga_dessem) por antecedência

Script: `src/erro_carga.py`. Objetivo: **diagnóstico**. "Nesta época do ano, para D+x, a previsão costuma
ficar y GW (z %) acima ou abaixo do oficial, com tal dispersão."

## 1. Dados

| Papel | Tabela | Observação |
|---|---|---|
| Previsão | `fac_sintegre_prev_carga_dessem` | modelo do ONS, 1 rodada por dia (manhã), 7 dias à frente, 30 min, rótulo no fim do intervalo |
| Oficial (padrão) | `fac_sintegre_carga_dessem_hourly` | carga do deck DESSEM: ajuste manual do operador, sai à tarde em D-1 e é a que entra em vigor |
| Oficial (alternativo) | `fac_ons_carga` | carga verificada |

O oficial é definido em **um só lugar**: `OFICIAL` e `OFICIAIS` no topo do script, ou `--oficial deck|realizado`.
Para o deck, o oficial do dia D é a linha com **delta = 0 (confirmado)**: o deck feito na véspera para o
próprio dia. O dia e o delta vêm das colunas `dia` e `delta` da própria tabela. Se elas divergirem da conta
`valido_para` / `dia - rodada`, o terminal avisa. Dia sem deck de delta 0 fica **fora** da análise. Antes, entrava
o deck anterior, o que misturava decks; para voltar a esse comportamento, `usar_rodada_anterior=True`.

Na previsão, se houver mais de uma rodada no mesmo dia, só a última entra.

Histórico: 2022 até hoje. Feriados: `data/feriados_nacionais.csv` (ANBIMA, 2001-2099). A planilha de 2026
enviada já trazia a série completa, então não precisa de mais anos.

## 2. Pareamento

1. As duas séries viram médias horárias (hora cheia de início). A previsão é deslocada 30 min antes, porque o
   rótulo marca o fim do intervalo.
2. SIN = soma de SE, S, NE e N, só nas horas em que os 4 existem.
3. Cada valor previsto é pareado com o oficial da mesma hora e subsistema. Antecedência
   `h = dia alvo - dia da rodada`, mantendo D+1 a D+7. D+0 fica de fora porque, quando a previsão roda, o
   oficial do dia já existe.
4. Só entram dias com as 24 horas completas.
   Antes disso, valores <= 0 ou fora de 30 % a 200 % da mediana do subsistema são descartados, com aviso.
   Linhas repetidas para o mesmo horário também geram aviso, e entra a média delas.
   Dias listados em `dias_excluidos.csv` (colunas `dia;motivo`, na pasta do script) saem da análise.
5. **Checagem de datas** (SIN, D+1). O MAE é calculado com o oficial deslocado em -1/0/+1 h e em -1/0/+1 dia, e
   também a % de horas em que a previsão de D+1 é idêntica ao oficial. Como o operador parte dessa previsão, com
   datas certas muitas horas saem iguais. Com datas desencontradas, quase nenhuma. Também é gerado `cobertura.csv`,
   com os dias por ano x mês: rodadas da previsão, dias do oficial e dias pareados.
6. **Checagem de alinhamento (antiga).** Calcula o MAE de D+1 deslocando o oficial em -1, 0 e +1 h. Se o mínimo não
   estiver em 0, a convenção de rótulo de alguma tabela está trocada. Ajuste em `rotulo_fim`.

Erro = **previsto - oficial**. Positivo quer dizer previsão acima do oficial. O erro % é sobre o oficial.

## 3. Recortes

| Recorte | Definição |
|---|---|
| Média | média das 24 h (energia do dia) |
| Ponta | máximo horário de cada curva, comparados entre si; mais a diferença e o acerto da hora da ponta |
| Perfil | erro hora a hora (MW, %) e **erro de formato** = `100 x (prev/média_prev - ofi/média_ofi)` em p.p., que mostra se a curva erra a forma, descontado o erro de nível do dia |

## 4. Calendário

O tipo do dia alvo é classificado assim:
- `util`, `sabado`, `domingo`;
- `feriado`: nacional em dia de semana. Feriado no fim de semana fica como sábado/domingo;
- `ponte`: dia útil entre um feriado e o fim de semana;
- `especial`: quarta-feira de Cinzas e 24 a 31/12.

As tabelas sazonais principais usam **só dias normais** (útil, sábado, domingo), para que feriados não
contaminem o mês. Feriados, pontes e dias especiais têm uma tabela própria (`resumo_tipo_dia.csv`).
Feriados estaduais e municipais não entram.

## 5. Estatísticas por célula

Para cada célula (métrica, subsistema, mês, antecedência):
- `n` e `n_semanas`;
- viés (MW e %);
- desvio padrão (MW e %), que é a variância pedida, em unidade legível;
- MAE, MAPE e RMSE;
- quantis P5, P25, P50, P75 e P95 (MW e %);
- IC 95 % do viés.

**IC por bootstrap em blocos semanais.** Dias seguidos têm erros correlacionados, e o IC clássico ficaria
estreito demais. As semanas são reamostradas inteiras (500 vezes). Se o IC cruza zero, o viés daquela célula
não se distingue de ruído.

**Leitura de amostra.** Com 2022 a 2026, cada mês tem de 3 a 5 anos, ou seja, cerca de 60 a 130 dias normais
por célula de mês x antecedência. Isso basta para viés e desvio. Os quantis P5 e P95 de um mês isolado são
instáveis: para faixas, prefira `resumo_horizonte.csv` ou agrupe meses vizinhos.

**Quebras.** `resumo_ano_mes.csv` e `serie_vies_mensal.png` mostram o viés mês a mês. Um degrau persistente
indica mudança de modelo ou de metodologia da carga. Nesse caso, restrinja o período com `--ini`.

## 6. Saídas (`src/output/erro_carga/<oficial>/`)

| Arquivo | Conteúdo |
|---|---|
| `quadro_agregado.csv` | **todo o histórico** por antecedência, em GW e %: viés e IC, desvio, MAE, MAPE, faixa P5-P95; dias normais e todos os dias |
| `resumo_mes_horizonte.csv` | tabela principal: métrica x subsistema x mês x h |
| `resumo_horizonte.csv` | idem, todos os meses; acerto da hora da ponta |
| `resumo_horizonte_todos_dias.csv` | idem, com feriados, pontes e dias especiais |
| `resumo_tipo_dia.csv` | por tipo de dia (inclui feriado, ponte, especial) |
| `resumo_ano_mes.csv` | viés por ano e mês |
| `perfil_hora.csv`, `perfil_mes_hora.csv` | erro horário e de formato |
| `piores_dias.csv` | 30 maiores erros por métrica e subsistema, com o erro de cada subsistema ao lado, para achar dado ruim |
| `erro_diario.csv`, `pares_horarios.csv.gz` | bases para análises próprias |
| `horizonte.png` | viés, desvio e MAPE por antecedência |
| `mes_horizonte_media.png`, `mes_horizonte_ponta.png` | mapas mês x antecedência (viés % e desvio %) |
| `leque.png` | faixas P5-P95 e P25-P75 por antecedência (MW) |
| `perfil_hora_horizonte.png` | hora x antecedência: viés horário e de formato |
| `serie_vies_mensal.png` | viés mensal no tempo (D+1, D+4, D+7) |

## 7. Fora do escopo (próximas fases)

- Erro da previsão de temperatura como explicação do erro de carga.
- Feriados regionais (estaduais) por subsistema.
- Decomposição `previsto - realizado = (previsto - oficial) + (oficial - realizado)`: basta rodar com
  `--oficial realizado` e comparar com o resultado do deck.

## 8. Cenários (teste de sensibilidade), em %

Os cenários são dados como **ajuste % sobre a previsão**, para chegar ao oficial:
`carga do cenário = previsão x (1 + ajuste/100)`, com `ajuste = 100 x (1/(1+erro) - 1)`.
Positivo quer dizer oficial acima da previsão. A amostra são as rodadas a ±`--janela` dias (padrão 15) da data
de referência, em todos os anos. A data de referência padrão é a rodada mais recente.

- **Dia a dia:** quantis P5, P10, P50, P90 e P95 do ajuste, por métrica, subsistema e h (`cenarios_fatores.csv`).
  P5 e P10 são os cenários altos, P90 e P95 os baixos.
- **Semana inteira:** semanas reais de erro (D+1..D+7 da mesma rodada, todos os subsistemas juntos) ordenadas pelo
  erro médio da semana no SIN. Cada cenário é a **média das semanas de uma faixa**: estresse alto (< P5), alto
  (P5-P15), central (P40-P60), baixo (P85-P95) e estresse baixo (> P95). Saem em `cenarios_semana.csv`, com os membros
  de cada faixa em `cenarios_semana_membros.csv`. Uma semana isolada não serve: o ranking só controla a média da
  semana, e o caminho dia a dia dela é ruído (por exemplo, uma semana de estresse alto com D+7 quase zero). A média
  do grupo cancela esse ruído e mantém a coerência (todas as semanas foram altas, ou baixas). Com poucas semanas por
  faixa (menos de 4), o script avisa: aumente `--janela`.
- `cenarios_aplicados_*.csv` aplica os cenários à rodada mais recente (MW), para uso direto.
- `--mesmo-dia-semana` restringe a amostra a rodadas do mesmo dia da semana (amostra cerca de 7x menor).

## 9. Calibração dos cenários (fora da amostra)

O teste simula o uso real. Para cada ano de teste Y, os cenários são calculados **só com os dias anteriores a
01/01/Y** (mesma janela de ±`--janela` dias) e aplicados às rodadas de Y. Depois, conta-se onde o oficial caiu:

| Saída | Esperado |
|---|---|
| dentro de P10-P90 | 80 % |
| dentro de P5-P95 | 90 % |
| oficial acima do cenário alto (erro < P10) | 10 % |
| oficial abaixo do cenário baixo (erro > P90) | 10 % |

Como ler:
- Bem menos de 80 % dentro: as faixas são otimistas e precisam ser alargadas.
- Bem mais de 80 % dentro: as faixas são conservadoras.
- "Acima do alto" bem acima de 10 %: o cenário alto é insuficiente.

Mesmo com erro estável ao longo do tempo, a cobertura fica um pouco abaixo do nominal (cerca de 76-79 %), porque os
quantis são estimados com amostra finita. Os cenários semanais são testados da mesma forma (erro médio da semana).
Saídas: `calibracao_horizonte.csv`, `calibracao_ano.csv`, `calibracao_semanas.csv`.
