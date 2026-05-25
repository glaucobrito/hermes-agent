# PHASE.md — Hermes Agent

> Arquivo de estado do fluxo Higher Mind + Hermes. Lido pelo agente em toda sessão. Atualizado via `/next-stage`.

## Identificação

- **Projeto:** Hermes Agent (Nous Research)
- **Feature/Sprint:** _aguardando declaração de Glauco_ — próximo trabalho substantivo (após xAI migration 2026-05-15)
- **Owner:** Glauco Brito
- **Iniciado em:** 2026-05-23
- **Última atualização:** 2026-05-23
- **Repo:** /root/.hermes/hermes-agent
- **Stack:** Python 3.11 + Fire CLI + SQLite/FTS5 + Docusaurus + Ink (React TUI) + multi-gateway (Telegram/Discord/Slack/WhatsApp/Signal/...) + plugin system
- **LLM providers:** OpenAI, Anthropic, OpenRouter, GMI, xAI, Nous Portal, NovitaAI, NVIDIA NIM, Xiaomi MiMo, z.ai/GLM, Kimi/Moonshot, MiniMax, HF (uso pervasivo)

## Etapa atual

`current_stage: align`

`uses_llm: true` — agente conversacional com LLM em loop central (`run_agent.py` AIAgent class ~12k LOC). `llm-guardrails` é OBRIGATÓRIO antes de `qa` em qualquer feature que toque o caminho de inferência.

## Critérios de saída da etapa atual (`align`)

Para avançar para `init`, Glauco precisa:

- [ ] Declarar feature/sprint concreta (1 frase + escopo)
- [ ] Definir não-objetivos explícitos (o que NÃO entra)
- [ ] Definir critério de pronto observável
- [ ] Confirmar se feature é greenfield (novo módulo) ou refactor/extensão (toca código existente)

Sem isso, agente recusa transição.

## Próxima etapa

`next_stage: init`

Skills/agentes obrigatórios na próxima etapa: `hm-init` (executor — gera ARCHITECTURE.md da feature) + `ai-engineering-pipeline-full` (gate de qualidade do plan + checkpoint git).

## Histórico de transições

| Data | De | Para | Quem aprovou | Artefato gerado |
|---|---|---|---|---|
| 2026-05-23 | — | align | Glauco (adoção Stage Gate Protocol em hermes-agent) | PHASE.md criado |

## Reprovações / back-tracks

| Data | De | Voltou para | Motivo | Artefato relevante |
|---|---|---|---|---|
| — | — | — | — | — |

## Contexto adicional para o agente

- **Maturidade:** Projeto v0.14.0 em produção, multi-plataforma. NÃO é greenfield — escopo de feature deve respeitar arquitetura existente (plugins, gateway adapters, toolsets, registry).
- **Supply chain hardening:** `pyproject.toml` usa exact-pin (== sem ranges) em toda direct dep. Reação ao Mini Shai-Hulud worm de 2026-05-12. Qualquer adição de dep precisa de regenerar `uv.lock` e justificativa escrita.
- **AGENTS.md** lista entry points load-bearing (run_agent.py, model_tools.py, toolsets.py, cli.py, hermes_state.py, gateway/, plugins/). Mudanças nesses arquivos = automaticamente `full`.
- **Distribuição:** Container (Dockerfile + docker-compose), Serverless (Modal, Daytona, Vercel Sandbox), Local (Linux/macOS/WSL2/Termux/Windows), Library (Python). 6 modelos cobertos pelo DOMÍNIO de `hm-deploy`.
- **Identidade não-mesclável:** Sessions têm escopo por plataforma+usuário (`hermes_state.py` SessionDB). Pattern 13 de `hm-llm-guardrails` aplica-se direto — features que cruzam contextos de sessão precisam de revisão extra.
- **Sub-projetos com lifecycle próprio:** `ui-tui/` (React/Ink), `acp_adapter/` (LSP-like), `website/` (Docusaurus), `gateway/platforms/*`, `plugins/*` — cada um pode ter PHASE.md filho próprio se a feature for grande.
- **Branch atual:** `main` (clean). Toda feature `full` cria branch dedicada + worktree isolada.

## Referência do fluxo (12 etapas)

Ver `~/.hermes/brain/resources/templates/PHASE_TEMPLATE.md` para tabela completa de skills por etapa.

Mapping rápido (mix HM + Hermes — vencedor da avaliação 2026-05-23):

| # | Etapa | Skills/agentes |
|---|---|---|
| 1 | `align` | (Glauco decide) |
| 2 | `init` | `hm-init` + `ai-engineering-pipeline-full` |
| 3 | `spec` | `writing-plans` + `agent-spec-flow-analyzer` |
| 4 | `design` | `ui-ux-pro-max` → `hm-designer` (se feature tem UI: TUI/web) |
| 5 | `implement` | `test-driven-development` + `implement-feature` |
| 6 | `audit` | `hm-engineer` + `hm-security` + `hm-performance` + `hm-data-integrity` |
| 7 | `llm-guardrails` | `hm-llm-guardrails` (OBRIGATÓRIO se toca AIAgent/model_tools/agent/) |
| 8 | `qa` | `hm-qa` + `qa-tester` |
| 9 | `ux-flow` | `hm-ux-flow` (CLI/TUI flows + gateway message flows) |
| 10 | `pre-ship` | `hm-validate-all` (versão Hermes) |
| 11 | `deploy` | `hm-deploy` Security Gate + `devops-release` |
| 12 | `post-deploy` | `devops-release` monitoring (15 min) |

## Regras duras

- Não pular etapas sem registro de back-track explícito do Glauco
- Cada etapa só avança via `/next-stage`
- Se audit reprovar com CRÍTICO, volta automaticamente para `implement`
- `llm-guardrails` é OBRIGATÓRIO entre `audit` e `qa` (uses_llm: true)
- `done` só com Glauco declarando product-ready
- **Sub-projeto rule:** Se feature toca apenas `ui-tui/`, `website/`, ou um único `plugins/<x>/`, criar PHASE.md filho em `<subpath>/PHASE.md` e fazer o gate isolado.
- **Dependency add:** Toda nova dep direta requer atualização de `pyproject.toml` com exact-pin + `uv lock` + justificativa em commit body. Sem isso, `hm-security` reprova em `audit`.
