# CLAUDE.md — Hermes Agent

> Instruções específicas do projeto `hermes-agent` para agentes Claude. Complementa (não substitui) `/root/.claude/CLAUDE.md` global e `AGENTS.md` deste repo.

## Stage Gate Protocol — OBRIGATÓRIO

Este projeto adotou em **2026-05-23** o Stage Gate Protocol (Higher Mind × Hermes) com fluxo de 12 etapas. Antes de QUALQUER trabalho substantivo:

1. **Ler `PHASE.md` na raiz** (`/root/.hermes/hermes-agent/PHASE.md`)
2. Respeitar `current_stage` e critérios de saída
3. Avançar SEMPRE via `/next-stage` (nunca editar PHASE.md manualmente para "pular etapa")
4. Skills/agentes obrigatórios por etapa estão na tabela de PHASE.md

Se feature toca apenas sub-projeto (`ui-tui/`, `website/`, `plugins/<x>/`, `gateway/platforms/<y>/`), criar PHASE.md filho no sub-path e fazer o gate isolado.

## Classificação automática para este repo

`AGENTS.md` lista entry points load-bearing — qualquer mudança neles é **`full`** automaticamente:

- `run_agent.py` (AIAgent core loop)
- `model_tools.py` (tool orchestration)
- `toolsets.py` (_HERMES_CORE_TOOLS)
- `cli.py` (HermesCLI orchestrator)
- `hermes_state.py` (SessionDB / FTS5)
- `hermes_constants.py` / `hermes_logging.py`
- `gateway/run.py`, `gateway/session.py`, `gateway/platforms/*`
- `agent/` (provider adapters, memory, caching, compression)
- `cron/scheduler.py`

`lite` cabe em: mudanças em 1 plugin isolado, doc no `website/`, ajuste em 1 script de `scripts/`, fix em 1 teste já existente.

`trivial`: README/docs simples, typo, comment-only changes.

## LLM Guardrails — obrigatório para qualquer feature no caminho de inferência

`uses_llm: true` no PHASE.md. Toda feature que toca:
- `run_agent.py` (chamadas LLM, tool_call loop)
- `agent/providers/*` (adapters de provider)
- `agent/memory/*` (context que vai para prompt)
- `agent/caching.py`, `agent/compression.py`
- `model_tools.py` (function calling)
- `trajectory_compressor.py`

passa pela etapa `llm-guardrails` antes de `qa`. 14 patterns do `hm-llm-guardrails` aplicáveis especialmente:
- **Pattern 1 (sliding window):** AIAgent já tem `trajectory_compressor.py` — validar limite real vs ideal
- **Pattern 2 (lazy client factory):** providers em `agent/providers/` — não instanciar no import
- **Pattern 5 (streaming abort):** TUI/CLI precisa cancelar mid-stream limpo
- **Pattern 7 (schema validation):** tool_call responses validadas antes de execução
- **Pattern 8 (token budget):** context budget explícito por turn
- **Pattern 10 (cost tracking):** por sessão, por provider, por usuário (multi-tenant via gateway)
- **Pattern 11 (multi-provider failover):** já existe em design — validar fallback chain
- **Pattern 13 (identidade não-mesclável):** sessions têm escopo `platform+user_id` — features que cruzam contextos exigem revisão de bypass

## Supply chain — regra de pin absoluta

`pyproject.toml` usa **exact-pin (==)** em toda direct dep. Reação ao Mini Shai-Hulud worm de 2026-05-12 (mistralai 2.4.6 comprometida).

**Toda adição/upgrade de dep direta requer:**
1. Bump em `pyproject.toml` com versão exata (sem `>=`, `~=`, `<`)
2. `uv lock` regenerado (lockfile atualizado no mesmo commit)
3. Justificativa em commit body (por que essa versão, qual problema resolve)
4. **Scope check:** se dep é provider-specific (anthropic, exa-py, fal-client, etc.), vai em `extras` + lazy load via `tools/lazy_deps.py`, NUNCA em `dependencies` core

`hm-security` reprova `audit` se essa regra for quebrada.

## Testing

- `scripts/run_tests.sh` — runner padrão (probe `.venv` → `venv` → `~/.hermes/hermes-agent/venv`)
- `tests/` estruturado por área: `acp/`, `agent/`, `cli/`, `cron/`, `e2e/`, `environments/`
- **TDD obrigatório** para mudanças em entry points load-bearing
- Teste novo deve falhar antes do código existir (red → green → refactor)

## Sub-projetos com lifecycle próprio

| Path | Stack | Notas |
|---|---|---|
| `ui-tui/` | TypeScript + Ink (React for terminal) | npm/pnpm, package.json próprio. Feature TUI ⇒ PHASE.md filho. |
| `website/` | Docusaurus (Node) | Build separado. Docs change ⇒ `lite` direto, sem gate completo. |
| `acp_adapter/` | Python ACP server | LSP-like protocol para VS Code/Zed/JetBrains. |
| `tui_gateway/` | Python JSON-RPC | Backend do TUI. Mudanças aqui afetam ui-tui ⇒ testar pareado. |
| `gateway/platforms/<plat>/` | Python adapter | Cada plataforma é isolada. Adicionar nova: ver `ADDING_A_PLATFORM.md`. |
| `plugins/<plugin>/` | Plugin system | Isolado por convenção. PHASE.md filho recomendado se +5 arquivos. |
| `cron/` | Job scheduler | Mudança aqui ⇒ obrigatório `hm-data-integrity` em audit. |

## Não-objetivos deste CLAUDE.md

- Substituir AGENTS.md (que tem o detalhe técnico de cada módulo) — este arquivo é sobre **processo/protocol**, AGENTS.md é sobre **estrutura/onde editar**
- Listar skills HM individualmente — referência canônica em `/root/.claude/CLAUDE.md` + `~/.hermes/docs/skill-evaluation/2026-05-23-hm-vs-hermes-skills.md`
- Repetir regras globais (Definition of Done, anti-padrões, etc.) — apenas adicionar regras específicas do projeto
