# Licenças — barreira do grupo protegido no gateway (A2-pregate)

<!-- #atende:SPEC-licencas-acompanhamento/REQ-R4 REQ-R5 REQ-R8 -->

> **Substituído em parte pelo U1 (2026-11-28).** A guarda deste documento **não lê mais segredo**:
> não abre `licencas/credentials.env`, não tem `client_config`, não confere o ambiente nem as nove chaves do
> consumidor, e `enabled: false` não devolve mais o grupo ao caminho legado. A proteção vem do manifesto e do
> sinal de adoção, independentes do consumidor. Contrato vigente, estados e testes:
> `docs/plans/licencas-isolation-bdd.md`. Onde este texto divergir, vale o U1. As seções de segredos abaixo
> foram reduzidas a ponteiros para não deixar instrução insegura.

Escopo desta unidade: a barreira determinística, a configuração trusted e os testes. **Não** liga a
integração, não consome o inbox durável, não envia cobrança e não confere a saúde da bridge. O gateway e
o piloto **não** estão prontos com esta unidade.

Fontes: `B-EXECUTION-CONTRACTS.md` §1, §5 (A2 itens 1, 2 e 5), §6 e §7; `DELTA-D1-D2-D3-ARQUITETO.md`
("Segurança e gates"); `scripts/whatsapp-bridge/DURABLE_TRANSPORT.md` (A1);
`docs/plans/licencas-gateway-a2-client-bdd.md` (A2-client).

## Arquivos

| Arquivo | Papel |
|---|---|
| `gateway/licencas_followup.py` (novo; U1 retirou os segredos) | Tipo fechado `ProtectedGroupGuard` (`groups`/`all_groups`, sem `client_config`), leitura do `config.yaml`, do manifesto e do sinal de adoção, decisão e contadores |
| `gateway/platforms/whatsapp.py` | Barreira de entrada (bridge e evento) e de envio |
| `gateway/run.py` | Interceptação defensiva em `_handle_message` e no caminho de sessão ocupada |
| `hermes_cli/config.py` (follow-ups r3, r4 e r5) | Seção `licencas_followup` em `DEFAULT_CONFIG` (desligada, sem segredos; é do consumidor, não decide a proteção); os dois nomes reservados em `OPTIONAL_ENV_VARS`, só para a blocklist dos terminais, marcados "Do NOT set" |
| `tools/send_message_tool.py` (follow-up P1, sexto arquivo autorizado) | Barreira em `_send_whatsapp`, que fala direto com a bridge |
| `tests/gateway/test_whatsapp_group_gating.py` | 26 testes anteriores intactos + os desta unidade |

`gateway/licencas_client.py` e o teste dele **não mudaram** (ver "Cliente").

## Configuração

### `config.yaml` do gateway (nada aqui é segredo)

```yaml
licencas_followup:
  enabled: true                 # booleano YAML, do consumidor; NÃO decide a proteção (U1)
  group_jid: "<dígitos>@g.us"   # o grupo protegido
  instance_id: "<conta>@s.whatsapp.net"
  principal_id: "<id do principal>"
  policy_id: "<uuid minúsculo>"
  policy_epoch: 1               # inteiro >= 1 (também aceita o mesmo número como texto)
  account_binding_hash: "<64 hex minúsculos>"
  key_id: "<id da chave>"
  api_url: "https://<host>/<prefixo>/licencas-api"
  signed_pathname: "/licencas-api"
```

Lido do `config.yaml` do perfil base do processo (`HERMES_HOME` do ambiente, sem o override de perfil por
contexto), com `yaml.safe_load` depois de recusar chave duplicada.
A guarda só usa `enabled` (para validar o tipo) e `group_jid`; as outras oito chaves são do consumidor (U2).
Chave de segredo na seção é erro (`secret_in_config`). Com `enabled: false` e `group_jid` válido o grupo
continua barrado (U1).

### Segredos: nenhum na guarda (U1)

A guarda não lê nem carrega segredo. **Não crie `licencas/credentials.env` para ela**: nada o abre. O
arquivo do A2-pregate fica órfão e pode ser removido pelo operador. Capability e chave HMAC pertencem só ao
conector dedicado, entregues fora deste perfil (U2/U10). Segredo no `config.yaml` (chaves `capability`,
`hmac_key`, `hmac_key_b64` e equivalentes) é configuração inconsistente (`secret_in_config`, só o nome é
conferido) e segredo no `.env` não adianta: a guarda não o consulta e a blocklist dos terminais o retira.

### Estados

Ver `docs/plans/licencas-isolation-bdd.md`, seção "Estados". Resumo: `DISABLED` só sem adoção e sem seção
(ou seção desligada sem grupo); `PROTECTED` com manifesto íntegro ou `group_jid` válido na seção (mesmo
com `enabled: false`); `CONFIG_ERROR` fecha só o grupo identificável, ou todos os grupos de WhatsApp quando
o alvo é irrecuperável (inclusive manifesto ausente depois da adoção).

O guarda é lido **uma vez** por processo e fica congelado. No gateway isso acontece na primeira instrução
de `GatewayRunner.__init__`; fora dele, no primeiro uso. Mudar a configuração exige
reiniciar o gateway. Editar, quebrar ou apagar o `config.yaml` com o gateway no ar não solta o grupo.

## Pontos de interceptação

| # | Arquivo e função | Quando | Efeito no grupo protegido |
|---|---|---|---|
| 1 | `whatsapp.py` `WhatsAppAdapter._poll_messages` | Antes de `_build_message_event` (logo, antes de `cache_image_from_url`, `cache_audio_from_url`, leitura de documento e `print` de caminho) | Mensagem descartada; `inbound_bridge` |
| 2 | `whatsapp.py` `WhatsAppAdapter.handle_message` (override) | Antes de `BasePlatformAdapter.handle_message` (chave de sessão, fila pendente, interrupção, tarefa de fundo, typing) | Evento descartado; `inbound_event` |
| 3 | `run.py` `GatewayRunner._handle_message` (primeira instrução) | Antes de `pre_gateway_dispatch`, autorização, pareamento, comandos e sessão. Vale para qualquer plataforma e para `internal=True` | Retorna `None`; `dispatch` |
| 4 | `run.py` `GatewayRunner._handle_active_session_busy_message` (primeira instrução) | Antes da autorização e do tratamento de sessão ocupada | Retorna `True` (tratado, descartado); `dispatch` |
| 5 | `whatsapp.py` `send`, `edit_message`, `_send_media_to_bridge`, `send_image`, `send_typing` | Antes de qualquer checagem de conexão, download ou POST à bridge | `SendResult(success=False, error="licencas_followup_protected_group")`; `outbound` |
| 6 | `tools/send_message_tool.py` `_send_whatsapp` (primeira instrução) | Antes de importar `aiohttp` e do POST direto em `/send` | `{"error": "licencas_followup_protected_group"}`; `outbound` |

O alvo só existe se `group_jid`, depois de normalizado, tiver o formato de grupo que o cliente e o banco
exigem (`<dígitos>[-<dígitos>]@g.us`); em `PROTECTED` ele é exatamente o `group_jid` da seção (ou um grupo do manifesto).
Reconhecimento do grupo: comparação exata depois de tirar espaços, passar para minúsculas e remover
sufixo de dispositivo (`:N@`). Listas e tuplas são percorridas. Nos eventos são conferidos
`source.chat_id`, `source.chat_id_alt`, `source.parent_chat_id` e `raw_message["chatId"]`.

Erro dentro da barreira (inclusive ao ler a configuração ou diante de mensagem malformada da bridge)
bloqueia e conta `barrier_error`. Com a barreira quebrada, isso vale para **qualquer** evento e envio que
passe por esses pontos.

A barreira lê só o identificador do chat. Não lê texto, mídia, caminho nem URL do envelope, não cria
sessão e não chama modelo, ferramenta, plugin, cron, cache nem transcrição.

## Logs e contadores

- `licencas_followup.block_counts()` devolve `{tipo: contagem}`. Tipos: `inbound_bridge`,
  `inbound_event`, `dispatch`, `outbound`, `barrier_error`.
- Uma linha WARNING por bloqueio: `licencas_followup blocked kind=<tipo> count=<n>`.
- Erro na barreira: `licencas_followup barrier error=<classe da exceção> kind=<tipo>`, sem mensagem
  nem traceback.
- Carga do guarda: `licencas_followup guard state=<estado> scope=<none|group|all_whatsapp_groups>
  reason=<nome da chave|none>`.
- Nunca: texto, JID (de pessoa ou do grupo), telefone, nome, ID de mensagem, caminho ou URL de mídia,
  valores de configuração.

## Rastreabilidade contrato → teste

| Contrato | Cenário | Teste |
|---|---|---|
| §5 A2.1, §7 "A2 spies" | 17 formas de mensagem do grupo (texto, `/reset`, `/new`, `/stop`, comando com argumento, anexo, áudio, imagem, vídeo, resposta citada, menção, injeção, tipo desconhecido, JID em maiúsculas, com sufixo, em lista, marcado como DM): `_build_message_event`, cache de imagem e áudio, `handle_message`, handler e POST nunca chamados | `test_protected_group_is_barred_before_event_build` |
| §5 A2.1 | Mensagem protegida no meio do lote não impede as outras | `test_protected_message_does_not_stop_the_rest_of_the_batch` |
| §5 A2.1 (falta de config não cai no legado) | 5 configurações que bloqueiam × texto, `/reset`, anexo, áudio | `test_every_non_disabled_configuration_keeps_the_group_off_legacy` |
| §5 A2.1 (evento forjado) | 20 eventos (comandos, `internal=True`, Telegram e Discord com o JID, mídia, citação, injeção, JID só em `chat_id_alt`, `parent_chat_id` ou no envelope bruto) entregues ao adapter: classe base e handler nunca chamados | `test_adapter_refuses_events_for_the_protected_group` |
| §5 A2.1 (interceptação defensiva), §7 | Os mesmos 20 eventos no runner: hook de plugin, autorização, chave de sessão, pareamento, `_handle_message_with_agent`, `_run_agent`, preparo de texto, transcrição, visão, `/reset`, `session_store`, `hooks` e adapter sem nenhuma chamada | `test_runner_rejects_protected_group_before_hooks_auth_and_session` |
| §5 A2.1 | 5 configurações que bloqueiam no runner | `test_runner_rejects_protected_group_under_every_blocking_configuration` |
| §5 A2.1 | Caminho de sessão ocupada | `test_runner_busy_path_drops_protected_group` |
| Falha fechada | Exceção na barreira: adapter, runner (qualquer evento), sessão ocupada e envio bloqueiam | `test_barrier_error_blocks_the_adapter_path`, `test_barrier_error_blocks_the_runner_for_any_event`, `test_barrier_error_blocks_every_generic_send`, `test_malformed_bridge_payload_is_blocked_not_processed` |
| §5 A2.2 | Módulo só importa configuração e o tipo do cliente; não abre arquivo fora do `config.yaml`, não cria cliente, não envia | `test_barrier_module_has_no_agent_session_or_network_dependencies` |
| Item 1 (tipo fechado) | (U1) Configuração completa → `ProtectedGroupGuard` com `groups`, sem `client_config`; guarda imutável; estado sem alvo impossível | `test_complete_configuration_protects_only_its_group`, `test_protecting_state_without_a_target_is_rejected`, `test_disabled_state_cannot_carry_a_target` |
| Item 1 (ausente/desabilitada) | (U1) Ausente ou desligada **sem grupo** = nada protegido; desligada **com grupo válido** continua barrando | `test_disabled_section_without_a_group_stays_legacy`, `test_disabled_section_with_a_valid_group_still_bars_it`, `test_missing_config_file_or_section_is_the_legacy_gateway` |
| Item 1 (incompleta) | (U1) Obsoleto: a guarda não exige as oito chaves do consumidor | `test_consumer_fields_are_not_the_guards_business` |
| Item 1 (inconsistente) | (U1) `enabled` não booleano e chave de segredo na seção | `test_enabled_that_is_not_boolean_blocks_only_the_configured_group`, `test_secret_key_in_the_consumer_section_fails_without_revealing_it` |
| Item 1 (grupo não identificável) | 6 casos: todos os grupos barrados, DM não | `test_enabled_without_identifiable_group_blocks_every_whatsapp_group`, `test_unreadable_config_file_fails_closed`, `test_loader_failure_is_a_blocked_guard_not_an_exception` |
| Revisão canônica P1 (`group_jid` inválido) | 14 formas inválidas com `enabled: true`: alvo `None`; entrada, despacho e envio (adapter e bridge direta) de um grupo real barrados, DM não | `test_invalid_group_jid_never_becomes_the_barrier_target`, `test_invalid_group_jid_bars_every_real_group_and_no_dm` |
| Revisão canônica P1 (alvo) | (U1) JID válido (atual e com hífen) barra só o configurado; forma solta barra o grupo certo; formato igual ao do cliente. Divergência alvo × cliente deixou de existir (sem cliente) | `test_valid_group_jid_is_the_single_target`, `test_valid_group_jid_written_loosely_still_bars_the_right_group`, `test_barrier_group_format_is_the_client_format` |
| Revisão canônica P2 (raiz `null`) | Arquivo vazio ou só com comentário = sem integração; `null`, `~`, `--- null`, escalar e lista na raiz = bloqueio | `test_empty_config_file_is_the_legacy_gateway`, `test_config_root_that_is_not_a_mapping_fails_closed` |
| Item 1 (config.yaml) | Leitura real do `config.yaml` do perfil base; guarda congelado | `test_guard_comes_from_config_yaml_alone` |
| Item 5 (envio) | 5 configurações × 3 formas do JID: `send`, resposta, resposta do agente (`_send_with_retry`), edição, imagem por URL, arquivo de imagem, vídeo, voz, documento, `DeliveryRouter`, typing: zero POST, zero download | `test_generic_send_paths_cannot_reach_the_protected_group` |
| §7 regressão | Outro grupo e DM: mesmos eventos, mesma mídia em cache, com 5 configurações | `test_other_groups_and_dms_follow_the_legacy_path_unchanged`, `test_adapter_hands_other_events_to_the_base_class`, `test_runner_dispatches_other_chats_exactly_as_before`, `test_runner_reaches_authorization_for_other_chats`, `test_generic_send_paths_still_reach_other_chats` |
| Item 5 (envio direto, follow-up P1) | 5 configurações × 3 formas do JID em `_send_whatsapp` e em `_send_to_platform`: zero POST; erro na barreira recusa; outros chats recebem o mesmo POST de antes | `test_send_message_tool_direct_bridge_path_cannot_reach_the_protected_group`, `test_barrier_error_blocks_the_direct_bridge_path`, `test_direct_bridge_path_still_reaches_other_chats` |
| Item 5 (cron) | `_deliver_result` real com job `whatsapp:<grupo>`, com adapter vivo e standalone, 5 configurações: zero POST, erro devolvido; outro chat entrega | `test_cron_job_targeting_the_protected_group_does_not_send`, `test_cron_job_targeting_another_chat_still_delivers` |
| Item 5 (varredura) | Todo `.py` fora de `tests/`: os pontos que montam URL da bridge são exatamente os 8 da lista, e os 5 de efeito chamam `blocks_outbound` antes da URL | `test_every_direct_bridge_call_is_known_and_guarded`, `test_bridge_scan_notices_an_unlisted_or_unguarded_sender` |
| Revisão r2 P1-a (segredos em subprocessos) | Fechado por construção na r4: ver "Segredos e subprocessos" | testes listados naquela seção |
| Revisão r2 P1-b (perfil) | Primeira carga dentro de outro perfil usa o perfil base; outro perfil não habilita nem troca o alvo; guarda carregada não muda; cron sob outro perfil não envia (adapter vivo e direto, guarda iniciada ou não); ordem de início | `test_guard_first_loaded_inside_another_profile_context_uses_the_base_profile`, `test_another_profile_cannot_enable_or_retarget_the_guard`, `test_loaded_guard_is_not_changed_by_a_later_profile_context`, `test_cron_job_under_another_profile_does_not_reach_the_protected_group`, `test_gateway_runner_loads_the_guard_before_anything_else`, `test_gateway_start_builds_the_runner_before_cron_and_adapters` |
| Revisão r2 P1-c e r3 P1 (YAML ambíguo) | Regra única "seção efetiva = seção explícita": ver a seção de mesmo nome | testes listados naquela seção |
| Item 6 (logs) | Todos os cenários de bloqueio com log em DEBUG, stdout e stderr capturados: nenhum dos 8 valores sensíveis aparece | `test_blocks_are_logged_without_any_message_data`, `test_guard_loading_logs_state_and_key_name_only` |

Os espiões são `AsyncMock`/`MagicMock` no lugar dos colaboradores. Eles provam a **ausência** de chamada
dentro do processo de teste. Não provam nada sobre a bridge real, o WhatsApp ou a fachada.

## Cliente (A2-client)

`gateway/licencas_client.py` não foi alterado em nenhuma rodada e a guarda **não o importa** (U1). Quem monta
`load_licencas_config` e o `LicencasClient` é o conector dedicado, com leitor próprio e segredo próprio
(U2). `LicencasClient.from_env()` sem argumento ainda existe e não deve ser usado.

## Decisões do executor (onde o contrato não fixava)

1. **Seção `licencas_followup` no topo do `config.yaml`**, lida direto do arquivo do perfil.
   `gateway/config.py` (ponte YAML→`PlatformConfig.extra`) está fora dos arquivos desta unidade.
2. **Leitura estrita.** `read_raw_config()` devolve `{}` quando o YAML não abre, o que seria falha
   aberta. A barreira lê o arquivo ela mesma e trata erro de leitura como bloqueio.
3. **Sem JID utilizável, bloqueia todos os grupos de WhatsApp.** Não há como isolar "o grupo
   configurado" sem saber qual é. Isso inclui `config.yaml` com YAML inválido em instalação que nem usa
   a integração: os grupos de WhatsApp ficam mudos até o arquivo ser corrigido e o gateway reiniciado,
   com linha ERROR `scope=all_whatsapp_groups`. DMs e outras plataformas não são afetadas.
4. **Guarda congelado por processo.** Recarregar a quente permitiria soltar o grupo com uma edição ruim.
5. **`enabled` tem de ser booleano YAML.** `"true"`, `1` ou vazio bloqueiam.
6. **Valor não secreto no ambiente** (`LICENCAS_GROUP_JID` etc.) diferente do YAML é inconsistência.
7. **Typing também é recusado** para o grupo, embora a bridge A1 não o guarde.
8. **`get_chat_info` não é barrado**: é leitura, e a A2-consumer precisa dos participantes.

## O que a A2-consumer precisa saber

- **Guarda**: `licencas_followup.current_guard()` só diz quais grupos o gateway barra (`groups`,
  `all_groups`); **não** é porta de ativação do consumidor e não entrega configuração de cliente.
- **Cliente**: o conector monta o próprio `LicencasClientConfig` (U2). Nunca `LicencasClient.from_env()`.
- **Grupo**: conferir conta e grupo de cada envelope A1 contra a configuração do conector antes de chamar
  a fachada continua sendo do consumidor.
- **Entrada**: a barreira não entrega nada ao consumidor. A única fonte é `POST /durable/inbox/claim`.
  Se a bridge não estiver com o grupo em `WHATSAPP_DURABLE_GROUP_IDS`, as mensagens chegam por
  `/messages`, são barradas aqui (`inbound_bridge` sobe) e **se perdem**. `inbound_bridge > 0` é sinal
  de bridge mal configurada.
- **Envio**: `WhatsAppAdapter.send` e os demais métodos recusam o grupo. O consumidor precisa de um
  método próprio para `POST /send` com `attempt_id`, que não passe por `send`.
- **Saúde da bridge (§5 A2.5)**: nada foi feito aqui. `WhatsAppAdapter.connect` ainda reaproveita uma
  bridge só pelo `status: connected`.
- **Contadores**: `block_counts()` é memória do processo e zera no reinício.
- **Reinício**: obrigatório depois de mudar a seção ou o arquivo de segredos.
- **Segredos**: não existem na guarda. No conector, nunca em `os.environ`, log, argumento de linha de
  comando nem ambiente de processo filho.
- **Health**: incluir o estado da guarda (`state`, `config_error`, `all_groups`) e a conferência de que a bridge tem o
  grupo em `WHATSAPP_DURABLE_GROUP_IDS`; um processo separado sob outro perfil depende só disso.

## Pontos que falam direto com a bridge (follow-up P1)

O P1 da primeira rodada está fechado: `tools/send_message_tool.py` `_send_whatsapp` (usado pela ferramenta
`send_message` e pela entrega do cron) fazia POST direto em `/send` sem passar pelo adapter. Agora recusa
o grupo protegido na primeira instrução.

Varredura de todo `.py` do repositório fora de `tests/` (`tools/`, `cron/`, `gateway/`, `plugins/`,
`hermes_cli/`, `agent/`, `acp_adapter/`, `tui_gateway/`, `scripts/` e a raiz). Só dois arquivos montam
URL da bridge:

| Arquivo e função | Rota | Aceita chat de destino | Estado |
|---|---|---|---|
| `gateway/platforms/whatsapp.py` `send` | `POST /send` | sim | barrado |
| `gateway/platforms/whatsapp.py` `edit_message` | `POST /edit` | sim | barrado |
| `gateway/platforms/whatsapp.py` `_send_media_to_bridge` (imagem, vídeo, voz, documento) | `POST /send-media` | sim | barrado |
| `gateway/platforms/whatsapp.py` `send_typing` | `POST /typing` | sim | barrado |
| `tools/send_message_tool.py` `_send_whatsapp` | `POST /send` | sim | barrado (follow-up) |
| `gateway/platforms/whatsapp.py` `get_chat_info` | `GET /chat/<id>` | leitura | não barrado (decisão 8) |
| `gateway/platforms/whatsapp.py` `_poll_messages` | `GET /messages` | entrada | mensagem do grupo barrada antes do evento (ponto 1) |
| `gateway/platforms/whatsapp.py` `connect` | `GET /health` | não | sem efeito no chat |

Não existe no repositório chamada a rota de reação, exclusão ou leitura de mensagem da bridge. Fora do
Python, só a própria bridge (`scripts/whatsapp-bridge/`) conhece essas rotas. `hermes_cli/main.py`
(pareamento) e o adapter iniciam o processo `bridge.js`, sem enviar nada a chat.

A entrega do cron (`cron/scheduler.py` `_deliver_result`) não fala com a bridge por conta própria: usa o
adapter vivo (`adapter.send` e os métodos de mídia) e, se falhar ou não houver adapter, `_send_to_platform`
→ `_send_whatsapp`. Os dois caminhos estão barrados; `cron/scheduler.py` não foi alterado.

`test_every_direct_bridge_call_is_known_and_guarded` falha se aparecer um ponto novo fora dessa lista, se
um ponto listado sumir, ou se um ponto de efeito deixar de chamar `licencas_followup.blocks_outbound`
antes da URL. A A2-consumer vai acrescentar o envio com `attempt_id`: esse ponto novo tem de entrar na
lista do teste, conferido.

## Segredos e subprocessos (follow-up r4, revisto no U1)

A guarda não tem segredo, então não há segredo dela em `os.environ` nem em filho algum. Os dois nomes
`LICENCAS_CAPABILITY` e `LICENCAS_HMAC_KEY_B64` continuam em `OPTIONAL_ENV_VARS` (`hermes_cli/config.py`,
categoria `messaging`, `password`, `advanced`) só como defesa em profundidade: alimentam a blocklist dos
terminais (`tools/environments/local.py`), de modo que, se um operador os puser no ambiente por engano,
terminal local, passthrough de skill, `execute_code` e MCP não os repassam
(`test_local_terminal_environment_never_carries_the_secrets` e os quatro vizinhos). Limite: `OPTIONAL_ENV_VARS`
também alimenta o painel web e `hermes config`, onde os nomes aparecem como itens avançados "Do NOT set".

| Prova (U1) | Teste |
|---|---|
| A guarda nunca abre `credentials.env` nem `.env`, com ou sem adoção, ligada ou desligada | `test_guard_never_opens_a_credentials_file` |
| Sem `client_config`, sem importar o cliente, sem consultar o ambiente | `test_guard_module_has_no_secret_surface`, `test_guard_does_not_import_the_client_module` |
| Carga não altera o ambiente; filho que herda tudo não vê nomes nem valores | `test_child_process_that_inherits_the_whole_environment_sees_no_secret` |
| Recarga do `.env` pelo gateway não muda a guarda | `test_gateway_env_reload_does_not_change_the_guard` |
| Segredo no ambiente não muda a guarda | `test_secrets_in_the_environment_do_not_change_the_guard` |
| Chave de segredo na seção falha sem revelar valor | `test_secret_key_in_the_consumer_section_fails_without_revealing_it` |

**O que continua verdade:** código que roda no processo do gateway (plugin, ferramenta em processo) alcança
a memória dele; a guarda só não a enriquece com segredo. G5-root universal segue aberto.

## Perfil base e ordem de início (follow-up r3, P1-b)

- A guarda lê o `config.yaml` do **perfil base do processo** (`HERMES_HOME` do ambiente), ignorando o
  override de perfil por contexto que `cron/scheduler.py` `_job_profile_context` instala
  (`licencas_followup._base_config_path`).
- `GatewayRunner.__init__` chama `licencas_followup.current_guard()` na primeira instrução, antes de
  carregar a configuração do gateway, criar adapters ou iniciar o cron.
- Job de cron sob outro perfil que mande pelo adapter vivo ou pelo caminho direto é barrado pela guarda do
  gateway, não pela configuração do perfil do job.
- **Limite que sobra:** um processo separado (por exemplo um comando de CLI ou um cron standalone sob
  outro perfil) tem a própria guarda, lida do perfil dele. Se esse perfil não tiver a seção, ali a única
  barreira é a configuração durável da bridge do A1 (403 no `/send` sem `attempt_id`). **Item do health
  da A2-consumer:** confirmar que a bridge tem o grupo em `WHATSAPP_DURABLE_GROUP_IDS`.
- Os segredos e o `config.yaml` vêm dos arquivos do perfil base, nunca do ambiente; um job de perfil
  que troque `os.environ` não alcança a guarda. A carga continua no início do gateway.

## Seção efetiva = seção explícita (follow-ups r3, r4 e r5)

Regra única, em `licencas_followup._load_config_text`, para toda forma de "YAML ambíguo desliga a
proteção" (chave repetida, merge direto ou aninhado, alias, âncora, tag):

1. O arquivo é lido com `yaml.safe_load`, como o resto do Hermes. Se o resultado **não** tem a chave
   `licencas_followup` na raiz, a integração está ausente e o gateway segue como hoje, com qualquer YAML
   que o Hermes aceite (merge na raiz, chaves repetidas, aliases e tags em outras seções).
2. Se tem, o documento é composto de novo, sem construir, e a seção **explícita** é extraída: exatamente um
   mapa escrito diretamente sob a raiz, com chaves escalares únicas e valores escalares, sem merge, âncora,
   alias ou tag que mude o tipo dentro dele, e sem chave de merge na raiz.
3. A seção **efetiva** (o que o `safe_load` devolveu) tem de ser idêntica à explícita, chave por chave,
   valor por valor e tipo por tipo. Qualquer diferença, ou a falta de uma seção explícita única, é
   `AmbiguousConfigError`: falha fechada, todos os grupos de WhatsApp barrados.

A segunda composição existe porque construir achata os merges dentro dos próprios nós e apagaria o que
precisa ser visto. Não há mais lógica separada para chave duplicada: os casos das rodadas anteriores
passam por essa mesma regra.

| Cenário | Teste |
|---|---|
| 22 formas que bloqueiam: merge direto na seção; merge aninhado em 2 e 3 níveis na seção; seção só por merge na raiz (direto, 2 e 3 níveis); seção explícita mais outra por merge (antes, depois e aninhado); alias para a seção inteira; âncora na seção, em valor e em chave; alias em valor; tag personalizada na seção e em valor; tag padrão que muda o tipo; valor não escalar; seção nula ou lista; seção repetida; chave repetida | `test_section_that_is_not_exactly_the_explicit_one_fails_closed`, `test_duplicate_keys_in_config_yaml_fail_closed` |
| Caso feliz: simples, com comentários e aspas, `policy_epoch` como texto, tag padrão sem efeito, âncoras e merges em outras seções | `test_explicit_section_loads`, `test_config_yaml_without_duplicates_still_loads` |
| Sem a seção: merge na raiz (direto e aninhado), nome da seção dentro de outra ou como valor, chaves repetidas e tags alheias: leitura igual à do `safe_load`, integração ausente | `test_yaml_without_the_section_is_the_legacy_gateway`, `test_unrelated_duplicate_root_keys_behave_as_before` |

**Erro do parser.** Qualquer exceção ao ler ou interpretar o `config.yaml` (inclusive `RecursionError` de
um arquivo muito aninhado) vira "configuração ilegível": `current_guard()` não levanta, o gateway sobe e a
guarda fica em `CONFIG_ERROR` com todos os grupos de WhatsApp barrados
(`test_parser_recursion_error_is_an_unreadable_config_not_a_crash`,
`test_any_error_while_reading_the_config_is_an_unreadable_config`).

**`DEFAULT_CONFIG`.** A seção está registrada em `hermes_cli/config.py` com `enabled: false`, os nove
valores não secretos vazios (`policy_epoch: 0`) e nenhum segredo. A guarda lê o YAML cru do perfil base,
não a configuração mesclada com defaults: instalação sem a seção no arquivo continua "integração ausente"
(`test_default_config_registers_the_section_disabled_and_without_secrets`,
`test_defaults_do_not_count_as_a_present_section`).

## Débito registrado (P2/P3; não bloqueia, regra D0)

| # | Grau | Débito | Dono | Risco |
|---|---|---|---|---|
| 1 | P2 | YAML inválido no `config.yaml` cala todos os grupos de WhatsApp mesmo sem a integração (decisão 3) | Coordenador | Indisponibilidade de grupos por erro de edição alheio à integração |
| 2 | P2 | Mensagem do grupo que chega por `/messages` é descartada sem destino durável (bridge fora do escopo durável) | A2-consumer / A2-health | Perda de mensagem enquanto a bridge não estiver configurada para o grupo |
| 3 | P2 | A configuração durável da bridge (`WHATSAPP_DURABLE_*`) continua só no ambiente; não é derivada da seção `licencas_followup` nem conferida contra ela | A2-health | Gateway protege um grupo e a bridge outro |
| 4 | P3 | `LicencasClient.from_env()` sem argumento continua existindo; com os segredos fora do ambiente ele falha fechado | A2-consumer | Caminho morto no cliente |
| 5 | P3 | Uma linha WARNING por bloqueio, sem limite de taxa | Operação | Volume de log se a bridge estiver mal configurada |
| 6 | P3 | Contadores só em memória, sem exposição em health | A2-consumer | Sem visibilidade externa |
| 7 | P3 | A resposta do agente recusada passa duas vezes pela barreira (tentativa e fallback em texto puro de `_send_with_retry`) | — | Contagem `outbound` em dobro nesse caminho |
| 8 | P3 | `PHASE.md` continua em `align`; esta unidade não o altera | Coordenador | Protocolo de etapas sem registro desta fatia |
| 9 | P3 | `ruff format --check` já falhava em `whatsapp.py`, `run.py`, `send_message_tool.py` e no arquivo de teste antes desta unidade. O módulo novo e as linhas novas do arquivo de teste (374 em diante, com `ruff format --range`) estão formatados; as 373 linhas antigas não foram tocadas | Repositório | Nenhum |
| 10 | P3 | A varredura só enxerga URL literal em host local (`localhost`/`127.0.0.1`) com rota da bridge, em arquivos `.py`. Chamador que monte a URL de outra forma, ou em outra linguagem, escapa | A2-consumer | Caminho novo de envio sem barreira e sem alarme |
| 11 | P3 | `cron/scheduler.py` registra o destino (`plataforma:chat_id`) ao falhar ou entregar; com o grupo protegido como destino, o JID do grupo aparece nesse log, que é anterior a esta unidade. Texto, pessoa e mídia não aparecem | Coordenador | JID do grupo em log do cron |
| 12 | P3 | Entrega do cron para o grupo com adapter vivo passa duas vezes pela barreira (adapter e fallback standalone) | — | Contagem `outbound` em dobro nesse caminho |
| 13 | — | (U1) Obsoleto: a guarda não tem arquivo de segredos; ver o débito 1 de `licencas-isolation-bdd.md` | — | — |
| 18 | P2 | A guarda confia no `config.yaml` do perfil. Quem pode escrever nele (inclusive o agente pelo terminal, no mesmo usuário) pode desligar a seção; vale a partir do próximo reinício do gateway | Coordenador | Barreira desligada por edição local da configuração |
| 14 | P3 | Os dois nomes aparecem no painel web e em `hermes config` como variáveis avançadas "Do NOT set", porque a blocklist dos terminais é alimentada por `OPTIONAL_ENV_VARS`. Tirá-los de lá exige duas linhas na lista fixa de `tools/environments/local.py` | Coordenador | Operador grava o nome no `.env` pelo painel; a guarda não muda, a blocklist dos terminais o retira dos filhos |
| 15 | — | (U1) Obsoleto: a guarda não confere o ambiente; a conferência do consumidor é do conector (U2) | — | — |
| 16 | P3 | Arquivo de segredos só em POSIX; em Windows a carga falha fechada. O diretório acima de `licencas/` (o próprio `HERMES_HOME`) não é conferido | Coordenador | Integração indisponível em Windows; troca do diretório por quem já controla o `HERMES_HOME` |

| 19 | P2 | Com a seção presente, merge na raiz do `config.yaml` (mesmo sem relação com a seção) e qualquer âncora, alias ou tag dentro dela bloqueiam todos os grupos de WhatsApp. É o preço de fechar a classe por construção | Coordenador | Configuração válida para o YAML recusada pela guarda |
| 20 | P3 | Com a seção presente o `config.yaml` é interpretado três vezes na carga (uma só vez por processo) | — | Nenhum |
| 21 | P3 | `hermes config` ou o painel podem gravar no arquivo a seção vinda dos defaults (`enabled: false`); o efeito para a guarda é o mesmo de ausente | Operação | Seção desligada aparecendo no arquivo |

## Evidência

Logs em `/home/clawd/licencas-hermes-evidence/a2-pregate/`. Os números reais desta execução estão no
resultado do executor e em `checks.txt` nesse diretório.
