#!/usr/bin/env python3
"""Teste real do Job Object (`coupons/job_object.py`) e da integração com
`EdgeCdpProcess` -- sem pytest (não é dependência deste projeto, ver
`requirements.txt`), mesmo padrão de `smoke_test.py`: script standalone,
processos reais, `assert` direto.

Achado que motivou isto (2026-09-09, deploy da v1.0.1 em PROD): um
`--once` cortado por timeout externo deixava o Edge órfão preso na porta
9224 -- toda rodada seguinte falhava com "porta já responde como CDP" até
alguém matar o processo manualmente. Corrigido amarrando o Edge a um
Windows Job Object (mesmo mecanismo já usado no `collection_worker`
principal do AIShoppingAgent, `DEC-131`).

Uso:
  .venv\\Scripts\\python.exe test_job_object.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time

from coupons.job_object import EdgeLifecycleJob, JobObjectError


def pid_exists(pid: int) -> bool:
    """Sem `psutil` (não é dependência deste projeto) -- `tasklist` já
    vem com o Windows."""
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    )
    return str(pid) in result.stdout


def _spawn_disposable(seconds: int = 30) -> subprocess.Popen:
    """`ping` sobrevive a stdio redirecionado (achado real, sessão
    2026-09-08 do repositório principal: `timeout.exe` do Windows morre
    sozinho com stdin redirecionado -- não é bug do Job Object)."""
    return subprocess.Popen(
        ["ping", "-n", str(seconds), "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def test_close_kills_assigned_process() -> None:
    proc = _spawn_disposable()
    job = EdgeLifecycleJob()
    try:
        job.assign(proc.pid)
        assert pid_exists(proc.pid)
        job.close()
        time.sleep(1)
        assert not pid_exists(proc.pid), "processo atribuído devia morrer ao fechar o job"
    finally:
        if proc.poll() is None:
            proc.kill()
    print("PASS: close() mata processo atribuído")


def test_close_never_kills_unassigned_process() -> None:
    controle = _spawn_disposable()
    job = EdgeLifecycleJob()
    try:
        # Job criado, mas NADA atribuído a ele.
        job.close()
        time.sleep(1)
        assert pid_exists(controle.pid), "processo nunca atribuído não pode morrer"
    finally:
        job.close()  # idempotente
        controle.kill()
        controle.wait(timeout=5)
    print("PASS: close() nunca mata processo não atribuído")


def test_double_close_is_harmless() -> None:
    job = EdgeLifecycleJob()
    job.close()
    job.close()  # não pode levantar
    print("PASS: close() duplo é inofensivo")


def test_assign_to_already_dead_process_raises_cleanly() -> None:
    proc = _spawn_disposable(seconds=1)
    proc.wait(timeout=5)
    assert not pid_exists(proc.pid)
    job = EdgeLifecycleJob()
    try:
        raised = False
        try:
            job.assign(proc.pid)
        except JobObjectError:
            raised = True
        assert raised, "assign() num PID morto precisa falhar explícito, nunca em silêncio"
    finally:
        job.close()
    print("PASS: assign() em processo já morto falha explícito (JobObjectError)")


def test_jobs_are_independent() -> None:
    proc_a = _spawn_disposable()
    proc_b = _spawn_disposable()
    job_a = EdgeLifecycleJob()
    job_b = EdgeLifecycleJob()
    try:
        job_a.assign(proc_a.pid)
        job_b.assign(proc_b.pid)
        job_a.close()
        time.sleep(1)
        assert not pid_exists(proc_a.pid), "job A devia matar só o processo A"
        assert pid_exists(proc_b.pid), "job B nunca deveria ser afetado pelo close() do job A"
    finally:
        job_b.close()
        for p in (proc_a, proc_b):
            if p.poll() is None:
                p.kill()
    print("PASS: jobs são independentes entre si")


def test_child_dies_when_parent_process_is_killed_abruptly_no_close_called() -> None:
    """O cenário REAL que motivou a correção: processo pai morto
    abruptamente (kill, sem chance de rodar `EdgeCdpProcess.stop()`) --
    o filho atribuído ao job morre sozinho mesmo assim, garantido pelo
    kernel, nunca por código Python rodando durante o kill."""
    helper_code = (
        "import subprocess, sys, time\n"
        "sys.path.insert(0, r'" + str(__import__("pathlib").Path(__file__).resolve().parent) + "')\n"
        "from coupons.job_object import EdgeLifecycleJob\n"
        "job = EdgeLifecycleJob()\n"
        "grandchild = subprocess.Popen(['ping', '-n', '30', '127.0.0.1'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)\n"
        "job.assign(grandchild.pid)\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", helper_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    grandchild_pid_line = parent.stdout.readline().strip()
    assert grandchild_pid_line, "processo helper não reportou o PID do neto"
    grandchild_pid = int(grandchild_pid_line)
    assert pid_exists(grandchild_pid)

    # Kill duro do PAI -- nenhuma chance de `job.close()` nem de
    # `EdgeCdpProcess.stop()` rodarem. É a simulação exata do achado real.
    parent.kill()
    parent.wait(timeout=5)
    time.sleep(2)

    assert not pid_exists(grandchild_pid), (
        "o neto (Edge simulado) tinha que morrer sozinho quando o pai foi morto abruptamente, "
        "sem nenhum código de cleanup rodando -- é exatamente o achado real corrigido"
    )
    print("PASS: filho morre sozinho quando o pai é morto abruptamente (cenário real)")


async def test_edge_cdp_process_assigns_and_cleans_job() -> None:
    """Integração real com `EdgeCdpProcess` -- processo Edge de verdade
    lançado numa porta de teste (19224, nunca 9224 -- essa é usada pelo
    worker real desta mesma máquina) e um perfil PRÓPRIO do teste (nunca
    o perfil default, `aishopping-coupon-edge-profile` -- colidiria com
    uma rodada real concorrente, Edge não permite dois processos no mesmo
    perfil ao mesmo tempo)."""
    import tempfile
    from pathlib import Path

    from coupons.edge_transport import EdgeCdpProcess

    test_profile = Path(tempfile.gettempdir()) / "aishopping-coupon-edge-profile-TESTE-job-object"
    edge = EdgeCdpProcess(port=19224, profile_dir=test_profile, startup_timeout_seconds=30)
    await edge.start()
    try:
        assert edge._process is not None
        assert edge._job is not None, "Job Object devia ter sido atribuído no start()"
        pid = edge._process.pid
        assert pid_exists(pid)
    finally:
        await edge.stop()
    time.sleep(1)
    assert not pid_exists(pid), "processo do Edge devia estar morto depois de stop()"
    assert edge._job is None, "job devia ter sido limpo (self._job = None) depois de stop()"
    print("PASS: EdgeCdpProcess atribui e limpa o Job Object corretamente (processo real)")


def main() -> None:
    test_close_kills_assigned_process()
    test_close_never_kills_unassigned_process()
    test_double_close_is_harmless()
    test_assign_to_already_dead_process_raises_cleanly()
    test_jobs_are_independent()
    test_child_dies_when_parent_process_is_killed_abruptly_no_close_called()
    asyncio.run(test_edge_cdp_process_assigns_and_cleans_job())
    print("\nTODOS OS TESTES DO JOB OBJECT PASSARAM (7/7)")


if __name__ == "__main__":
    main()
