# Licenças — isolamento do conector: U1 (guarda sem segredo, proteção independente de `enabled`)

<!-- #atende:SPEC-licencas-acompanhamento/REQ-R4 REQ-R5 REQ-R8 -->

Fonte: `DELTA-ISOLAMENTO-2026-11-28.md` ("Design / layer doctrine" e "U1 — contrato detalhado antes código").
Base preservada: A2-client (`4d66f2834`) + A2-pregate não commitado (`docs/plans/licencas-gateway-a2-pregate-bdd.md`).
Esta unidade **substitui** as partes do A2-pregate que liam segredo na guarda (ver "O que muda" e a nota
no topo do BDD anterior). Não implementa U2–U10.

## Escopo e arquivos

| Arquivo | Papel em U1 |
|---|---|
| `gateway/licencas_followup.py` | Guarda sem segredo: remove leitura de `credentials.env`, `client_config`, `read_env` e a validação das nove chaves do consumidor; acrescenta manifesto + sinal de adoção; `enabled` deixa de decidir a proteção |
| `tests/gateway/test_whatsapp_group_gating.py` | Testes negativos primeiro (RED), depois o restante ajustado ao contrato novo; nenhum teste valida o comportamento antigo só para manter contagem |
| `hermes_cli/config.py` | Só a seção `licencas_followup` e os comentários dos dois nomes reservados: `enabled` é do consumidor; a guarda não lê segredo |
| `docs/plans/licencas-isolation-bdd.md` | Este documento |
| `docs/plans/licencas-gateway-a2-pregate-bdd.md` | Exceção documental: instruções inseguras antigas removidas (arquivo de segredos criado à mão, `LicencasClient(guard.client_config)`, `READY` como porta do consumidor) |

`gateway/run.py`, `gateway/platforms/whatsapp.py` e `tools/send_message_tool.py` **não mudam**: só chamam
`current_guard()`, `blocks_event`, `blocks_bridge_message` e `blocks_outbound`, cuja assinatura não muda.

**Por que não há `gateway/licencas_guard_manifest.py`.** A leitura do manifesto e do sinal de adoção são
duas funções pequenas (≈80 linhas) que só a guarda usa e que já dependem de `_base_home()`. Um módulo novo
exigiria expor `_base_home` ou duplicá-lo. Fica em `licencas_followup.py`; se U2 (configuração do conector)
precisar reler o manifesto, extrair então.

## Contrato

### Manifesto técnico (não secreto, independente do consumidor)

`<HERMES_HOME do perfil base>/licencas/protected-groups.json`:

```json
{"version": 1, "groups": ["<dígitos>@g.us"]}
```

- Formato estrito: exatamente as chaves `version` (inteiro `1`, não booleano) e `groups`; chave duplicada,
  chave extra, JSON inválido, não UTF-8, mais de 4096 bytes: inválido.
- `groups`: de 1 a 8 JIDs de grupo na forma canônica (`<dígitos>[-<dígitos>]@g.us`, minúsculo, sem espaço,
  sem sufixo de dispositivo), sem repetição. Piloto = um. Nenhum dado de negócio, nenhum segredo, nenhum ID
  de produção no código.
- Lido do perfil **base** do processo (nunca do override de perfil por contexto do cron).

### Sinal de adoção persistente

`<HERMES_HOME base>/licencas/adopted` com exatamente `licencas-guard-adopted v1\n`. Criado uma vez pela
instalação (U10) e **nunca removido pelo rollback**. Adoção vale se **o marcador OU o manifesto** existir:
apagar só um dos dois não solta nada.

Confiança dos arquivos (checada no descritor aberto com `O_NOFOLLOW`): diretório `licencas/` real, do
usuário do processo, sem escrita para grupo/outros; arquivos regulares, do usuário do processo, sem link
simbólico nem físico, sem escrita para grupo/outros.

### Estados

| Situação | Estado | Efeito |
|---|---|---|
| Não adotado (sem diretório `licencas/`, ou sem marcador e sem manifesto) **e** sem seção `licencas_followup` | `DISABLED` | Instalação existente sem integração, igual a hoje |
| Adotado, manifesto íntegro | `PROTECTED` | Só os grupos do manifesto (mais o `group_jid` da seção, se identificável) são barrados. `enabled`, segredo ausente, consumidor parado, reinício, `config.yaml` ilegível/ambíguo/sem a seção **não** soltam nem ampliam: config ruim vira `config_error` mas só fecha o piloto; outros grupos e DMs seguem |
| Adotado, manifesto ausente, ilegível, não confiável ou malformado; marcador inválido; diretório ilegível | `CONFIG_ERROR`, todos os grupos | Alvo irrecuperável: **todo** grupo de WhatsApp (`@g.us`) é barrado; DMs seguem. Ausente após adoção **não** libera |
| Seção com `enabled: true` e `group_jid` de grupo identificável, sem manifesto | `PROTECTED` | Compatibilidade A2-pregate: só esse grupo |
| Seção com `enabled: false` e `group_jid` de grupo válido, sem manifesto | `PROTECTED` | **Mudança**: `enabled=false` nunca remove proteção |
| Seção com `enabled: false` e `group_jid` ausente, nulo ou em branco, sem manifesto | `DISABLED` | Seção de defaults do `DEFAULT_CONFIG` não adota nada |
| Seção com `enabled: false` e `group_jid` não vazio que não identifica um grupo, sem manifesto | `CONFIG_ERROR`, todos os grupos | Intenção explícita ilegível: fecha, não adivinha |
| `enabled` não booleano, seção que não é mapa, `enabled: true` sem grupo identificável, `config.yaml` ilegível/ambíguo, raiz que não é mapa (sem manifesto) | `CONFIG_ERROR` | Como no A2-pregate: grupo identificável, senão todos os grupos |
| Chave de segredo na seção (`capability`, `hmac_key`, `hmac_key_b64`, `licencas_capability`, `licencas_hmac_key_b64`) | `CONFIG_ERROR`, `config_error=secret_in_config` | O grupo identificável continua barrado; só o **nome** da chave é conferido, o valor nunca é lido nem logado |

`config_error` é sempre nome de chave ou código (`manifest_missing`, `manifest_format`, `adoption_file`,
`adoption_dir`, `adoption_state`, `adoption_marker`, `group_jid`, `enabled`, `secret_in_config`,
`config_root`, `guard_load_error`), nunca valor.

### O que a guarda não faz mais

- Não abre `credentials.env` nem nenhum arquivo de segredo; não carrega capability/HMAC.
- Não importa nem instancia `gateway.licencas_client`; `ProtectedGroupGuard` não tem `client_config`.
- Não lê `os.environ` (nem os nomes de segredo, nem os valores não secretos do cliente): conferir o
  consumidor passa a ser trabalho do conector (U2). Não valida as nove chaves do consumidor.
- `ProtectedGroupGuard.groups` (conjunto) + `all_groups` substituem `group_jid`; `GuardState.READY` vira
  `PROTECTED` (a guarda não diz mais "consumidor pronto").

### Limites (não escondidos)

- **G5-root universal permanece aberto.** Manifesto e marcador são arquivos do mesmo usuário do gateway:
  qualquer processo com esse usuário, e root administrativo, pode apagá-los ou editá-los e vale a partir do
  próximo reinício. Nenhum teste aqui prova o contrário; root-admin está fora da garantia local.
- A guarda é lida uma vez por processo; mudar manifesto exige reiniciar o gateway.
- Windows: sem `O_NOFOLLOW`/`geteuid`, uma instalação com diretório `licencas/` falha fechada (todos os
  grupos); sem o diretório, segue como `DISABLED`.
- O manifesto não é ainda escrito por ninguém: quem o instala e cria o marcador é U10. Até lá, só o
  fallback de configuração protege.

Criação (operador, sem segredo): `umask 077; mkdir -p "$HERMES_HOME/licencas"` e gravar os dois arquivos
com o conteúdo acima. Não passar por `.env`.

## Rastreabilidade requisito → teste (`tests/gateway/test_whatsapp_group_gating.py`)

| Requisito do delta | Teste |
|---|---|
| `enabled=false`, consumidor parado, segredo ausente, reinício: piloto continua barrado | `test_manifest_keeps_the_pilot_barred_while_the_consumer_is_off` (3 variações × reinício) |
| `enabled=false` + `group_jid` válido sem manifesto continua barrando | `test_disabled_section_with_a_valid_group_still_bars_it` |
| `enabled=false` sem grupo = integração ausente; grupo ilegível fecha | `test_disabled_section_without_a_group_stays_legacy`, `test_disabled_section_with_an_unreadable_group_fails_closed` |
| /reset, anexo, spoof `internal`, entrada ocupada, perfil cron: zero LLM/ferramenta/sessão/legado no piloto sob manifesto | os testes `BLOCKING_CONFIGS` existentes (adapter, runner, ocupado, envios, cron) agora também com `so_manifesto`, `manifesto_e_consumidor_desligado`, `manifesto_e_config_ilegivel` |
| Manifesto bom + config do consumidor ruim não bloqueia outros grupos nem DM | `test_good_manifest_with_a_broken_consumer_config_bars_only_the_pilot` |
| Manifesto adotado ausente/inválido/não confiável falha fechado e não libera o piloto; DM preserva | `test_adopted_install_without_a_usable_manifest_bars_every_group`, `test_malformed_manifest_bars_every_group`, `test_untrusted_manifest_files_bar_every_group` |
| Apagar só o marcador ou só o manifesto não solta | `test_adoption_by_either_file_alone_keeps_the_pilot_barred` |
| Sem adoção e sem seção: instalação existente intacta, mesmo com `licencas/credentials.env` | `test_not_adopted_install_stays_on_the_legacy_gateway` |
| Perfil/cron não troca guarda base | `test_manifest_comes_from_the_base_profile_only` |
| Guarda/log/env/filhos não recebem segredo; proibir open/read de credentials | `test_guard_never_opens_a_credentials_file`, `test_guard_module_has_no_secret_surface`, `test_guard_does_not_import_the_client_module`, `test_secret_key_in_the_consumer_section_fails_without_revealing_it`, `test_secrets_in_the_environment_do_not_change_the_guard`, `test_child_process_that_inherits_the_whole_environment_sees_no_secret` |
| Estado de proteção sem alvo é impossível | `test_protecting_state_without_a_target_is_rejected` |
| Default merge não apaga seção escrita; aliases/duplicatas/merge seguem fail-closed | testes `INHERITED_OR_AMBIGUOUS_YAML`, `AMBIGUOUS_YAML`, `EXPLICIT_YAML`, `ABSENT_YAML` (inalterados em intenção) e `test_default_config_registers_the_section_disabled_and_without_secrets` |
| Manifesto não vira flag desativável por `enabled` | `test_enabled_flag_of_the_consumer_cannot_lift_a_manifest_group` |

Antigos removidos por validarem comportamento obsoleto: leitura/permissões/forma de `credentials.env`
(≈ 40 casos), campos incompletos/inconsistentes das nove chaves, `secret_in_environment`, divergência de
ambiente não secreto, `client_config`/`READY`.

## Comandos reais

```sh
HOME=/tmp/u1-home scripts/run_tests.sh tests/gateway/test_whatsapp_group_gating.py \
    tests/gateway/test_licencas_client.py -- -p no:cacheprovider -q
```

`/tmp/u1-home/.hermes/hermes-agent/venv` é um link para o venv do repositório
(`/home/clawd/.local/share/hermes-agent/venv`, Python 3.11.15, pytest 9.0.2): o worktree não tem `.venv`
nem `venv`, e `run_tests.sh` também procura em `$HOME/.hermes/hermes-agent/venv`. Evidência em
`/home/clawd/licencas-hermes-evidence/u1/`.

## Débito registrado (P2/P3; não reabre rodada)

| # | Grau | Débito | Dono |
|---|---|---|---|
| 1 | P2 | Marcador e manifesto no mesmo usuário do gateway: editáveis por quem tem esse usuário (inclui root). G5-root universal continua aberto | Gate G5 |
| 2 | P2 | Ninguém instala ainda o manifesto/marcador (U10); conferência do consumidor (nove chaves, segredos no ambiente) vai para U2 | U2 / U10 |
| 3 | P3 | `licencas/credentials.env` do A2-pregate fica órfão: nada o lê; remoção é do operador | Operação |
| 4 | P3 | `LICENCAS_CAPABILITY`/`LICENCAS_HMAC_KEY_B64` seguem em `OPTIONAL_ENV_VARS` só para a blocklist dos terminais (defesa em profundidade) | Coordenador |
