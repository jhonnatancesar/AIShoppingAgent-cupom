# Coupon Collector Worker (TASK-106)

Pesquisador de **cupons / códigos de desconto / promoções** para as lojas
monitoradas pelo GG Oferta. Roda no mesmo Windows Server da produção do
AIShoppingAgent, como processo **totalmente separado e autocontido**
(nenhuma dependência do repositório principal — pode ser removido/movido
para outra máquina sem tocar em mais nada), com **Microsoft Edge real +
Playwright** via CDP dedicado — é o Coupon Collector, **não** faz coleta
normal de preço/oferta.

> Histórico: a primeira versão deste worker rodava numa Raspberry Pi 3B+
> (Debian 13 trixie, arm64) com Firefox. Essa rota foi descartada — a
> partir desta versão o transporte é sempre Edge/Windows.

## O que ele faz (escopo, DEC-093 / TASK-106)

- **Varredura por loja**, não por missão/produto/oferta, sempre **em
  fila** (uma loja de cada vez, nunca em paralelo).
- 4 lojas: Amazon, Kabum, Magalu, Mercado Livre — cada uma com fontes
  próprias (home, área/página de cupons, carrossel, busca) apontando
  pros elementos REAIS de cupom de cada site (nunca seletor genérico de
  produto), confirmadas por inspeção ao vivo do DOM real, não suposição.
- **Regra de evidência:** só persiste cupom quando há evidência literal no
  texto capturado (cupom explícito no card/página ou regra promocional
  oficial publicada). **Nunca inventa, nunca presume** — inclusive pra
  campos estruturados extras quando a página os declarar (valor mínimo de
  compra, teto de desconto, hint de "está esgotando"/"vence X").
- Cotação/parsing apenas de **texto já capturado** (código, %, valor).
- Ritmo humano/respeitoso entre interações e cooldown maior na troca de
  loja (configurável, ver abaixo) — **nunca uma técnica de evasão
  anti-bot**, só não bater as fontes rápido demais; sem pressa nenhuma,
  a cadência já é de 1h/30min.
- **Fora de escopo:** preço/oferta, auto-aplicar cupom, agregadores de
  terceiros, cupom exclusivo de conta/login (a menos que uma sessão
  autenticada seja fornecida manualmente, ver "Login manual" abaixo),
  histórico formal de preço, qualquer técnica de evasão anti-bot
  (fingerprint spoofing, stealth, resolver captcha).

## Cadência (America/Sao_Paulo, por relógio)

| Modo | Cadência |
|------|----------|
| normal | a cada **1h**, em **HH:00** (da meia-noite em diante) |
| promotion | a cada **30min**, em **HH:00 e HH:30** |

O modo é decidido por *agora dentro da janela promocional*
(`window_start <= now < window_end`). Quando a janela acaba, o worker **volta
sozinho** ao normal. Se reiniciar no meio de uma promoção, recupera o estado
persistido e continua na cadência certa. Nada de intervalos arbitrários.

## Ritmo entre interações (`config.json` → `scanner`)

| Opção | Padrão | O que faz |
|---|---|---|
| `delay_between_requests_seconds` | 5 | Pausa antes de cada navegação (fonte, produto aprofundado, candidato verificado) dentro da MESMA loja |
| `cooldown_between_stores_seconds` | 20 | Pausa maior, só na troca de loja |
| `scroll_steps_before_cards` / `scroll_pause_seconds` | 4 / 1.5 | Rola a página antes de extrair cards, pra carregar carrosséis com lazy-load (achado real: só 2 de 23 cards de cupom do Mercado Livre apareciam sem rolar) |

Tudo configurável, pode aumentar à vontade — nunca é evasão, é ritmo
respeitoso.

## Auto-configuração: descobre e adota fontes de cupom novas sozinho

Quando uma loja abre uma área/aba/botão de cupom que ainda não está
configurada (ex.: o badge **"AQUI TEM 9.9"** do Mercado Livre, que só
existe durante uma campanha sazonal), o worker **não fica só avisando** —
ele mesmo confirma e passa a usar:

1. Em toda página de nível de loja (home/coupons/banners), procura links
   curtos (até 60 caracteres) cujo texto ou `href` contenha "cupom",
   "liquida", "promoção" ou um padrão tipo "9.9"/"11.11" — excluindo
   links de produto individual (`/dp/`, `/produto/`, `/p/MLB` etc.).
2. Um candidato novo é **visitado na mesma rodada**, numa aba própria, e
   passa pela MESMA extração de evidência (`build_coupons`) usada em
   qualquer fonte normal.
3. Só se achar cupom real de verdade o candidato é **adotado**
   (`source_candidates.status = 'adopted'` no SQLite) — a partir daí,
   essa URL é escaneada automaticamente **toda rodada futura**, junto
   das fontes de `config.json`, sem precisar editar nada.
4. Sem evidência, fica `rejected` mas continua elegível pra
   reverificação nas próximas rodadas (a promoção pode ainda não ter
   começado — não é uma rejeição definitiva).

**Identidade por texto do badge, não por URL:** a mesma promoção pode
gerar um link de **rastreamento** diferente a cada carregamento de
página (achado real: `click1.mercadolivre.com.br/.../count?a=<token>`
mudando toda vez). Por isso o candidato é identificado pelo texto do
botão/badge (estável), e a URL guardada ao adotar é o **destino final
já resolvido** (depois de seguir o redirecionamento de verdade), nunca
o link frágil original. Se o destino resolvido bater com uma fonte já
configurada manualmente, não duplica.

Validado ao vivo: descobriu e adotou sozinho, em uma única rodada, o
badge "9.9" (resolveu pra exatamente a mesma URL configurada
manualmente) e uma página de outlet nunca vista antes
(`lista.mercadolivre.com.br/_Container_outlet-full`, com cupom real).

## Cupom que some vira "expirado"; hints de status são capturados

- **Expiração por ausência:** se um cupom `active` não é confirmado
  (upsert) numa rodada inteira que completou sem bloqueio/erro, ele
  sumiu da loja — `expire_stale()` marca `status = 'expired'`
  automaticamente. Comparação real de histórico no banco, nunca
  inferência/IA.
- **Hint de status/validade:** quando a página escreve literalmente
  "Está esgotando!", "Vence amanhã", "Válido até X", esse texto é
  capturado e anexado ao `raw_rule_text` da evidência — genérico, vale
  pra qualquer loja. Nunca calcula data relativa nem decide um status
  estruturado sozinho, só preserva a evidência literal.

## Login manual (sessão autenticada, conta de pesquisa)

Algumas páginas de cupom (ex.: `/cupons` do Mercado Livre) exigem estar
logado. O Coupon Collector **nunca** digita credencial nenhuma — em vez
disso, `login_manual.py` abre o mesmo perfil dedicado do Edge que o
worker usa (`%TEMP%\aishopping-coupon-edge-profile`) numa URL pública,
pra você logar manualmente com uma **conta de pesquisa** (nunca a
pessoal):

```powershell
.venv\Scripts\python.exe login_manual.py                                   # abre o Mercado Livre
.venv\Scripts\python.exe login_manual.py https://www.magazineluiza.com.br/ # outra loja
```

A sessão (cookies) fica salva nesse perfil em disco e é reaproveitada
automaticamente pelo worker daí em diante — o scanner reutiliza sempre o
**contexto padrão** do Edge (`browser.contexts[0]`), nunca
`browser.new_context()` (que criaria um contexto isolado tipo anônimo,
sem os cookies do perfil — achado real que fazia o login nunca "aparecer"
pro scanner antes dessa correção).

## Estrutura

```
install.ps1                     # venv, dependencias, .env -- Windows
manage_coupon_worker_task.ps1   # instala/gerencia a Scheduled Task (Install/Status/Remove/...)
login_manual.py                 # abre o perfil dedicado numa URL publica pra login manual (sem digitar credencial)
requirements.txt                # playwright (so o driver, sem navegador gerenciado), aiohttp, python-dotenv, tzdata
config.json                     # lojas, termos, URLs, selectores, ritmo/cooldown/scroll, porta CDP do Edge (SEM senha)
.env.example                    # modelo do AUTH_TOKEN
worker.py                       # loop principal (cadência + varredura single-flight)
cadence.py                      # política normal/promo + próximo slot
control_server.py               # POST /control/promo (Bearer token, fail-closed)
coupons/__init__.py
coupons/edge_transport.py       # sobe/derruba o Edge dedicado (CDP) por rodada, sem depender do repo principal
coupons/scanner.py              # Edge+Playwright: navega, detecta bloqueio, extrai, descobre/adota fontes novas
coupons/evidence.py             # parsing literal de codigo/valor/%/campos extras, real por loja (nao suposicao)
coupons/persistence.py          # CouponStore (SQLite): coupons + source_candidates (descoberta/adocao)
coupons/stores.py               # URLs de busca/home por loja
README.md
```

## Instalação no Windows Server

Copie a pasta inteira (`C:\AIShoppingAgenteCupom`) para o Windows Server.
Depois, no servidor:

```powershell
cd C:\AIShoppingAgenteCupom
powershell -File install.ps1
```

O script faz:
1. Confirma que o Microsoft Edge está instalado no caminho padrão do
   Windows — **nenhum navegador é baixado**, sempre usa o Edge real já
   instalado.
2. `.venv` + `pip install -r requirements.txt`.
3. Gera `.env` com `AUTH_TOKEN` aleatório (se não existir; não sobrescreve).
4. Valida a cadência (próximos slots calculados).

Depois, registre a Scheduled Task — o usuário precisa ser o mesmo da
sessão com auto-logon: o Edge dedicado precisa de sessão interativa real
(tela bloqueada é suportada, sessão deslogada não — mesmo requisito já
documentado para o `collection_worker` principal do AIShoppingAgent):

```powershell
powershell -File manage_coupon_worker_task.ps1 -Action Install -TaskUser "DOMINIO\Usuario"
```

## Comandos úteis

```powershell
powershell -File manage_coupon_worker_task.ps1 -Action Status    # estado
Start-ScheduledTask -TaskName "AIShoppingCoupon-Worker"          # iniciar/reiniciar
powershell -File manage_coupon_worker_task.ps1 -Action Remove    # desinstalar a tarefa
```

**Restart automático:** a tarefa usa o restart nativo do Task Scheduler
(`RestartCount`/`RestartInterval`) — funciona para o processo terminar
sozinho com erro, mas **não** para `Stop-Process -Force` externo (mesmo
achado já documentado no `collection_worker` principal). Este worker não
tem um supervisor externo dedicado — proporcional ao seu risco/frequência,
bem menor que o worker de coleta de preço.

## Endpoint de controle

`POST http://127.0.0.1:8090/control/promo` (host/porta em `config.json`).
Autenticação **obrigatória**: `Authorization: Bearer <AUTH_TOKEN>` do `.env`.
Sem token configurado, o servidor **não sobe** (fail-closed).

**Chamador automático (GG Oferta, desde `v1.3.10`):** além do uso manual
abaixo, o scheduler de coleta do GG Oferta (`claim_due_work`) chama este
endpoint sozinho, best-effort, sempre que decide `HIGH_ACTIVITY` para uma
loja/escopo (`app/coupons/worker_control.py` no repositório GG Oferta,
`window_end` = agora + `AISHOPPING_COLLECTION_HIGH_ACTIVITY_DURATION_MINUTES`).
Só dispara se `AISHOPPING_COUPON_WORKER_CONTROL_URL` e
`AISHOPPING_COUPON_WORKER_CONTROL_TOKEN_FILE` estiverem configurados do
lado do GG Oferta (`None`/desligado por padrão); nunca lança exceção, só
loga aviso se o Coupon Worker estiver fora do ar ou rejeitar o token —
uma falha aqui nunca derruba a coleta de preço. O uso manual via `curl`
abaixo continua válido para forçar uma janela de promoção fora do que o
GG detecta sozinho.

Entrar em promoção (datas em ISO com timezone):

```bash
curl -s -X POST http://127.0.0.1:8090/control/promo \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"promotion","window_start":"2026-08-29T14:00:00-03:00","window_end":"2026-08-29T20:00:00-03:00"}'
```

Voltar ao normal:

```bash
curl -s -X POST http://127.0.0.1:8090/control/promo \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"normal"}'
```

Respostas: `200` confirma o estado; `400` = modo inválido / janela com
`end <= start`; `401` = token ausente/errado.

## Smoke test

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # defina o AUTH_TOKEN
.venv\Scripts\python.exe smoke_test.py --stores kabum   # ou amazon,kabum,magalu,mercadolivre
```

Não precisa de `playwright install` — nenhum navegador é baixado, o
smoke test usa o Edge real já instalado no Windows (`coupons/edge_transport.py`
descobre o caminho automaticamente).

`smoke_test.py` imprime o resumo da varredura por loja e usa um banco SQLite
temporário isolado por execução. Pode não haver cupom (vazio = varredura ok;
sem evidência **não** grava cupom — isso é o comportamento correto).
`worker.py --once` faz o mesmo, mas grava em `data/worker.db` (persistente).

## Validação real (2026-08-30, sessão autenticada, DOM real inspecionado)

| Loja | Evidências reais persistidas numa rodada |
|---|---|
| Amazon | 18 |
| Kabum | 90 |
| Magalu | 24 |
| Mercado Livre | ~150-180 (carrossel + área de cupons + auto-descoberta) |

Todas as 4 lojas confirmadas achando cupom real, não só "varredura sem
erro". Detalhe completo de cada achado/correção nos commits do
histórico deste repositório.

## Persistência no PostgreSQL do GG Oferta (concluído, commit `caca098`)

`PostgresCouponStore` (`coupons/persistence.py`) implementa a mesma
interface `CouponStore`, apontando para o Postgres do AIShoppingAgent
(`stores.id` / `coupons`) via `psycopg[binary]` assíncrono, quando
`COUPONS_POSTGRES_DSN` está definido em `.env` — sem essa variável, o
worker continua gravando só em SQLite local (`data/worker.db`), e o
GG Oferta não vê os cupons coletados. Nenhum outro módulo deste worker
muda; só o factory de persistência escolhe qual `CouponStore` usar.
`evidence.py` também foi refinado nesse mesmo commit para distinguir
"esgotado" (marca expirado) de "está esgotando" (ambíguo, não decidido
automaticamente).

Fase de **aplicabilidade** (consumo do cupom no preço) e **notificação**
ficam do lado do GG Oferta (`app/coupons/pricing.py`), não neste worker
— preservando a separação da DEC-093. Guia de instalação dos três
componentes juntos (GG Oferta + César Core/OmniRoute + este worker):
[`docs/installation/integrated-setup.md`](https://github.com/jhonnatancesar/AIShoppingAgent/blob/main/docs/installation/integrated-setup.md)
no repositório `AIShoppingAgent`. Deploy em PROD dos três componentes
(repositórios, ordem, migrations, gate Gemini):
[`docs/operations/prod-deployment-handoff.md`](https://github.com/jhonnatancesar/AIShoppingAgent/blob/main/docs/operations/prod-deployment-handoff.md).
Arquitetura canônica da integração GG ↔ César Core (onde este worker se
encaixa como fonte de dado de cupom):
[`docs/architecture/gg-oferta-core.md`](https://github.com/jhonnatancesar/cesar-core/blob/main/docs/architecture/gg-oferta-core.md)
no repositório `cesar-core`.
