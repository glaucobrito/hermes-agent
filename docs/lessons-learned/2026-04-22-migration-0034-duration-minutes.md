# 2026-04-22 — Migration 0034 não aceita `duration_minutes` fixo

## Contexto
Reparo de migration envolvendo `duration_minutes` em fluxo onde o agente tentou assumir um valor default para destravar a execução.

## Tentativa que falhou
Usar `60` minutos fixos na migration 0034 como fallback implícito.

## Causa raiz
A suposição de default não era suportada pelo contrato real do reparo. O validador/Codex exigia `duration_minutes` dinâmico, derivado da fonte correta, e barrou a solução simplista.

## Regra operacional nova
Nunca assumir valores default em migrations de reparo. Antes de propor SQL ou patch de migration:
1. ler o schema/contrato real;
2. validar como o campo é calculado no sistema atual;
3. só então escrever a migration.

## Evidência / arquivo afetado
Caso de referência citado no Dojo de Erros e incorporado ao `OPERATING_POLICY.md` como exemplo canônico de post-mortem.
