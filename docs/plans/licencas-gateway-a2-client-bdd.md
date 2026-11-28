# Licenças — A2-client: cliente HTTP tipado da fachada `licencas-api` (BDD)

<!-- #atende: B-EXECUTION-CONTRACTS §5/§6/§7 + RPC-COMPLEMENTO-CONTRATUAL §1.4/§1.5/§2 — micro-unidade A2-client -->

Unidade do piloto Alvarás/Licenças. Entrega só o cliente e os testes dele. **Não liga nada**: nenhum arquivo
existente foi alterado, o cliente não é importado por `gateway/run.py`, pelo adapter WhatsApp nem pela bridge, e
esta unidade não implementa pré-gate nem consumidor. Aceite desta unidade não é ativação do gateway nem do piloto.

## Arquivos (3, todos novos)

| Arquivo | Papel |
|---|---|
| `gateway/licencas_client.py` | Cliente assíncrono (`httpx`, sem dependência nova), tipos fechados, erros tipados |
| `tests/gateway/test_licencas_client.py` | Testes pytest com `httpx.MockTransport` — **fronteira de unidade**, sem socket |
| `docs/plans/licencas-gateway-a2-client-bdd.md` | Este documento |

## Fontes

- `B-EXECUTION-CONTRACTS.md` §5 (HTTP, cabeçalhos, bytes assinados, itens A2 1–5), §6 e §7.
- `RPC-COMPLEMENTO-CONTRATUAL.md` §1.4, §1.5 e §2.
- Contrato real do outro lado, só leitura: `supabase/functions/licencas-api/contract.ts` do worktree
  `armando-licencas-capability-http`, sha256 `933a827c6f97d940eefd7e5a98814c8d7861c9ea422e390b6d861db16954d7a0`.
  É a fonte dos formatos, codecs, DTOs e códigos de erro copiados aqui.
- Decisões D0–D4 do Glauco (`DECISOES-GLAUCO-2026-11-27.md`).

## Pedido

- `POST` para o endpoint configurado, corpo `{"version":1,"operation","context","payload"}` em JSON compacto UTF-8,
  sem NaN/Infinity, no máximo 16 KiB (16 384 bytes) conferidos **antes** de enviar.
- Os bytes enviados são exatamente os assinados.
- Bytes assinados: `licencas/v1\nPOST\n<pathname assinado>\n<key-id>\n<timestamp>\n<nonce>\n<request-id>\n<sha256 hex do corpo>`,
  HMAC-SHA256 em hex.
- Cabeçalhos: `Content-Type: application/json`, `X-Licencas-Capability`, `X-Licencas-Key-Id`,
  `X-Licencas-Timestamp` (segundos Unix), `X-Licencas-Nonce` (32 bytes de `secrets`, 64 hex),
  `X-Licencas-Request-Id` (UUID v4 novo), `X-Licencas-Signature`. Nunca `Authorization`, `apikey` ou cookie.
- Timestamp, nonce e request_id são novos **a cada chamada de método**. O chamador não os escolhe.
- `httpx.AsyncClient` com `follow_redirects=False`, `trust_env=False`, `verify=True`, timeouts explícitos
  (conexão 5 s, leitura 15 s, escrita 5 s, pool 5 s) e pote de cookies que recusa tudo.
- **Um pedido HTTP por chamada.** Não existe retry dentro do cliente.

## Configuração (falha fechada)

`load_licencas_config(read=os.environ.get, *, raw_config=<disco>)` devolve `LicencasClientConfig`, dataclass congelada que
valida tudo na construção. A configuração tem duas fontes, sem sobreposição:

- **Nove opções não secretas: só o `config.yaml`**, seção `licencas_followup` do perfil base, lido pelo leitor estrito
  que a guarda já usa (`gateway/licencas_followup.py`; o cliente importa o leitor, a guarda nunca importa o cliente).
  O YAML é soberano: não há fallback para variável de ambiente nem valor padrão que resgate ausência (`policy_epoch: 0`
  é inválido). O contrato antigo de 11 variáveis de ambiente foi removido: era o defeito (P1 da PR #5).
- **Dois segredos: só o leitor de credenciais `read`.** Ele só é consultado para `LICENCAS_CAPABILITY` e
  `LICENCAS_HMAC_KEY_B64`, e só depois de as nove opções públicas serem validadas. O padrão `os.environ.get` é
  compatibilidade; o conector isolado injeta um leitor dedicado. Segredo no YAML é recusado (`secret_in_config`).

`raw_config` (raiz bruta do YAML) é injeção opcional só por palavra-chave: omitido, lê o disco; `None` explícito é
inválido e não volta ao disco. Falha (arquivo ilegível, YAML inválido, duplicado, com alias ou merge, seção ausente,
chave desconhecida, `enabled` que não seja booleano, opção ausente, vazia ou fora da forma) levanta
`LicencasConfigError` com o **nome** da opção ou um código fixo (`config_yaml`, `config_root`, `licencas_followup`,
`unexpected_key`, `secret_in_config`), nunca o valor, e nenhum cliente é criado. `enabled` é aceito mas não liga nada.

| Chave em `licencas_followup` | Forma | Observação |
|---|---|---|
| `api_url` | `https://<host>[:porta]<caminho>`, sem credencial, query, fragmento ou barra final | O caminho precisa terminar no pathname assinado |
| `signed_pathname` | `/segmento[/segmento…]` | O pathname que a Edge assina (hoje `/licencas-api`) |
| `key_id` | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` | Mesmo valor da Edge |
| `principal_id` | idem | Mesmo valor da Edge |
| `instance_id` | JID da conta WhatsApp | Mesmo valor da Edge |
| `policy_id` | UUID minúsculo | Mesmo valor da Edge |
| `group_jid` | `<dígitos>@g.us` | Mesmo valor da Edge |
| `policy_epoch` | `int` YAML estrito (não bool, texto nem float), 1 a 2^53−1 | Mesmo valor da Edge |
| `account_binding_hash` | 64 hex minúsculos | Mesmo valor da Edge |

| Credencial (leitor) | Forma | Observação |
|---|---|---|
| `LICENCAS_CAPABILITY` | 32–512 de `[A-Za-z0-9._~+/=-]`; forma de JWT ou prefixo `sb_` recusados | Segredo. Mesmo valor da Edge |
| `LICENCAS_HMAC_KEY_B64` | base64 canônico de 32–128 bytes | Segredo. Mesmo valor da Edge |

Leitor que levanta exceção, devolve tipo errado, `None` ou vazio vira `LicencasConfigError` com o nome do segredo, sem
causa nem contexto encadeados. A chave do cursor (`LICENCAS_CURSOR_MAC_KEY`) **não** vai para o gateway (§2.3).

Loopback: `LicencasClient(config, loopback_test_port=<porta>)` troca o destino por `http://127.0.0.1:<porta><pathname
assinado>`. É um argumento do construtor. `LicencasClient.from_env()` não o aceita, nenhuma variável de ambiente nem chave do YAML o
alcança e ele não é campo da configuração. Existe só para a prova de interoperabilidade local.

## Contextos

O escopo (`principal_id`, `instance_id`, `policy_id`, `group_jid`, `policy_epoch`, `account_binding_hash`) vem sempre
da configuração. O resto vem do chamador, que só pode passar valores do transporte trusted (bridge A1), nunca texto
de conversa nem saída de modelo. O pedido leva 17 chaves; a Edge acrescenta `request_id` e `operation`.

| Tipo | Operações | Entrada | No contexto |
|---|---|---|---|
| Ator | `ingestEnvelope`, `recordReply`, `queryFollowups` | `ActorContext(actor_jid, actor_display_name, wa_message_id, envelope_id, message_timestamp, observed_at, membership, phone)` | `actor_is_current_member: true`, `member_checked_at` do atestado; telefone só com `PhoneAttestation` |
| Worker | `listCaptured`, `claimDue`, `prepareIntent`, `confirmReceipt` | `WorkerContext(observed_at)` | campos de ator, mensagem, envelope, membership e telefone `null`; `actor_is_current_member: false` |

- `MembershipAttestation(is_current_member=False, …)` é recusado **localmente** (`membership_not_attested`), sem pedido.
- O atestado do responsável (`OwnerMembership`) vai em `payload.owner_membership` do `prepareIntent`, nunca no contexto.
- Passar o tipo de contexto errado para uma operação é recusa local.

## As 7 operações

| Método | Operação | Payload enviado (chaves exatas) | Retorno tipado |
|---|---|---|---|
| `ingest_envelope(actor, envelope)` | `ingestEnvelope` | `{envelope:{envelope_id, instance_id, group_jid, actor_jid, wa_message_id, message_timestamp, received_at, text, text_truncated, quote_message_id, quote_actor_jid, attachment_metadata:{media_type,size_bytes}\|null}}` — identidade copiada do contexto, instância e grupo da configuração | `IngestReceipt(outcome: captured\|already_captured, event_id, envelope_id, processing_state: captured\|applied\|needs_clarification, identity_state)` |
| `record_reply(actor, ApplyReply(correlation, patch))` | `recordReply` | `{envelope_id, disposition:{kind:"apply"}, correlation:{quote_message_id?, token?}, patch:{operational_status?, next_action?, next_action_due_at?, blocker_summary?, evidence_summary?, next_contact?}}` — pelo menos uma chave em cada um | `AppliedReceipt(outcome: applied\|already_applied, document_id, followup_id, event_ids, revision, agenda_effect, current_next_contact, recorded_at, envelope_id, identity_state)` |
| `record_reply(actor, NeedsClarification(reason))` | `recordReply` | `{envelope_id, disposition:{kind:"needs_clarification", reason}}` | `ClarificationReceipt(outcome: needs_clarification\|already_needs_clarification, envelope_id, event_id, processing_state, reason, recorded_at)` |
| `query_followups(actor, filter, limit=, cursor=)` | `queryFollowups` | `{filter:{document_id?, operational_status?, risk_level?}, limit: 1..50, cursor: texto\|null}` | `FollowupPage(items: (FollowupItem × 10 campos), next_cursor)` |
| `list_captured(worker, limit=, cursor=)` | `listCaptured` | `{limit: 1..50, cursor: texto\|null}` | `CapturedPage(items: (CapturedItem × 9 campos), next_cursor)` |
| `claim_due(worker, limit=, lease_seconds=)` | `claimDue` | `{limit: 1..20, lease_seconds: 10..120}` | `ClaimResult(claims: (Claim(job_id, attempt_id, fence, lease_until, followup_id, revision, epoch, owner_jid\|None)), next_window_at\|None)` |
| `prepare_intent(worker, attempt, owner_membership)` | `prepareIntent` | `{job_id, attempt_id, fence, owner_membership:{owner_jid, is_current_member, checked_at}\|null}` | `PreparedIntent(attempt_id, fence, lease_until, send_before, intent_hash, group_jid, mention_jid\|None, text, correlation_token)` |
| `confirm_receipt(worker, attempt, ConfirmedReceipt(message_ids, accepted_at, intent_hash))` | `confirmReceipt` | `{job_id, attempt_id, fence, receipt:{state:"confirmed", message_ids, accepted_at, account_binding_hash, group_jid, intent_hash}}` — conta e grupo da configuração | `ReceiptConfirmation(outcome: confirmed\|already_confirmed, state: confirmed, event_id, message_ids)` |
| `confirm_receipt(worker, attempt, DeliveryUnknownReceipt(reason))` | `confirmReceipt` | `{…, receipt:{state:"delivery_unknown", reason}}` | `ReceiptConfirmation(outcome: recorded_unknown, state: delivery_unknown, event_id, message_ids: ())` |

Regras de leitura da resposta (qualquer desvio é `LicencasResponseError`, nunca sucesso nem `dict` cru):

- JSON estrito: UTF-8, sem BOM, sem NaN/Infinity, sem chave duplicada; chaves exatas em todo objeto; inteiro é `int`
  (booleano e fração recusados); enums fechados; instantes na gramática da Edge, com data existente.
- Conferências cruzadas iguais às da Edge: `envelope_id` e `reason` iguais aos do pedido; recibo do ramo pedido
  (apply não aceita recibo de esclarecimento e vice-versa); itens ≤ `limit`; `claims` sem `job_id`/`attempt_id`
  repetido e com `epoch` da configuração; `next_window_at` só com `claims` vazio; `attempt_id`, `fence` e `group_jid`
  do `prepareIntent` iguais aos do pedido/configuração; `send_before ≤ lease_until`; `text` não vazio;
  `message_ids` do recibo confirmado idênticos e na mesma ordem.
- `current_next_contact`: `{state:"none"}` vira `None`; `{state:"scheduled", job_id, due_at}` vira `ScheduledContact`.

## Erros tipados

Todo erro é `LicencasError` com `operation`, `status`, `code`, `request_id`, `detail` (caminho técnico, nunca valor) e
`action`. A exceção do `httpx` não fica encadeada (ela carrega o pedido e os cabeçalhos).

| Situação | Classe | `action` | O que o chamador pode fazer |
|---|---|---|---|
| 503 `busy` | `LicencasBusyError` | `RETRY_SAME_ATTEMPT` | Chamar de novo com as mesmas identidades de domínio (envelope, tentativa, fence) |
| 409 `replay_unconfirmed` | `LicencasReplayUnconfirmedError` | `RESEND_FRESH_AUTH` | Chamar de novo; o cliente gera timestamp, nonce e request_id novos |
| 429 | `LicencasRateLimitedError` | `BACK_OFF` | Recuar |
| 400, 401, 403, 404, 413 e demais 409 | `LicencasRejectedError` | `DO_NOT_RETRY` | Não repetir igual; decidir pelo `code` (matriz do §1.5) |
| 503 com outro código | `LicencasUnavailableError` | `UNAVAILABLE` | Próxima passada. Não assumir efeito nem ausência de efeito |
| Conexão não estabelecida | `LicencasNotSentError` | `BACK_OFF` | Nada saiu; recuar |
| Timeout ou erro de rede depois de enviar | `LicencasOutcomeUnknownError` | `OUTCOME_UNKNOWN` | Resultado desconhecido |
| Resposta fora do contrato, redirect, status fora do conjunto, corpo acima de 2 MiB | `LicencasResponseError` (subclasse da anterior) | `OUTCOME_UNKNOWN` | Resultado desconhecido |
| Entrada fora do codec, NaN, corpo > 16 KiB, membership não atestada | `LicencasRequestError` | `DO_NOT_RETRY` | Nada saiu; é erro do chamador |
| Configuração ausente ou inválida | `LicencasConfigError` | `DO_NOT_RETRY` | Integração fechada |

## Log

Uma linha por chamada, no logger `gateway.licencas_client`:
`licencas_http operation=<op> status=<n|none> code=<código> request_id=<uuid|none>`. Nada além disso. Campos sensíveis
(`capability`, `hmac_key`, `text`, `correlation_token`, `token`) ficam fora do `repr` dos tipos.

## Cenários (rastreabilidade contrato → teste em `tests/gateway/test_licencas_client.py`)

| # | Contrato | Cenário | Teste |
|---|---|---|---|
| 1 | §5 bytes assinados | Dado o vetor calculado com `openssl` fora do cliente, a assinatura é igual | `test_signature_known_vector` |
| 2 | §5 cabeçalhos e corpo | Para cada operação (9 formas), cabeçalhos exatos, HMAC recalculado sobre os bytes enviados, corpo byte a byte, sem `Authorization`/`apikey`/cookie | `test_headers_and_body_are_exact` |
| 3 | DTOs da Edge | Cada operação devolve o tipo fechado esperado | `test_typed_result`, `test_claim_due_closed_window_and_null_owner` |
| 4 | §5 contexto de ator | 17 chaves, atestado e telefone do transporte | `test_actor_context_has_the_17_keys_and_phone_attestation` |
| 5 | §5/B2 contexto de worker | Campos de ator `null`, `actor_is_current_member:false` nas 4 operações | `test_worker_context_has_no_actor` |
| 6 | D4 | Atestado do responsável só no payload; `null` é enviado, nunca omitido | `test_owner_membership_goes_in_payload_never_in_context` |
| 7 | §7 denies locais | Sem atestado positivo, zero pedidos | `test_membership_not_attested_is_refused_locally` |
| 8 | §5 status / §1.5 | 18 pares status+código → classe e `action`; um pedido por chamada | `test_typed_error_by_status_and_code` |
| 9 | §5 retry após timeout | Timeout e erro de rede → desconhecido; conexão recusada → não enviado; sem encadear a exceção do `httpx` | `test_transport_failures` |
| 10 | §5 redirects | 301/302/307/308 recusados e não seguidos | `test_redirect_is_refused_not_followed` |
| 11 | §5 conjunto fechado | Status fora do conjunto e corpo de erro fora de `{"error": código}` → desconhecido | `test_status_outside_closed_set_is_unknown`, `test_error_body_outside_contract_is_unknown` |
| 12 | DTOs estritos | Chave extra, faltante, tipo errado e não-objeto em cada operação, mais 30 desvios específicos | `test_malformed_response_is_a_typed_error` |
| 13 | §5 JSON estrito | Resposta com chave duplicada, NaN, BOM, lixo, UTF-8 inválido | `test_response_json_is_strict` |
| 14 | §5 16 KiB e NaN | 26 recusas locais com zero pedidos; 16 384 bytes passam, 16 385 não | `test_refused_before_sending`, `test_largest_accepted_body_is_exactly_16_kib` |
| 15 | §5 configuração | Tipo fechado; segredo ausente ou vazio; opções públicas inválidas (http, query, fragmento, credencial, outro caminho…) e segredos inválidos (JWT, chave curta…); construção direta também valida | `test_config_loads_into_closed_type`, `test_missing_or_empty_secret_fails_closed`, `test_invalid_public_option_fails_closed`, `test_invalid_secret_fails_closed`, `test_config_type_validates_direct_construction`, `test_default_reader_is_the_process_environment` |
| 15b | P1 PR #5 config.yaml | `config.yaml` real (HERMES_HOME temporário, YAML sintético) é a única fonte das nove opções, com ambiente conflitante e leitor espião só com os dois segredos; opção ausente/vazia/nula, `policy_epoch` não-`int` ou fora de 1..2^53−1, `enabled` não booleano, chave desconhecida, segredo no YAML, YAML inválido/duplicado/alias/merge, arquivo ausente ou ilegível, `raw_config` inválido ou `None` falham fechado **antes** de ler credencial; `raw_config` não toca o disco; perfil base ignora override; leitor que falha é sanitizado; erros e logs sem valores públicos | `test_loader_reads_the_real_config_yaml_and_only_the_two_secrets`, `test_from_env_reads_yaml_even_with_conflicting_environment`, `test_yaml_missing_or_empty_option_fails_before_credentials`, `test_yaml_policy_epoch_must_be_a_strict_int`, `test_yaml_policy_epoch_accepts_the_range_edges`, `test_yaml_values_keep_every_existing_validator`, `test_invalid_raw_config_structure_fails_closed`, `test_explicit_none_raw_config_never_falls_back_to_disk`, `test_raw_config_injection_does_not_touch_the_disk`, `test_yaml_file_that_is_not_one_explicit_section_fails_closed`, `test_missing_config_file_and_io_error_fail_closed`, `test_base_profile_ignores_a_context_override`, `test_secret_key_in_yaml_is_refused_without_echoing_it`, `test_unknown_yaml_key_is_refused`, `test_credential_reader_failures_are_sanitized`, `test_config_errors_and_logs_never_carry_public_values` |
| 16 | §5 transporte | Sem redirect, sem `trust_env`, TLS com verificação e hostname, timeouts explícitos, proxy do ambiente ignorado, cookies nunca guardados | `test_http_client_is_locked_down`, `test_proxy_environment_is_ignored`, `test_cookies_are_never_stored_or_sent` |
| 17 | prova local | Loopback só pelo argumento explícito | `test_loopback_only_through_explicit_test_parameter` |
| 18 | §5 anti-replay | Nonce (≥128 bits) e request_id diferentes em 25 pedidos; timestamp do relógio a cada pedido | `test_nonce_and_request_id_are_fresh_on_every_request`, `test_timestamp_comes_from_the_clock_at_each_request` |
| 19 | §5 sigilo | 63 chamadas (sucesso, resposta malformada, 409, 503, timeout, conexão, redirect): log só com os 4 campos, nenhum dado sensível em nenhum logger | `test_log_has_only_operation_status_code_and_request_id`, `test_local_refusal_log_has_no_request_id_or_values`, `test_sensitive_fields_are_hidden_from_repr` |
| 20 | §5 item 2 | Importar não carrega agente, modelo, cron, sessão nem ferramentas, não escreve nada e não instala handler | `test_import_has_no_side_effects`, `test_module_source_has_no_literal_endpoint_or_file_access` |
| 21 | limite de resposta | Corpo acima de 2 MiB recusado | `test_oversized_response_is_refused` |

## Evidência (números reais desta execução; `/home/clawd/licencas-hermes-evidence/a2-client/`)

| Check | Resultado | Log |
|---|---|---|
| RED (módulo inexistente) | erro de coleta `ModuleNotFoundError` | `red.log` |
| `pytest tests/gateway/test_licencas_client.py` | 228 passaram, 0 falharam | `green.log` |
| `pytest tests/gateway/test_whatsapp_group_gating.py` (regressão) | 26 passaram | `green.log` |
| `ruff check` e `ruff format --check` nos 2 arquivos Python | limpos | `ruff.log` |
| Interoperabilidade com o handler real da Edge | 25 PASS, 0 FAIL | `interop/interop.log` |
| Vetor da assinatura (`openssl`) | igual ao do cliente | `signature-kat-openssl.txt` |
| Sweep antes e depois | sem achado | `sweep-pre.log`, `sweep-post.log` |

### Interoperabilidade (fora do repositório)

`interop/edge_server.ts` importa, só para leitura, `createLicencasHandler`, `loadLicencasConfig` e `importCursorKey`
do `contract.ts` real e serve em `127.0.0.1` numa porta livre. A configuração é sintética e gerada a cada execução.
A fronteira RPC é **falsa**: registra cada chamada e devolve DTOs válidos. `interop/interop_client.py` usa o cliente
novo. Isto prova que os dois lados concordam em autenticação, contexto, codecs e DTOs. **Não** prova o banco, as
RPCs SQL, a Edge implantada, a rede nem o TLS.

| Operação | Resultado |
|---|---|
| `ingestEnvelope` | PASS: 200, `authorize` + `ingest_license_whatsapp_envelope`, `IngestReceipt` |
| `recordReply` | PASS nos dois ramos (apply e needs_clarification) |
| `queryFollowups` | PASS: 1ª página e 2ª página com o cursor selado pela Edge real |
| `listCaptured` | PASS: 1ª página e 2ª página com o cursor selado pela Edge real |
| `claimDue` | PASS: com tentativa e com janela fechada (`next_window_at`) |
| `prepareIntent` | PASS: com atestado em `p_owner_membership` (contexto de worker) e com `null` |
| `confirmReceipt` | PASS: `confirmed` e `delivery_unknown` |
| Assinatura adulterada | PASS: 401 `unauthenticated`, zero RPC |
| Payload fora do codec | PASS: 400 `invalid_request`, zero RPC (pelo cliente e com corpo assinado à mão) |
| Membership velha (5 min) | PASS: 403 `membership_not_attested`, zero RPC |
| Replay idêntico | PASS: 409 `replay_unconfirmed` → `LicencasReplayUnconfirmedError`, domínio não roda |
| `55P03 busy` / `53400` | PASS: 503 `busy` → `LicencasBusyError`; 429 → `LicencasRateLimitedError` |

Cada chamada positiva também confere, no registro da fronteira falsa: 2 RPCs (authorize + a de domínio), nomes dos
parâmetros na ordem do mapa literal, `operation` do contexto, 19 chaves no contexto e tipo de contexto correto.

## Decisões do executor (onde o contrato não fixava)

1. **Pathname assinado separado do endpoint** (`LICENCAS_API_SIGNED_PATHNAME`). A Edge assina e confere o pathname
   fixo `/licencas-api`, que é o que a função enxerga. A URL pública de uma Edge function tem prefixo de plataforma
   antes dele. O cliente exige que o caminho do endpoint **termine** no pathname assinado. Precisa ser confirmado
   contra a Edge implantada (gate de ativação, não desta unidade).
2. **Nomes de variável** iguais aos da Edge para os nove valores compartilhados, mais as duas do endpoint.
3. **503 que não é `busy`** ganhou classe própria (`UNAVAILABLE`). Alguns códigos (`invalid_rpc_response`,
   `internal_error`, `unavailable`) podem sair depois de a RPC de domínio ter rodado, então o cliente não afirma
   "sem efeito".
4. **Conexão não estabelecida** é `LicencasNotSentError`, distinta de "resultado desconhecido".
5. **Resposta malformada com status 200** é resultado desconhecido: o efeito no banco provavelmente aconteceu.
6. **Contexto de worker**: `message_timestamp` recebe o mesmo instante de `observed_at` (não há mensagem).
7. **Membership não atestada é recusa local**, sem pedido (§7: zero RPC nos denies locais).
8. **Validação de entrada antes de enviar** com os mesmos formatos da Edge. O cliente é mais restrito em três
   pontos: exige pelo menos uma chave no patch e na correlação (regra do SQL, §1.4), confere a forma do cursor e
   não aceita `1.0` como inteiro nas respostas.
9. **Frescor não é conferido no cliente** (atestado ≤ 30 s, instantes no máximo 5 s no futuro, timestamp ±60 s):
   quem decide é a Edge, com o relógio dela.
10. **Limite de 2 MiB na resposta** e **timeouts fixos** em constantes do módulo.
11. **Nonce de 32 bytes** (o contrato pede pelo menos 16).

## O que A2-pregate e A2-consumer precisam saber

- **Criação**: `LicencasClient.from_env(<leitor dedicado dos dois segredos>)` uma vez, no start gerenciado (o nome `from_env` é histórico: as nove opções vêm do `config.yaml`); `await client.aclose()` no stop.
  `LicencasConfigError` fecha só a integração protegida. Nunca cair no caminho legado por falta de configuração.
- **Importar `gateway.licencas_client`** não lê ambiente nem abre rede. O `gateway/__init__.py` existente já importa
  `config`, `session` e `delivery`; isso é anterior a esta unidade.
- **Logger `httpx`**: a biblioteca emite, em INFO, uma linha própria com método e URL do endpoint (sem cabeçalho nem
  corpo). O `hermes_logging` já mantém `httpx` em WARNING; não baixar esse nível.
- **Contexto de ator**: `actor_jid`, `wa_message_id`, `envelope_id`, `message_timestamp`, nome exibido e telefone vêm
  do envelope A1; `membership` vem da consulta de participantes à bridge feita **agora** (a Edge recusa atestado com
  mais de 30 s). `observed_at` é o relógio do gateway. Nada disso pode vir de texto ou de modelo.
- **Envelope A1 de outro escopo**: o cliente preenche `instance_id` e `group_jid` com a configuração. Conferir conta
  e grupo do envelope A1 contra a configuração **antes** de chamar é obrigação do consumidor.
- **Texto grande**: o corpo tem teto de 16 KiB e o padrão de A1 é 8 192 caracteres. Envelope que não cabe levanta
  `LicencasRequestError(code="payload_too_large")` sem enviar. O consumidor precisa de um destino para esse caso
  (ver débito 1).
- **Ack do A1** só depois de `IngestReceipt` (`captured` ou `already_captured`). Em qualquer erro, não dar ack.
- **`recordReply`**: decidir pela matriz do §1.5 usando `error.status` e `error.code`. O cliente não converte recusa
  em esclarecimento por conta própria.
- **Paginação**: cada passada começa com `cursor=None` e segue `next_cursor` até `None`. O cursor é opaco, não é
  guardado entre passadas e não é interpretado. `invalid_cursor` (400) → recomeçar do `None`.
- **Cobrança**: `claim_due` → para cada `Claim`, conferir pela bridge se `owner_jid` é participante atual →
  `prepare_intent(worker, AttemptRef(...), OwnerMembership(...) ou None)` → conferir relógio < `send_before` → A1
  `/send` com o **mesmo** `attempt_id`, para `group_jid`, com `text` byte a byte → `confirm_receipt`. `mention_jid`
  `None` = não marcar ninguém.
- **Depois do `prepare_intent`**, qualquer `OUTCOME_UNKNOWN`/`UNAVAILABLE` ou prazo vencido termina em
  `confirm_receipt(DeliveryUnknownReceipt(...))` da mesma tentativa. Nunca novo envio nem novo `attempt_id`.
- **`ConfirmedReceipt.message_ids`**: tupla com os IDs reais de A1, 1 a 16, na mesma ordem em toda repetição.
- **`action`**: `RETRY_SAME_ATTEMPT` e `RESEND_FRESH_AUTH` significam "chame o mesmo método com os mesmos
  argumentos de domínio". O cliente nunca repete sozinho; o laço e o backoff são do consumidor.
- **Instantes** são texto ISO-8601 com fuso. `message_timestamp` tem de ser exatamente o texto do envelope A1.
- **`repr` dos DTOs** esconde texto e tokens, mas JIDs e IDs aparecem. Não logar DTOs.

## Débito registrado (P2/P3; não bloqueia, regra D0)

| # | Grau | Débito | Dono | Risco |
|---|---|---|---|---|
| 1 | P2 | Teto de 16 KiB do corpo contra o limite de texto de A1 (8 192 caracteres por padrão): mensagem longa em UTF-8 de vários bytes não cabe no `ingestEnvelope`. Falta decidir se A1 trunca antes (`WHATSAPP_DURABLE_MAX_TEXT_CHARS` menor, com `text_truncated`) ou se o consumidor grava tombstone | A2-consumer | Envelope reentregue para sempre sem captura |
| 2 | P2 | Pathname assinado (`/licencas-api`) contra a URL pública real não foi provado com a Edge implantada | Gate de ativação | 401 em todo pedido até acertar a configuração |
| 3 | P3 | `receipt_reconcile` não é operação HTTP (lacuna já registrada na B2); o cliente não tem método para ela | B2/Edge | Reconciliação após revogação só por SQL trusted |
| 4 | P3 | Frescor não é conferido localmente; atestado velho custa um pedido e volta 403 | A2-consumer | Pedido desperdiçado, sem efeito |
| 5 | P3 | Linha INFO do logger `httpx` traz a URL do endpoint se alguém baixar o nível desse logger | Operação | Host do projeto em log |
| 6 | P3 | `Content-Type` da resposta não é conferido (o corpo é validado por inteiro) | A2-client | Nenhum efeito conhecido |
| 7 | P3 | `repr` dos DTOs mostra JIDs e IDs de mensagem | A2-consumer | Vazamento se alguém logar o DTO |
| 8 | P3 | `PHASE.md` do repositório continua em `align`; esta unidade tem escopo fechado de 3 arquivos e não o altera | Coordenador | Protocolo de etapas sem registro desta fatia |
