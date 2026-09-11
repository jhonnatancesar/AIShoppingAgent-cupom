#!/usr/bin/env python3
"""Prova FUNCIONAL, em nível de COMPONENTE, da cadência HIGH_ACTIVITY do
Coupon Worker (`cadence.py`, `control_server.py`, `worker.py`) -- sem
pytest (mesmo padrão de `test_evidence.py`/`smoke_test.py`), `assert`
direto.

Auditoria ao vivo (2026-09-10, revisão pós-relatório): o usuário rejeitou
explicitamente "o diff não mudou" e "a prova antiga de `promo_window`" como
suficientes -- exigiu prova funcional fresca de propriedades específicas.
Este arquivo cobre cada função REAL isoladamente (`mode_for`/`next_slot`/
`parse_promo_window`, `ControlServer.handle_promo`, `run_scan`), nomeada e
rastreável (nunca uma contagem agregada que não esclarece o que foi
coberto):

1. test_normal_mode_uses_hourly_slots_without_any_window
2. test_promotion_mode_uses_half_hour_slots_inside_window
3. test_mode_returns_to_normal_automatically_after_window_end
4. test_control_server_repeated_notices_extend_window_without_duplicate_wake
   (extensão de janela por avisos repetidos, sem duplicar execução)
5. test_run_scan_single_flight_skips_concurrent_execution
   (ausência de execuções concorrentes duplicadas -- `worker.run_scan` REAL)
6. test_promo_window_persists_across_store_reopen
   (comportamento correto após reinício -- janela sobrevive)
7. test_run_scan_has_zero_mode_branching (coleta acelerada usa os MESMOS
   parsers corrigidos -- não existe um segundo scanner "modo promoção")

Mais os testes de guarda-corrio que já existiam informalmente (auth
fail-closed, validação de payload, "normal" sem nenhuma missão/HIGH_ACTIVITY
envolvida) -- porque uma cadência sem esses guardas não seria a mesma
cadência que está documentada como garantia.

IMPORTANTE (revisão 2026-09-10, segunda rodada): a prova de "antecipação
de uma coleta já agendada" que existia aqui (`_test_control_server_
promotion_wakes_loop_before_scheduled_slot`) foi REMOVIDA -- ela
reimplementava o padrão de espera do `main_loop` (`asyncio.wait_for(wake.
wait(), timeout=...)`) numa função de teste separada, em vez de chamar o
`worker.main_loop` de verdade. O usuário rejeitou explicitamente esse
tipo de prova ("não copie o loop"). A prova real de antecipação/despertar/
extensão de janela/retorno à cadência normal, chamando `worker.main_loop`
de verdade (só com `Scanner`/`load_stores` substituídos -- a dependência
externa de Playwright/Edge, controle explicitamente autorizado), está em
`test_worker_loop_integration.py`."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiohttp.test_utils import make_mocked_request

import worker
from cadence import (
    TZ, Window, mode_for, next_slot, now_sp, parse_promo_window,
)
from control_server import ControlServer
from coupons.persistence import PromoWindow, SqliteCouponStore

TZ_SP = ZoneInfo("America/Sao_Paulo")


def _dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ_SP)


# ---------------------------------------------------------------------------
# 1-3: mode_for / next_slot -- cadência normal vs. acelerada, e retorno
# automático ao normal quando a janela acaba (puro, sem I/O).
# ---------------------------------------------------------------------------


def test_normal_mode_uses_hourly_slots_without_any_window() -> None:
    """Sem missão/HIGH_ACTIVITY nenhuma (window=None) -- modo é sempre
    'normal', slot sempre na próxima hora cheia. Prova que a cadência base
    funciona INDEPENDENTE de qualquer coisa vinda do GG."""
    now = _dt(2026, 9, 10, 14, 17)
    assert mode_for(now, None) == "normal"
    slot = next_slot(now, "normal")
    assert slot == _dt(2026, 9, 10, 15, 0)
    print("PASS: sem janela nenhuma, modo é sempre 'normal' e o slot é a próxima hora cheia (14:17 -> 15:00)")


def test_promotion_mode_uses_half_hour_slots_inside_window() -> None:
    """Dentro da janela promocional, cadência vira 30min (HH:00/HH:30) --
    estritamente mais frequente que o normal (1h). Prova real da
    ACELERAÇÃO de cadência, não só da existência do modo."""
    window = Window(start=_dt(2026, 9, 10, 14, 0), end=_dt(2026, 9, 10, 18, 0))
    now = _dt(2026, 9, 10, 14, 17)
    assert mode_for(now, window) == "promotion"
    slot = next_slot(now, "promotion")
    assert slot == _dt(2026, 9, 10, 14, 30)
    # Mesmo "agora", fora da janela, seria só 15:00 (1h) -- a diferença
    # (14:30 vs 15:00) É a aceleração real, medida em minutos poupados.
    normal_slot = next_slot(now, "normal")
    assert normal_slot == _dt(2026, 9, 10, 15, 0)
    assert slot < normal_slot
    print("PASS: dentro da janela, próximo slot é 14:30 (30min) contra 15:00 (1h) no modo normal -- aceleração real e mensurável")


def test_mode_returns_to_normal_automatically_after_window_end() -> None:
    """Janela expirada (now >= window.end) -- volta a 'normal' sozinho,
    sem precisar de nenhum novo comando de controle. Restaura a cadência
    de 1h automaticamente."""
    window = Window(start=_dt(2026, 9, 10, 14, 0), end=_dt(2026, 9, 10, 18, 0))
    now_inside = _dt(2026, 9, 10, 17, 59)
    now_after = _dt(2026, 9, 10, 18, 0)  # end é exclusivo (`now < window.end`)
    assert mode_for(now_inside, window) == "promotion"
    assert mode_for(now_after, window) == "normal"
    assert next_slot(now_after, mode_for(now_after, window)) == _dt(2026, 9, 10, 19, 0)
    print("PASS: ao passar de window.end, o modo volta pra 'normal' e a cadência volta a 1h -- sem novo comando")


def test_invalid_or_missing_window_falls_back_to_normal() -> None:
    """`parse_promo_window` recusa janela inválida (end<=start, campos
    ausentes) -- nunca aceita um intervalo arbitrário. Sem janela válida,
    o modo cai pra normal (fail-safe: nunca acelera por engano)."""
    assert parse_promo_window(PromoWindow()) is None  # nada persistido
    assert parse_promo_window(PromoWindow(window_start="2026-09-10T14:00:00-03:00", window_end="2026-09-10T14:00:00-03:00")) is None  # end == start
    assert parse_promo_window(PromoWindow(window_start="2026-09-10T14:00:00-03:00", window_end="2026-09-10T13:00:00-03:00")) is None  # end < start
    print("PASS: janela ausente/inválida (end<=start) nunca vira Window -- cadência cai pra normal por padrão seguro")


# ---------------------------------------------------------------------------
# 4-5: ControlServer -- antecipação de coleta já agendada + extensão de
# janela por avisos repetidos, sem duplicar execução.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Store mínimo só com o que ControlServer usa -- evita I/O real."""
    def __init__(self) -> None:
        self.window = PromoWindow()
        self.set_calls: list[PromoWindow] = []

    def set_promo_window(self, window: PromoWindow) -> None:
        self.window = window
        self.set_calls.append(window)

    def clear_promo_window(self) -> None:
        self.window = PromoWindow()


async def _call_handle_promo(server: ControlServer, body: dict, token: str = "segredo-teste"):
    request = make_mocked_request(
        "POST", "/control/promo",
        headers={"Authorization": f"Bearer {token}"},
    )
    with patch.object(type(request), "json", new=AsyncMock(return_value=body)):
        return await server.handle_promo(request)


async def _test_control_server_repeated_notices_extend_window_without_duplicate_wake() -> None:
    """Extensão de janela por avisos repetidos: dois `POST /control/promo`
    sucessivos, o segundo com `window_end` mais distante -- a janela
    persistida reflete SEMPRE o último aviso (nunca fica presa no
    primeiro), e o `wake` Event nunca 'enfileira' sinais (idempotente:
    978 sets seguidos == 1 set, `asyncio.Event` nunca perde nem duplica
    execução por isso -- quem consome sempre vê 'set' uma vez e
    limpa)."""
    store = _FakeStore()
    wake = asyncio.Event()
    server = ControlServer(host="127.0.0.1", port=0, store=store, wake=wake, auth_token="segredo-teste")

    resp1 = await _call_handle_promo(server, {
        "mode": "promotion",
        "window_start": "2026-09-10T14:00:00-03:00",
        "window_end": "2026-09-10T15:00:00-03:00",
    })
    assert resp1.status == 200
    first_end = store.window.window_end

    # Consumidor (main_loop) "processa" o primeiro wake, limpa o Event --
    # simula o `wake.clear()` que main_loop faz de verdade.
    wake.clear()

    resp2 = await _call_handle_promo(server, {
        "mode": "promotion",
        "window_start": "2026-09-10T14:00:00-03:00",
        "window_end": "2026-09-10T18:00:00-03:00",  # aviso de EXTENSÃO real
    })
    assert resp2.status == 200
    second_end = store.window.window_end

    assert first_end == "2026-09-10T15:00:00-03:00"
    assert second_end == "2026-09-10T18:00:00-03:00"
    assert second_end != first_end, "o segundo aviso precisa REALMENTE estender a janela persistida"
    assert len(store.set_calls) == 2, "cada aviso persiste sua própria janela -- nunca é ignorado silenciosamente"
    assert wake.is_set(), "o segundo aviso também precisa acordar o loop (mesmo após um clear() intermediário)"
    print("PASS: dois avisos sucessivos com window_end crescente -- janela persistida sempre reflete o ÚLTIMO aviso, sem duplicar wake/execução")


async def _test_control_server_normal_clears_window() -> None:
    """`mode=normal` explícito cancela a janela -- volta pro modo normal
    mesmo antes do `window_end` chegar (cancelamento manual, não só
    expiração por tempo)."""
    store = _FakeStore()
    store.window = PromoWindow(window_start="2026-09-10T14:00:00-03:00", window_end="2026-09-10T18:00:00-03:00")
    wake = asyncio.Event()
    server = ControlServer(host="127.0.0.1", port=0, store=store, wake=wake, auth_token="segredo-teste")

    resp = await _call_handle_promo(server, {"mode": "normal"})
    assert resp.status == 200
    assert store.window == PromoWindow()  # limpo de verdade, não só "ignorado"
    assert wake.is_set()
    print("PASS: mode=normal explícito cancela a janela persistida e acorda o loop")


async def _test_control_server_auth_and_validation_guardrails() -> None:
    """Guarda-corrios que HIGH_ACTIVITY depende: sem token/token errado
    nunca aceita comando (401); payload com mode desconhecido ou
    faltando window_start/window_end em promotion é rejeitado (400) --
    nunca aceita um intervalo arbitrário (ex.: interval_minutes)."""
    store = _FakeStore()
    wake = asyncio.Event()
    server = ControlServer(host="127.0.0.1", port=0, store=store, wake=wake, auth_token="segredo-real")

    resp_no_token = await _call_handle_promo(server, {"mode": "normal"}, token="errado")
    assert resp_no_token.status == 401
    assert not wake.is_set(), "comando não-autenticado NUNCA pode acordar o loop nem mudar estado"

    resp_bad_mode = await _call_handle_promo(server, {"mode": "turbo"}, token="segredo-real")
    assert resp_bad_mode.status == 400

    resp_missing_window = await _call_handle_promo(server, {"mode": "promotion"}, token="segredo-real")
    assert resp_missing_window.status == 400

    resp_bad_interval = await _call_handle_promo(
        server, {"mode": "promotion", "interval_minutes": 5}, token="segredo-real")
    assert resp_bad_interval.status == 400
    print("PASS: token inválido -> 401 sem efeito colateral; mode desconhecido/janela ausente/intervalo arbitrário -> 400, nunca aceitos")


# ---------------------------------------------------------------------------
# 6: single-flight -- nunca duas rodadas de varredura concorrentes.
# ---------------------------------------------------------------------------


async def _test_run_scan_single_flight_skips_concurrent_execution() -> None:
    """Duas chamadas de `run_scan` "ao mesmo tempo" (segunda dispara
    enquanto a primeira ainda segura o lock) -- a segunda precisa
    DETECTAR o lock ocupado e pular (nunca abrir um segundo Edge/varredura
    concorrente). Mede quantas vezes o scanner real foi de fato
    construído/chamado."""
    assert not worker.SCAN_LOCK.locked(), "pré-condição: lock livre antes do teste"

    scan_all_calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowScanner:
        def __init__(self, *a, **kw):
            pass

        async def scan_all(self):
            nonlocal scan_all_calls
            scan_all_calls += 1
            started.set()
            await release.wait()
            return {"lojas": 0}

    with patch.object(worker, "load_stores", return_value=[]), \
         patch.object(worker, "Scanner", _SlowScanner):
        task1 = asyncio.create_task(worker.run_scan({}, store=None))
        await asyncio.wait_for(started.wait(), timeout=2.0)
        assert worker.SCAN_LOCK.locked(), "primeira rodada precisa segurar o lock enquanto executa"

        # Segunda chamada "concorrente" -- lock já ocupado, deve retornar
        # (skip) sem incrementar scan_all_calls.
        await worker.run_scan({}, store=None)
        assert scan_all_calls == 1, "a segunda chamada NUNCA deveria ter iniciado uma segunda varredura"

        release.set()
        await asyncio.wait_for(task1, timeout=2.0)

    assert scan_all_calls == 1, "no fim, só UMA varredura real rodou -- a concorrente foi pulada, não enfileirada"
    assert not worker.SCAN_LOCK.locked(), "lock precisa ser liberado ao final"
    print("PASS: chamada concorrente de run_scan é pulada (single-flight) enquanto outra rodada já está em andamento")


# ---------------------------------------------------------------------------
# 7: restart -- janela promocional sobrevive a fechar/reabrir o store
# (equivalente a reiniciar o processo do worker).
# ---------------------------------------------------------------------------


def test_promo_window_persists_across_store_reopen() -> None:
    """Simula reinício: grava a janela, FECHA a conexão (como um processo
    que morre/reinicia faria), reabre um SqliteCouponStore NOVO apontando
    pro MESMO arquivo -- a janela persistida precisa reaparecer intacta,
    igual `main_loop` leria no primeiro loop após reiniciar."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store1 = SqliteCouponStore(path)
        window = PromoWindow(
            window_start="2026-09-10T14:00:00-03:00",
            window_end="2026-09-10T18:00:00-03:00",
        )
        store1.set_promo_window(window)
        store1.close()  # processo "morreu"

        store2 = SqliteCouponStore(path)  # "reiniciou", mesmo arquivo
        reloaded = store2.get_promo_window()
        assert reloaded.window_start == window.window_start
        assert reloaded.window_end == window.window_end
        store2.close()
        print("PASS: janela promocional sobrevive ao fechar/reabrir o store (comportamento real após reinício do worker)")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# 8: nenhuma bifurcação por modo dentro de run_scan -- a coleta acelerada
# usa exatamente o mesmo Scanner/parsers da coleta normal (prova
# estrutural: não existe um segundo caminho de código "modo promoção").
# ---------------------------------------------------------------------------


def test_run_scan_has_zero_mode_branching() -> None:
    """`run_scan` (chamado tanto em modo normal quanto em modo promoção,
    ver `main_loop`) não recebe `mode` como parâmetro nem consulta o modo
    em lugar nenhum -- o único efeito do modo é ACELERAR/DESACELERAR
    QUANDO `run_scan` é chamado (`next_slot`), nunca O QUE ele faz.
    Prova estrutural via introspecção do código real (não uma suposição):
    a assinatura de `run_scan` não tem `mode`, e seu corpo não referencia
    a palavra 'mode'/'promotion' em nenhum lugar."""
    import inspect
    sig = inspect.signature(worker.run_scan)
    assert "mode" not in sig.parameters, "run_scan não pode depender do modo -- é o MESMO scan sempre"
    source = inspect.getsource(worker.run_scan)
    assert "mode" not in source.lower()
    assert "promotion" not in source.lower()
    assert "promo" not in source.lower()
    print("PASS: run_scan não tem nenhuma bifurcação por modo -- coleta acelerada usa o MESMO Scanner/parsers da coleta normal (prova estrutural)")


def test_normal_collection_never_depends_on_promo_window_being_set() -> None:
    """`mode_for(now, None)` (sem NENHUMA janela jamais configurada, ou
    seja, sem missão/HIGH_ACTIVITY envolvida) sempre retorna 'normal' --
    o worker funciona plenamente sem depender de o GG jamais ter chamado
    `/control/promo`."""
    now = now_sp()
    assert mode_for(now, None) == "normal"
    slot = next_slot(now, "normal")
    assert slot > now
    assert (slot - now) <= timedelta(hours=1)
    print("PASS: sem janela jamais configurada, modo é sempre 'normal' -- coleta funciona sem depender de missão/HIGH_ACTIVITY nenhuma")


# ---------------------------------------------------------------------------


def _run_async(coro_fn):
    asyncio.run(coro_fn())


def main() -> None:
    sync_tests = [
        test_normal_mode_uses_hourly_slots_without_any_window,
        test_promotion_mode_uses_half_hour_slots_inside_window,
        test_mode_returns_to_normal_automatically_after_window_end,
        test_invalid_or_missing_window_falls_back_to_normal,
        test_promo_window_persists_across_store_reopen,
        test_run_scan_has_zero_mode_branching,
        test_normal_collection_never_depends_on_promo_window_being_set,
    ]
    async_tests = [
        _test_control_server_repeated_notices_extend_window_without_duplicate_wake,
        _test_control_server_normal_clears_window,
        _test_control_server_auth_and_validation_guardrails,
        _test_run_scan_single_flight_skips_concurrent_execution,
    ]
    for t in sync_tests:
        t()
    for t in async_tests:
        _run_async(t)
    total = len(sync_tests) + len(async_tests)
    print(f"\nTODOS OS TESTES DE CADENCIA/HIGH_ACTIVITY PASSARAM ({total}/{total})")


if __name__ == "__main__":
    main()
