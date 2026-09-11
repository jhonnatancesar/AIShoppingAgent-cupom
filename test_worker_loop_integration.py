#!/usr/bin/env python3
"""Prova INTEGRADA (revisão 2026-09-10, segunda rodada) do caminho real
de HIGH_ACTIVITY -- `worker.main_loop` de verdade, rodando como task real,
nunca uma reimplementação/simulação do laço de espera.

O usuário rejeitou explicitamente a versão anterior (`test_cadence.py`,
`_test_control_server_promotion_wakes_loop_before_scheduled_slot`) por
reimplementar o padrão `asyncio.wait_for(wake.wait(), timeout=...)` em vez
de chamar `worker.main_loop` -- removida de lá; a prova de antecipação/
despertar agora só existe aqui, contra o laço real.

Único ponto de controle externo permitido (dependência externa, não
lógica de decisão): `worker.now_sp` é trocado por um relógio falso
programável -- `cadence.next_slot` sempre alinha ao próximo HH:00/HH:30
do relógio REAL, e esperar horas de parede de verdade a cada teste é
inviável. Cada valor do relógio falso é escolhido logo ANTES do próprio
limite que `next_slot` (real, não modificado) calcularia -- o
`asyncio.sleep`/`wait_for` real ainda espera de verdade, só que poucos
milissegundos. A sincronização entre o laço real e as verificações do
teste usa um `asyncio.Event` real (setado pelo stub do Scanner a cada
rodada concluída) -- nunca `sleep` adivinhado.

`cadence.mode_for`/`next_slot`/`parse_promo_window`, `worker.main_loop`,
`worker.run_scan`, `worker.SCAN_LOCK`, `ControlServer` (rodando numa
porta loopback REAL, POST via `aiohttp.ClientSession` de verdade) e o
`SqliteCouponStore` real (mesmo backend que persiste a janela em PROD,
ver DEC-133/handoff seção 10) são todos reais, nunca reimplementados. Só
`Scanner`/`load_stores` (a dependência externa de verdade -- Playwright/
Edge) são substituídos por um stub instantâneo -- exatamente o "controle
de dependência externa" autorizado explicitamente pelo usuário.

Ausência de duplicação (single-flight) já é provada contra
`worker.run_scan` REAL em `test_cadence.py::_test_run_scan_single_flight_
skips_concurrent_execution` -- não duplicada aqui; citada no relatório
final como a mesma função real, só chamada de um teste diferente.
"""
from __future__ import annotations

import asyncio
import os
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiohttp

import worker
from cadence import mode_for, parse_promo_window
from control_server import ControlServer
from coupons.persistence import PromoWindow, SqliteCouponStore

TZ_SP = ZoneInfo("America/Sao_Paulo")


@dataclass
class FakeClock:
    """Sequência programável de retornos para `worker.now_sp` -- cada
    chamada consome o valor seguinte da lista; a última é reusada se o
    laço iterar mais vezes do que valores programados (nunca lança
    IndexError, só deixa de avançar o cenário)."""
    values: list
    calls: int = field(default=0)

    def __call__(self) -> datetime:
        idx = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[idx]


def _make_scanner_stub(done_event: asyncio.Event):
    """Stub da ÚNICA dependência externa de verdade (Playwright/Edge) --
    autorizado explicitamente pelo usuário. Sinaliza `done_event` a cada
    rodada concluída, pra sincronizar o teste com o laço real sem
    `sleep` adivinhado."""
    calls = {"n": 0}

    class _InstantScanner:
        def __init__(self, *a, **kw) -> None:
            pass

        async def scan_all(self):
            calls["n"] += 1
            done_event.set()
            return {"total_coupons_persisted": 0}

    return _InstantScanner, calls


def _dt(h, mi, s=0, ms=0, day=10):
    return datetime(2026, 9, day, h, mi, s, ms * 1000, tzinfo=TZ_SP)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _post_promo(port: int, token: str, body: dict) -> aiohttp.ClientResponse:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/control/promo",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            await resp.read()
            return resp


async def _wait_scan(done_event: asyncio.Event, label: str) -> None:
    try:
        await asyncio.wait_for(done_event.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        raise AssertionError(f"rodada esperada ({label}) não aconteceu em 5s -- main_loop real não chamou o Scanner")
    done_event.clear()


async def scenario_schedule_wake_and_acceleration() -> None:
    """Pontos 1-4: coleta normal agendada -> POST autenticado real ->
    despertar/antecipação real -> Scanner efetivamente chamado, via
    `worker.main_loop` real, sem nenhuma reimplementação do laço."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = SqliteCouponStore(db_path)
    wake = asyncio.Event()
    scan_done = asyncio.Event()
    token = "token-teste-integracao-1"
    port = _free_port()
    Scanner, calls = _make_scanner_stub(scan_done)

    control = ControlServer(host="127.0.0.1", port=port, store=store, wake=wake, auth_token=token)
    await control.start()

    # [0] 1a iteração: 'normal' (sem janela), slot real=10:00:00 -- 300ms
    #     de margem (tempo de sobra pro POST HTTP real chegar ANTES do
    #     timeout natural -- prova de antecipação, não de coincidência).
    # [1] 2a iteração (pós-wake real): já 'promotion' (janela setada
    #     pelo POST), slot real=10:30:00 -- 40ms de margem.
    clock = FakeClock(values=[_dt(9, 59, 59, 700), _dt(10, 29, 59, 960)])

    loop_task = asyncio.create_task(worker.main_loop({}, store, wake))
    try:
        with patch.object(worker, "now_sp", clock), \
             patch.object(worker, "load_stores", return_value={}), \
             patch.object(worker, "Scanner", Scanner):

            # ---- 1) coleta normal agendada -----------------------------
            await asyncio.sleep(0.02)
            assert calls["n"] == 0, "main_loop ainda deveria estar dormindo o slot normal (10:00:00), não escaneou ainda"
            assert not scan_done.is_set()

            # ---- 2) POST autenticado (HTTP real, ControlServer real) --
            resp = await _post_promo(port, token, {
                "mode": "promotion",
                "window_start": "2026-09-10T09:59:00-03:00",
                "window_end": "2026-09-10T11:00:00-03:00",
            })
            assert resp.status == 200

            # ---- 3) despertar real + antecipação -----------------------
            # main_loop acordou ANTES do timeout de 10:00:00 (que ainda
            # não tinha chegado quando o POST foi enviado) -- recomputou
            # 'promotion' (janela ativa), slot=10:30:00 real (curto).
            # ---- 4) Scanner efetivamente chamado ------------------------
            await _wait_scan(scan_done, "slot de promoção acelerado")
            assert calls["n"] == 1
            print("PASS (integrado, worker.main_loop real): agendado -> POST real -> despertar/antecipação real -> Scanner chamado")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await control.stop()
        store.close()
        os.unlink(db_path)


async def scenario_window_extension_and_return_to_normal() -> None:
    """Pontos 6-7: extensão de janela por um segundo POST real, e
    retorno à cadência normal depois que a janela (já estendida) expira
    -- via `worker.main_loop`/`cadence.mode_for` reais."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = SqliteCouponStore(db_path)
    # Janela promocional já ativa ANTES do laço começar (persistida no
    # MESMO backend real, `SqliteCouponStore`) -- cenário: HIGH_ACTIVITY
    # já estava em andamento, e um aviso de EXTENSÃO chega no meio dela.
    store.set_promo_window(PromoWindow(
        window_start="2026-09-10T09:59:00-03:00",
        window_end="2026-09-10T10:30:30-03:00",
    ))
    wake = asyncio.Event()
    scan_done = asyncio.Event()
    token = "token-teste-integracao-2"
    port = _free_port()
    Scanner, calls = _make_scanner_stub(scan_done)

    control = ControlServer(host="127.0.0.1", port=port, store=store, wake=wake, auth_token=token)
    await control.start()

    # [0] 1a iteração: já 'promotion' (janela setada antes do laço
    #     começar), slot real=10:30:00 -- 40ms de margem.
    # [1] 2a iteração: "estacionada" no meio da janela original
    #     (10:30:05, longe de qualquer limite de slot) -- `next_slot`
    #     real calcula 11:00:00 como próximo boundary de promoção
    #     (minuto >= 30 rola pra próxima hora cheia), delay real GRANDE
    #     -- dá tempo de sobra pro teste mandar o POST de extensão antes
    #     do timeout natural (nunca corrida entre teste e laço).
    # [2] 3a iteração (pós-wake do POST de extensão): ainda 'promotion'
    #     (janela ESTENDIDA), slot real=11:00:00 -- 40ms de margem.
    # [3+] 4a+ iteração: DEPOIS do window_end estendido (11:00:00) --
    #      retorno real ao modo normal.
    clock = FakeClock(values=[
        _dt(10, 29, 59, 960),
        _dt(10, 30, 5, 0),
        _dt(10, 59, 59, 960),
        _dt(11, 0, 0, 200),
    ])

    loop_task = asyncio.create_task(worker.main_loop({}, store, wake))
    try:
        with patch.object(worker, "now_sp", clock), \
             patch.object(worker, "load_stores", return_value={}), \
             patch.object(worker, "Scanner", Scanner):

            # 1a rodada (janela original, ainda não estendida).
            await _wait_scan(scan_done, "1a rodada, janela original")
            assert calls["n"] == 1

            # ---- 6) extensão de janela (POST real) ----------------------
            resp = await _post_promo(port, token, {
                "mode": "promotion",
                "window_start": "2026-09-10T09:59:00-03:00",
                "window_end": "2026-09-10T11:00:00-03:00",  # estendida (era 10:30:30)
            })
            assert resp.status == 200
            persisted = store.get_promo_window()
            assert persisted.window_end == "2026-09-10T11:00:00-03:00", "janela persistida real precisa refletir a extensão"

            # 2a rodada -- só acontece porque a janela FOI estendida (sem
            # extensão, 10:59:59.960 já estaria fora da janela original
            # de 10:30:30 e o modo teria voltado a 'normal' aqui).
            await _wait_scan(scan_done, "2a rodada, dentro da janela ESTENDIDA")
            assert calls["n"] == 2
            print("PASS (integrado, worker.main_loop real): 2a rodada só aconteceu porque a extensão de janela real foi persistida e respeitada")

            # ---- 7) retorno à cadência normal -----------------------------
            # `now_sp` da PRÓXIMA iteração real (11:00:00.200) já é
            # depois do window_end estendido -- main_loop, na volta ao
            # topo do laço, decide 'normal' sozinho (mesma `mode_for`
            # real que ele mesmo chama) sem nenhum novo POST.
            reloaded_window = parse_promo_window(store.get_promo_window())
            now_after = clock.values[3]
            assert mode_for(now_after, reloaded_window) == "normal", "cadence.mode_for real precisa voltar a 'normal' depois do window_end estendido"
            print("PASS (integrado): cadência volta a 'normal' sozinha depois que a janela (já estendida) expira, sem novo comando")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await control.stop()
        store.close()
        os.unlink(db_path)


def main() -> None:
    asyncio.run(scenario_schedule_wake_and_acceleration())
    asyncio.run(scenario_window_extension_and_return_to_normal())
    print("\nTODOS OS CENARIOS DE INTEGRACAO DO WORKER LOOP PASSARAM (2/2)")


if __name__ == "__main__":
    main()
