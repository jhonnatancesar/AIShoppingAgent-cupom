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

- **Varredura por loja**, não por missão/produto/oferta.
- Fontes das 4 lojas: **Amazon e Kabum** nos cards da busca; **Magalu e
  Mercado Livre** na home (onde o cupom aparece).
- **Regra de evidência:** só persiste cupom quando há evidência literal no
  texto capturado (cupom explícito no card/página ou regra promocional oficial
  publicada). **Nunca inventa, nunca presume.**
- Cotação/parsing apenas de **texto já capturado** (código, %, valor).
- **Fora de escopo:** preço/oferta, auto-aplicar cupom, agregadores de terceiros,
  cupom exclusivo de conta, histórico, evasão anti-bot.

## Cadência (America/Sao_Paulo, por relógio)

| Modo | Cadência |
|------|----------|
| normal | a cada **1h**, em **HH:00** (da meia-noite em diante) |
| promotion | a cada **30min**, em **HH:00 e HH:30** |

O modo é decidido por *agora dentro da janela promocional*
(`window_start <= now < window_end`). Quando a janela acaba, o worker **volta
sozinho** ao normal. Se reiniciar no meio de uma promoção, recupera o estado
persistido e continua na cadência certa. Nada de intervalos arbitrários.

## Estrutura

```
install.ps1                     # venv, dependencias, .env -- Windows
manage_coupon_worker_task.ps1   # instala/gerencia a Scheduled Task (Install/Status/Remove/...)
requirements.txt                # playwright (so o driver, sem navegador gerenciado), aiohttp, python-dotenv, tzdata
config.json                     # lojas, termos, URLs, selectores, tempos, porta CDP do Edge (SEM senha)
.env.example                    # modelo do AUTH_TOKEN
worker.py                       # loop principal (cadência + varredura single-flight)
cadence.py                      # política normal/promo + próximo slot
control_server.py               # POST /control/promo (Bearer token, fail-closed)
coupons/__init__.py
coupons/edge_transport.py       # sobe/derruba o Edge dedicado (CDP) por rodada, sem depender do repo principal
coupons/scanner.py              # Edge+Playwright: navega, detecta bloqueio, extrai
coupons/evidence.py             # parsing literal de codigo/valor/% por loja
coupons/persistence.py          # CouponStore (SQLite) + modelo Coupon da TASK-106
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

## Próximo passo (quando você informar o banco)

- `PostgresCouponStore` com a mesma interface `CouponStore`, apontando para o
  Postgres do AIShoppingAgent (`stores.id` / `coupons`). Nenhum outro módulo
  muda — só o factory de persistência.
- Fase de **aplicabilidade** e **notificação** ficam do lado do backend (não
  neste worker), preservando a separação da DEC-093.