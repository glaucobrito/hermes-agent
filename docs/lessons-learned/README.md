# Dojo de Erros

Repositório de post-mortems operacionais do Hermes.

Objetivo: transformar erro repetível em regra reutilizável. Se uma falha já aconteceu uma vez e tinha causa evitável, ela precisa virar memória escrita.

## Quando registrar
- migration barrada por suposição errada;
- `max-turns` desperdiçado por exploração cega;
- trust gate / write approval / TUI interativa travando fluxo que poderia ser headless;
- drift entre contrato, migration, schema e código;
- bloqueio causado por leitura excessiva de contexto ou busca mal feita.

## Template mínimo
- Contexto
- Tentativa que falhou
- Causa raiz
- Regra operacional nova
- Evidência / arquivo afetado

## Regra
Se o erro custou tempo real e era evitável, registrar no mesmo bloco de trabalho. Depois esquecer é pedir replay do incidente.
