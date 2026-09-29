# Metodologia: extensão das previsões de renováveis do DESSEM

Fontes: UTE (biomassa), PCH, CGH, UHE (pequenas / must run) e MGD (micro e minigeração distribuída).

## 1. Problema

A carga líquida precisa das previsões dessas fontes para os próximos 7 a 10 dias. O DESSEM
fornece previsões oficiais boas, mas o horizonte termina na sexta-feira da semana operativa:
na quinta, os dias de sábado em diante ficam descobertos. Não existe modelo de previsão
dedicado para essas fontes, e elas têm comportamento quase constante no curto prazo.

Objetivo: **estender a curva do DESSEM além do último dia coberto**, com a regra mais simples
que erre pouco e seja fácil de manter atualizada.

## 2. Dados

Tabela `fac_ons_renovaveis` (PostgreSQL), com as previsões de renováveis que entram no DESSEM:

| Coluna | Uso |
|---|---|
| `rodada_dia` | dia em que o DESSEM rodou |
| `valido_para` | horário previsto, resolução de 30 min |
| `tipo_fonte_energia` | UTE, PCH, CGH, UHE, MGD (UEE e UFV existem, mas têm modelo próprio) |
| `submercado` | SE, S, NE, N |
| `previsao` | MW |
| `rev` | revisão semanal do PMO; irrelevante aqui, só a rodada mais recente importa |

Histórico usado: 31/12/2022 a 28/09/2026 (1.261 dias com dado; MGD a partir de 29/04/2023).
Para o backtest, o "realizado" de referência é o **D+0 de cada rodada** (a previsão do DESSEM
para o próprio dia), que é a melhor referência disponível para essas fontes, já que o
realizado oficial por fonte não separa da mesma forma.

Todos os dias foram mantidos, inclusive períodos com lacunas ou valores repetidos, por serem
dados oficiais.

## 3. Comportamento observado

**Intradiário (formato):** variação de um dia para o outro de 2 a 4,5 %.
- UTE: reta, ±2 % ao longo do dia.
- PCH, CGH, UHE: "banheira": mínimo entre 11 h e 14 h (−4 a −8 %), pico às 20 h (+4 a +7 %).
- MGD: curva solar; pico = 3,5× a média diária, zero à noite.
- Dia útil, sábado e domingo diferem em menos de 1 %.

**Sazonal (nível):**
- UTE: safra da cana. ~4,2 GW de maio a outubro, 1,3 a 1,5 GW em fevereiro/março.
- PCH, CGH, UHE: hidrologia. Altas no úmido (fev-abr), baixas no seco. 2026 ~40 % acima de 2025.
- MGD: crescimento de ~30 % ao ano, somado ao ciclo solar.

O próprio DESSEM repete o mesmo valor diário de UTE, PCH, CGH e UHE em todos os dias de uma
rodada (só a MGD varia), ou seja, o ONS já usa persistência para essas fontes.

## 4. Métodos candidatos

Todos usam apenas dados disponíveis até a sexta (último dia coberto pelo DESSEM).

| Método | Regra |
|---|---|
| Persistência | repete o nível do último dia do DESSEM |
| Média móvel 7 / 14 / 28 dias | média do D+0 dos últimos N dias |
| Mediana 14 dias | idem, mediana |
| Tendência 14 / 28 dias | reta ajustada nos últimos N dias, extrapolada |
| Sazonal ajustado | mesmo período do ano anterior, escalado pelo nível recente |
| Persistência + deriva sazonal | persistência × (variação que o mesmo período teve no ano anterior), 1 ou 2 anos |
| Formato: persistir a sexta | copia a curva de 30 min da sexta |
| Formato: persistir a semana | copia a curva do mesmo dia da semana anterior |
| Formato: nível × perfil 28 d | nível × curva normalizada média dos últimos 28 dias |
| Formato: nível × perfil por tipo de dia | idem, separando útil / sábado / domingo |

## 5. Desenho do backtest

Simula a lacuna real. Em cada quinta-feira do histórico:
1. usa só os dados até a sexta seguinte;
2. prevê de sábado até a quinta seguinte (7 dias), e também até 28 dias para testar horizontes longos;
3. compara com o D+0 do DESSEM desses dias.

Métricas: MAE (MW), MAPE (%) e viés (MW), no SIN (soma dos submercados), por fonte, por
antecedência e por hora do dia. Semanas testadas: 172 (nível diário, mai/2023 a set/2026),
86 (30 min, jan/2025 a set/2026), 134 (deriva sazonal, exige 1 ano de histórico anterior).

Scripts: `src/backtest_renovaveis.py` (nível diário), `src/backtest_30min.py` (formato),
`src/backtest_sazonal.py` (sazonalidade e horizontes longos).

## 6. Resultados

**Nível diário, sábado a quinta (MAPE %):**

| Fonte | Persistência | Média 7 d | Média 14 d | Média 28 d | Sazonal aj. |
|---|---|---|---|---|---|
| UTE | **4,6** | 7,3 | 8,6 | 11,4 | 9,5 |
| PCH | **6,7** | 9,5 | 10,2 | 10,6 | 13,4 |
| CGH | **10,3** | 14,1 | 14,9 | 15,4 | 20,5 |
| UHE | **5,3** | 7,0 | 7,5 | 8,0 | 8,8 |
| MGD | 7,4 | 7,6 | **7,2** | 7,8 | 8,4 |

Quanto maior a janela da média, pior: ela atrasa a reação às mudanças de patamar.
O erro da persistência é estável até D+5 e só sobe em D+6 e D+7.

**Formato em 30 min (MAPE %):**

| Fonte | Persistir sexta | Nível × perfil 28 d | Perfil por tipo de dia | Persistir semana |
|---|---|---|---|---|
| UTE | **4,9** | 5,0 | 5,0 | 7,1 |
| PCH | **7,2** | 7,5 | 7,5 | 8,8 |
| CGH | **11,8** | 12,6 | 12,6 | 14,7 |
| UHE | **6,2** | 6,4 | 6,4 | 7,6 |
| MGD | **7,3** | 7,6 | 7,6 | 8,1 |

Copiar a sexta é ótimo até 16 h, mas o erro sobe muito após 17 h: o fim do D+0 de sexta
carrega ajustes de transição e não representa um dia típico. Nível × perfil erra bem menos
nesse trecho. Separar por tipo de dia não muda nada.

**Deriva sazonal (MAPE %, persistência vs persistência + deriva):**

| Horizonte | UTE | PCH | CGH | UHE | MGD |
|---|---|---|---|---|---|
| 1-7 dias | 4,7 vs 6,8 | 6,7 vs 11,0 | 10,8 vs 21,0 | 5,3 vs 7,0 | 7,2 vs 8,6 |
| 8-14 dias | 9,5 vs 11,4 | 10,8 vs 15,9 | 16,6 vs 30,5 | 9,0 vs 11,0 | 8,6 vs 10,6 |
| 15-28 dias | 14,6 vs 14,0 | 13,6 vs 17,6 | 20,2 vs 33,2 | 11,6 vs 12,3 | 10,1 vs 10,8 |

A sazonalidade do ano anterior **piora** em todos os horizontes até 14 dias, e muito nas
hidráulicas (hidrologia e parque mudam de um ano para outro). Só ajuda na UTE acima de 15 dias,
e apenas nos meses de virada de safra (janeiro e maio).

## 7. Regra vigente

Para cada fonte e submercado:

```
curva(dia, h) = nível × perfil(h)

nível     = média das 48 meias-horas do último dia COMPLETO coberto pela rodada mais recente
perfil(h) = média, nos últimos 28 dias de D+0, de [previsão(h) / média do dia]
```

- Vale para os 5 tipos de fonte, sem separação por dia da semana e sem correção sazonal.
- Dias parciais (o DESSEM termina às 00:00 do dia seguinte, que fica com um ponto) são
  descartados e também estendidos pelo perfil.
- Saída em 30 min, formato longo, com a coluna `origem` = DESSEM ou PERFIL.

Implementação: `src/gerar_renovaveis.py`. Custo: 1 consulta em lotes de 7 dias, ~10 s.

Erro esperado no horizonte de 7 dias (MAPE): UTE 5 %, PCH 7,5 %, CGH 12,5 %, UHE 6,5 %, MGD 7,5 %.
Em MW médios: UTE ~160, PCH ~150, CGH ~18, UHE ~56, MGD ~525 (SIN).

## 8. Limitações e próximos passos

- A referência é o D+0 do DESSEM, não o realizado. Quando houver realizado por fonte no
  mesmo recorte, o backtest deve ser refeito contra ele.
- A MGD é a fonte com maior erro absoluto (~500 MW) porque depende do tempo (nuvens). Um
  próximo passo é substituir o perfil da MGD por um modelo ligado à previsão de irradiância,
  ou escalá-la com a razão solar prevista/típica do deck UFV do ONS.
- Correção sazonal da UTE só vale a pena para horizontes acima de duas semanas; se esse
  horizonte passar a ser necessário, aplicar apenas nas viradas de safra.
- Reexecutar os backtests a cada semestre, pois o parque cresce (MGD) e a hidrologia muda.
