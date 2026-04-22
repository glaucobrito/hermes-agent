# OPERATING_POLICY.md

## Autonomia operacional

Estas regras definem a postura padrão de execução para metas estruturais.

### 1. Execução em lote e loop contínuo
Ao receber uma meta estrutural (ex.: finalizar uma sub-fase do Cogniflux), o agente deve:
- planejar o bloco completo de trabalho;
- executar em lote, sem pedir validação humana a cada micro-passo;
- rodar testes e comandos de verificação por conta própria;
- ler logs de erro, diagnosticar falhas e iterar no código continuamente;
- repetir o ciclo até a macro-tarefa ficar funcional e verificada, ou até encontrar bloqueio real de infraestrutura, permissão ou dependência externa.

### 2. Zero human-in-the-loop para validação intermediária
O agente não deve parar para pedir ao usuário revisão de arquivos intermediários, diffs parciais ou validações operacionais básicas.
O próprio agente deve:
- executar testes;
- validar comportamento;
- conferir consistência entre artefatos;
- só notificar o usuário quando a macro-tarefa estiver 100% concluída e testada, ou quando houver bloqueio irrecuperável sem ação local possível.

### 3. Foco em zero drift
O agente deve manter sincronia rigorosa entre código, contratos, migrations e banco de dados, sem depender de lembretes manuais do usuário.
Sempre que houver risco de drift, o agente deve:
- detectar o desvio;
- registrar a discrepância;
- corrigir ou propor correção concreta;
- verificar o estado final com evidência real.

### 4. Protocolo enxuto de exploração para tarefas de código
Para tarefas de codificação, seguir este protocolo por padrão:
- localizar o arquivo alvo com busca (`grep`/`rg`/`search_files`) antes de abrir arquivos inteiros;
- consultar `schema.sql`, migrations existentes ou contratos reais antes de propor SQL, repair migration ou ajuste de schema;
- limitar a exploração cega a 2 turns antes de pedir confirmação de escopo, desde que a dúvida realmente mude o alvo do patch;
- evitar leitura do repo inteiro quando o problema puder ser delimitado por arquivo, símbolo, traceback ou diff.

### 5. Pré-log obrigatório antes de alterar disco
Antes de qualquer `write_file`, criação de arquivo novo ou patch relevante, o agente deve registrar no log/resposta exatamente 3 bullets:
- o que será alterado;
- por que a alteração é necessária;
- por que a mudança não viola a regra de zero drift.

Esse pré-log é obrigatório. Código sem plano curto vira ruído caro.

### 6. Dojo de Erros e memória de post-mortem
Sempre que houver falha evitável, bloqueio do Codex/Claude, max-turns desperdiçado, migration rejeitada, suposição falsa de default ou retrabalho por exploração burra, o agente deve registrar a lição no Dojo de Erros.

Formato mínimo da lição:
- contexto;
- tentativa que falhou;
- causa raiz;
- regra operacional nova;
- evidência ou arquivo afetado.

Exemplo canônico:
> Tentativa de usar 60min fixos na migration 0034 falhou porque o Codex exige `duration_minutes` dinâmico. Regra: nunca assumir valores default em migrations de reparo.

### 7. Orquestração padrão para Claude Code
Quando usar Claude Code para execução de coding, o padrão deve ser headless primeiro:
- preferir `claude -p` para tarefas one-shot e automação;
- combinar `-p` com `--max-turns` e, quando útil, `--output-format json` para reduzir loops cegos e melhorar auditabilidade;
- usar TUI/tmux apenas quando houver necessidade real de multi-turn interativo;
- evitar caminhos que disparem trust gate ou write approval interativo quando o modo headless resolver o trabalho com menos atrito;
- não tratar travas de TUI como comportamento normal; se o headless resolve, o headless é o default.

### 8. Escalonamento permitido
Escalar ao usuário apenas quando houver:
- bloqueio de infraestrutura irrecuperável localmente;
- dependência externa sem acesso ou credencial necessária;
- decisão de negócio/escopo que mude materialmente o resultado;
- ação destrutiva ou sensível fora da autonomia concedida.

### 9. Limite explícito
Esta policy regula a autonomia operacional do agente, mas não substitui salvaguardas do runtime, flags de inicialização, políticas de ferramenta ou restrições de segurança do ambiente.
Se houver conflito entre esta policy e o runtime ativo, o runtime prevalece.
