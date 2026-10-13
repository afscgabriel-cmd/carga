# Passagem: análise de erro da previsão EÓLICA (a partir do estudo de carga)

Este documento é para a IA que vai conduzir a nova conversa. Ele resume o que foi feito para a **carga**, o que
deu certo e o que não deu, e o que muda ao aplicar o mesmo estudo à **previsão eólica**. Leia inteiro antes de
propor código. A conversa da carga está documentada em `docs/metodologia_erro_carga.md` e em `src/erro_carga.py`
(branch `claude/lucid-ramanujan-7ojiyh`, PR afscgabriel-cmd/carga#1).

---

## 1. Quem é o usuário e como trabalha

- **Contexto.** Trabalha com mercado de energia (Central Energia). Português do Brasil em tudo: textos, terminal,
  gráficos, comentários de código.
- **Ambiente.** Windows, Anaconda. Roda os scripts numa pasta própria, **sem git**:
  `C:\Users\afons\OneDrive - Central Energia\Carga\Projeto_Erro_Carga\` (para a eólica, provavelmente uma pasta
  irmã). Você entrega o arquivo `.py` pela conversa e ele substitui na pasta. Ele roda e cola a saída do terminal.
- **Banco.** PostgreSQL. A credencial vem de
  `C:\Users\afons\OneDrive - Central Energia\ATUALIZAR\database_config.json`, com as chaves `POSTGRES_USERNAME`,
  `POSTGRES_PASSWORD`, `postgres_host`, `postgres_port` e `postgres_database`. A função `engine_banco()` de
  `erro_carga.py` já faz isso. **Você não tem acesso ao banco**: teste com dados sintéticos realistas e diga
  claramente que os números do teste não são reais.
- **Script independente.** A pasta dele não tem os outros scripts do repositório. O script novo precisa rodar
  sozinho, com apenas `feriados_nacionais.csv` ao lado (como `erro_carga.py` faz).
- **Preferências observadas:**
  - **Valida a metodologia antes do código.** Discuta primeiro e liste as dúvidas.
  - **Questiona resultados que "não fazem sentido".** Ele tem razão com frequência. Investigue antes de defender.
  - **Terminal enxuto:** só o essencial e só SIN, com `--detalhado` para o resto. CSVs completos ficam em
    `output/`.
  - **Cenários em %.** Gráfico final em GW, **um gráfico só**, com a previsão da última rodada já corrigida pelos
    cenários e o valor escrito em cada ponto (no estilo do gráfico "Carga do SIN por dia" que ele usa).
  - **Explicações simples,** com exemplo numérico, para levar ao superior.
  - **Modo rápido.** Um modo que refaz o relatório a partir dos arquivos já gerados, sem ler o banco
    (`--so-relatorio`).

---

## 2. O que foi feito na carga (o modelo a seguir)

**Pergunta.** Quanto a previsão `prev_carga_dessem` (ONS, 1 rodada de manhã, 7 dias à frente) erra em relação à
carga **oficial** (deck DESSEM `delta = 0`, ajuste do operador publicado à tarde de D-1), por antecedência D+1..D+7?
E, com isso, como montar **cenários de sensibilidade** para os próximos dias?

**Pipeline final (`src/erro_carga.py`):**

1. **Leitura em lotes trimestrais** (evita timeout). Tudo vira média horária (hora cheia de início). SIN = soma dos
   4 subsistemas, só onde os 4 existem.
2. **Pareamento.** `erro = previsão (rodada R, para R+h) − oficial (dia R+h)`, h = 1..7. Dias com 24 h completas.
3. **Checagens que se pagaram:**
   - **Alinhamento:** MAE com o oficial deslocado ±1 h e ±1 dia; o mínimo tem de estar em 0.
   - **Origem do dado:** % de horas idênticas entre previsão e oficial. Revelou que o operador parte da previsão
     da manhã e que a tabela não foi sobrescrita.
   - **Cobertura:** dias por ano × mês. Revelou que a previsão só existe desde 24/10/2023.
   - **Piores dias:** com o erro de cada subsistema ao lado. Os erros "absurdos" de 23 GW eram feriados previstos
     a 7 dias, não dado ruim.
   - **Limpeza:** zeros, valores implausíveis, duplicidades, e um `dias_excluidos.csv` manual.
4. **Diagnóstico.** Viés, erro médio (MAE), erro mediano, desvio e quantis por antecedência, mês, tipo de dia e
   hora, mais o IC 95 % do viés por bootstrap em blocos semanais.
5. **Cenários: método "padronizado + nível recente", o escolhido.**
   - Unidade: `r = 100 × (oficial / previsão − 1)`; `carga do cenário = previsão × (1 + r/100)`.
   - **Centro e largura da época:** mediana e IQR/1,349 de r nas rodadas a ±30 dias da data, em todos os anos,
     suavizados em h.
   - **Formato das caudas:** quantis de `z = (r − centro)/largura`, com todo o histórico (D+2..D+7 juntos).
   - **Nível recente:** deslocamento e escala de z nos últimos 365 dias, em todas as épocas. Isso preserva a
     sazonalidade (que vem de todos os anos) e capta a melhora do modelo.
   - **D+1:** modelo próprio (chance de o operador ajustar × tamanho do ajuste), sem nível recente.
   - **Semana:** quantis do erro médio da semana (W), mais o caminho dia a dia `r_h = centro_h + β_h (W − centro_W)`.
6. **Calibração fora da amostra.** Cada ano de teste é estimado só com os anos anteriores. Mede a cobertura de
   P10–P90 (meta 80 %), as caudas (10 % cada) e o **pinball** (perda quantílica; menor é melhor). Compara três
   métodos: empírico ±15 d, padronizado e padronizado + recente.
7. **Saída prática.** Tabela e gráfico em GW da última rodada corrigida pelos cenários (alto = P90 na média e
   P95 na ponta).

**Resultados da carga, para referência:**
- O viés é negativo e significativo: o modelo subestima, de −0,2 GW em D+1 a −0,9 GW em D+7 na média do SIN.
- Erro médio de 0,6 a 2,5 GW.
- Erro assimétrico: a cauda de "oficial acima" é maior.
- Calibração: 86 % dentro de P10–P90.
- A ponta de 2026 passou a ser mais subestimada, por isso o alto da ponta passou a ser o P95.

---

## 3. Lições da carga: não repetir

| Erro cometido | O que fazer |
|---|---|
| Cenário semanal com **semanas históricas isoladas** (e depois média de 2–3 semanas por faixa). Deu caminhos incoerentes, como estresse alto com D+7 a +1 %. | Com poucos anos, não sortear trajetórias. Usar quantis da média semanal com o caminho condicional (β por h). |
| Quantis de **mês fechado** ou de janela curta: P5/P95 com uns 3 pontos, saltos entre antecedências. | Separar o formato das caudas (histórico todo, padronizado) do centro e da largura da época (janela de ±30 d). |
| **Nível recente aplicado ao D+1** (comportamento do operador) piorou a calibração. | Não aplicar o nível recente a componentes que não são erro do modelo. |
| Calibração contando diferenças de poucos MW como "fora da faixa" quando o quantil era 0. | Tratar "sem ajuste" (\|dif\| < 10 MW) como zero exato. |
| **Sinal confuso:** começou com `erro = previsto − oficial` e os cenários com P10 = alto. O usuário se confundiu. | Definir desde o início `r = oficial/previsão − 1` (positivo = acima da previsão) e P90 = alto. Escrever o sinal em toda tabela. |
| Pareamento por delta calculado em vez da coluna `delta` da tabela, e mistura de decks quando faltava delta 0. | Usar as colunas da própria tabela; checar e avisar divergências; não misturar rodadas. |
| `DataFrame` guardado em `df.attrs` quebrou o `concat` do pandas. | Nunca guardar objetos pesados em `attrs`; devolver tuplas. |
| **Windows:** `dt.dayofyear` sai em int32 e `np.arange` em int64; o `merge` quebra só no Windows. | `astype("int64")` em toda chave de junção. Não dá para reproduzir no Linux, então force sempre. |
| `global X` declarado depois de usar X dentro de `main()` dá `SyntaxError`. | Declarar `global` na primeira linha da função. |
| Terminal com mais de 400 linhas, por subsistema. | Terminal enxuto (SIN) + `--detalhado` + CSVs. |
| Gráfico com rótulos sobrepostos. | Olhar o PNG antes de entregar; rotular só os pontos que importam. |

---

## 4. O que muda na eólica (o essencial desta passagem)

### 4.1 Dados já conhecidos no repositório

| Papel | Tabela | O que se sabe |
|---|---|---|
| **Previsão** | `fac_tempook_geracao_eolica_hourly` | Fornecedor **Tempo OK** (não ONS). Colunas: `rodada_dia`, `valido_para`, `valido_para_dia`, `valido_para_hora`, `geracao`, `cd_subsistema`, `mnemonico_subsistema`, `nome_subsistema`. **`geracao` em GW** (`EOLICA_EM_GW = True` em `carga_liquida.py`). Rodadas de D+0 a D+9. Resolução a confirmar (30 min ou horária). `src/inspecionar_eolica.py` levantou a suspeita de que a tabela traz **dias já passados** (antecedência negativa, possível consolidado): confirmar e excluir. |
| **Realizado** | `fac_ons_geracao_eolica` | Colunas `subsistema, dia, hora, carga` (o nome da coluna é "carga", mas é geração eólica em MW), rótulo de início da hora. Lido por `ler_realizado()` (descarta valores > 60 GW). |
| **Programado ONS** | `fac_ons_renovaveis`, `tipo_fonte_energia = 'UEE'` | Deck DESSEM: `rodada_dia`, `valido_para` (30 min), `submercado`, `previsao` (MW), `rev`. D+0 de cada rodada = programado do dia. |
| **Corte de geração** | Dados Abertos ONS `RESTRICAO_COFF_EOLICA_AAAA_MM` (arquivo, não está no banco) | `src/coff_eolica.py` lê: `val_geracao` (verificada), `val_geracaoreferencia` (sem corte), `val_geracaonaorealizadaapurada` (frustrada) por razão: REL (elétrica, constrained-off), CNF (confiabilidade), ENE (energética), PAR. |

Já existe `src/validar_eolica.py`, uma validação rápida da Tempo OK contra o realizado: MAE e viés por
antecedência e hora, teste de deslocamento ±2 h, razão realizado/previsto por hora, que mostra a assinatura de
corte. Ele é um bom ponto de partida para a fase de checagem.

### 4.2 Diferenças que mudam a metodologia

1. **Qual é a "verdade" (a decisão nº 1, a tomar com o usuário).** Na carga era o oficial (delta 0). Na eólica há
   três candidatos:
   - **Realizado** (`fac_ons_geracao_eolica`): a geração **injetada**, já com os cortes do ONS. Mede "quanto vai
     entrar de fato".
   - **Geração de referência sem corte** (COFF `val_geracaoreferencia`, ou realizado + frustrada): mede o erro
     **meteorológico** da previsão, que é o que o fornecedor controla.
   - **Programado DESSEM UEE delta 0**: equivale ao "oficial" da carga, se a pergunta for "quanto a Tempo OK erra
     em relação ao que o ONS programa".

   No repositório já foi visto que o realizado tem **platôs de corte** (cerca de 12 GW nos dias úteis de
   set/2026, constrained-off por escoamento do NE) e **vales de fim de semana** (corte energético, com razão
   realizado/previsto de 0,23 a 0,60). Comparar previsão com realizado sem tratar o corte mistura erro
   meteorológico com decisão do operador. Sugestão: fazer as duas medidas e separar
   `previsão − realizado = erro meteorológico + energia frustrada`, como foi feito na carga com
   oficial × realizado.

2. **Unidade do cenário: % sobre a previsão NÃO funciona bem.** A eólica vai a perto de zero (horas e dias de
   calmaria), então `oficial/previsão − 1` explode. Alternativas, por ordem de preferência:
   - erro em **MW normalizado pela capacidade instalada** (fator de capacidade), se houver a série de capacidade;
   - erro em **MW**, condicionado ao **nível previsto** (faixas da previsão);
   - % só para agregados grandes (média diária do NE ou do SIN) e em épocas de vento.

   Discuta com o usuário, que gosta de % mas vai entender o problema.

3. **O erro depende do nível previsto.** Ele é limitado por 0 e pela capacidade, e é maior em geração
   intermediária. Os cenários devem ser condicionados a **antecedência × época × faixa de geração prevista**, não
   só a antecedência × época. O método padronizado (centro e largura locais, formato global) se adapta bem:
   basta incluir a faixa prevista no "local".

4. **Sazonalidade forte e diferente da carga.** O NE tem a safra dos ventos (aprox. jul–nov) e a baixa em fev–abr.
   O perfil diário também conta, com pico noturno no NE. O nível recente precisa ser medido em z (sem
   sazonalidade), como na carga, para não confundir estação com melhora do modelo.

5. **Crescimento do parque.** A capacidade cresce ano a ano, e um erro em MW de 2024 não é comparável com 2026.
   Normalize pela capacidade ou por uma média móvel da geração. Pergunte se existe série de capacidade
   instalada por subsistema.

6. **Subsistemas.** O NE domina, o S vem em seguida, o N é pequeno e o SE é quase zero. O SIN é a soma.
   Concentre o terminal no SIN e no NE.

7. **Antecedência maior.** A Tempo OK vai até D+9. Confirme o horário da rodada e qual rodada estaria disponível
   na hora da decisão.

8. **Resolução horária importa mais.** O que pesa para a carga líquida é a rampa e as horas da ponta (17–20 h).
   Faça o recorte horário (perfil) com cuidado. Média diária e uma métrica de horário de ponta (por exemplo,
   18–20 h) provavelmente são mais úteis que o "máximo diário".

9. **Correlação mais forte entre dias.** Os regimes de vento duram dias, então o erro da semana é mais persistente.
   O bootstrap em blocos e o caminho condicional semanal ficam ainda mais importantes.

10. **Não há "operador" no meio.** Diferente da carga, a Tempo OK não tem ajuste manual do ONS. O tratamento
    especial do D+1 provavelmente não se aplica. Se a verdade for o programado DESSEM, aí sim existe um ajuste do
    ONS, e vale testar.

---

## 5. Plano sugerido

**Fase 0 – alinhar com o usuário, antes de qualquer código:**
- Qual é a verdade: injetada, sem corte ou programado DESSEM? (Sugira medir injetada e sem corte.)
- O arquivo COFF está disponível para todo o período, ou só alguns meses?
- Existe série de capacidade instalada por subsistema?
- Horário da rodada da Tempo OK e uso prático: carga líquida? PLD? Horas de ponta?
- Unidade dos cenários (MW, % da capacidade, % da previsão) e recortes (média diária, horas da ponta).

**Fase 1 – dados e checagens** (reaproveitar as funções de `erro_carga.py`):
- Unidades (GW → MW), resolução, rótulo do horário (deslocamento ±1 h; `validar_eolica.py` já testa).
- Linhas de "consolidado" com antecedência negativa: excluir.
- Duplicidade de rodadas no mesmo dia: usar a última.
- Cobertura ano × mês, e piores dias com abertura por subsistema.
- Corte: platôs e vales; cruzar com o COFF.

**Fase 2 – diagnóstico:** viés, MAE e quantis por antecedência (D+1..D+9), mês, faixa de geração prevista e hora,
com IC por bootstrap semanal.

**Fase 3 – cenários:** método padronizado (centro e largura da época × faixa prevista; formato das caudas do
histórico; nível recente em z), cenário semanal pelo caminho condicional e saída em MW ou % da capacidade.

**Fase 4 – calibração fora da amostra,** com cobertura, caudas e pinball, comparando com o empírico simples.
Só adotar o método que ganhar.

**Fase 5 – saída prática:**
- Terminal enxuto.
- Gráfico em GW da última rodada corrigida, com faixa baixo–alto e o valor em cada ponto (SIN e NE).
- Um documento curto para o superior.

**Arquitetura sugerida:** um `src/erro_eolica.py` independente, copiando de `erro_carga.py`:
- leitura em lotes, `_horario`, `_com_sin`, limpeza, `excluir_dias`, `checar_alinhamento`, `cobertura`;
- `resumir` com bootstrap;
- o bloco de cenários (`tabela_epoca`, `forma_caudas`, `_nivel`, `ajustar_modelo`, `quantis_dia`,
  `cenarios_semana`);
- `calibracao`, `_calibracao_curta`, `grafico_cenarios_gw`;
- as opções `--so-relatorio`, `--detalhado`, `--janela`, `--dias-recentes` e `--sem-nivel-recente`.

Adapte a unidade (item 4.2.2) e acrescente a dimensão "faixa de geração prevista" (item 4.2.3).

---

## 6. Como explicar ao usuário (já validado na carga)

- **Cenários:** "olhamos como o modelo errou no passado, na mesma época e com a mesma antecedência, e aplicamos
  esses erros à previsão de hoje". Central = erro típico; alto e baixo = só 1 em cada 10 vezes passou disso.
- **Pinball:** uma nota de acerto dos cenários. Penaliza muito quando a geração passa do alto ou fica abaixo do
  baixo, e pouco quando a faixa é larga à toa. Menor é melhor; serve para comparar métodos.
- **Calibração:** testar "como se estivéssemos no passado". A faixa de 80 % tem de conter cerca de 80 % dos dias.
- **Não use jargão** (z, IQR, quantil padronizado) sem tradução, e dê sempre um exemplo em GW.
