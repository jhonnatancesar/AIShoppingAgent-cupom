"""Transporte Edge/CDP do Coupon Collector -- autocontido, sem nenhuma
dependência do repositório principal AIShoppingAgent (propositalmente:
esta pasta precisa poder ser removida/movida para outra máquina sem
tocar em mais nada).

Sobe um Microsoft Edge REAL do sistema como subprocesso dedicado --
nunca ``playwright.chromium.launch()`` gerenciado (mesmo princípio já
adotado pelo projeto principal na TASK-109: nenhum binário de navegador
é baixado/gerenciado pelo Playwright neste projeto) -- com porta CDP e
perfil próprios, distintos do Edge do ``collection_worker`` principal
(evita qualquer conflito entre os dois processos na mesma máquina).

Ciclo de vida por RODADA, não por lease/idle-timeout: este worker varre
no máximo a cada 30min (``cadence.py``), então sobe o Edge no início de
``scan_all()`` e encerra no fim -- o mesmo padrão que o Firefox já tinha
antes. O supervisor com lease/idle-timeout do worker principal existe
para atender uso contínuo e imprevisível (requisições de coleta a
qualquer momento); esse problema não existe aqui.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import aiohttp

from .job_object import EdgeLifecycleJob, JobObjectError

logger = logging.getLogger("coupons.edge_transport")


class EdgeLaunchError(RuntimeError):
    """Falha ao localizar, iniciar ou conectar ao Edge dedicado."""


# Caminhos padrão de instalação do Microsoft Edge no Windows, nessa ordem.
_COMMON_EDGE_PATHS = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
)


def discover_edge_executable(explicit_path: Optional[str] = None) -> Path:
    """Localiza o ``msedge.exe`` real já instalado no sistema.

    Nunca baixa, instala ou gerencia um binário de navegador -- só
    localiza uma instalação normal já existente."""
    if explicit_path:
        path = Path(os.path.expandvars(explicit_path))
        if path.is_file():
            return path
        raise EdgeLaunchError(
            f"EDGE_EXECUTABLE aponta para um caminho inexistente: {path}"
        )
    for candidate in _COMMON_EDGE_PATHS:
        path = Path(os.path.expandvars(candidate))
        if path.is_file():
            return path
    raise EdgeLaunchError(
        "Microsoft Edge não encontrado nos caminhos padrão do Windows. "
        "Defina \"edge_executable\" em config.json com o caminho completo do msedge.exe."
    )


def default_edge_profile_dir() -> Path:
    """Perfil próprio e exclusivo do Coupon Collector -- nunca o perfil
    pessoal do usuário, nunca o mesmo perfil/porta do ``collection_worker``
    principal."""
    return Path(tempfile.gettempdir()) / "aishopping-coupon-edge-profile"


class EdgeCdpProcess:
    """Sobe e derruba um Edge dedicado, uma vez por rodada de varredura.

    Uso::

        async with EdgeCdpProcess(port=9224) as cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            ...
    """

    def __init__(
        self,
        *,
        port: int = 9224,
        executable: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        headless: bool = False,
        startup_timeout_seconds: float = 30.0,
        probe_interval_seconds: float = 0.5,
    ) -> None:
        if platform.system() != "Windows":
            raise EdgeLaunchError("EdgeCdpProcess requer Windows (msedge.exe nativo).")
        if startup_timeout_seconds <= 0 or probe_interval_seconds <= 0:
            raise ValueError("timeouts devem ser positivos")
        self._port = port
        self._executable = discover_edge_executable(executable)
        self._profile_dir = (profile_dir or default_edge_profile_dir()).resolve()
        self._headless = headless
        self._startup_timeout_seconds = startup_timeout_seconds
        self._probe_interval_seconds = probe_interval_seconds
        self._process: Optional[asyncio.subprocess.Process] = None
        self._job: Optional[EdgeLifecycleJob] = None
        self._cdp_url = f"http://127.0.0.1:{port}"

    @property
    def cdp_url(self) -> str:
        return self._cdp_url

    @property
    def profile_dir(self) -> Path:
        return self._profile_dir

    async def __aenter__(self) -> str:
        await self.start()
        return self._cdp_url

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if await self._cdp_ready():
            raise EdgeLaunchError(
                f"porta {self._port} já responde como CDP -- outro Edge (ou rodada "
                "presa de uma execução anterior) já está usando essa porta. "
                "Verifique processos msedge.exe travados antes de tentar de novo."
            )
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        arguments = [
            str(self._executable),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            # Achado real (2026-09-09, deploy da v1.0.1 em PROD): sem esta
            # flag, o msedge.exe que a gente lança pode se relançar
            # sozinho (camada de compatibilidade do Windows) num PID
            # NOVO e encerrar o processo original -- `self._process`
            # ficava apontando pro processo ERRADO (já morto), então
            # `stop()`/Job Object nunca alcançavam o Edge real, que ficava
            # órfão preso na porta até alguém matar manualmente. Provado
            # ao vivo: sem a flag, PID rastreado != PID dono da porta;
            # com ela, os dois PIDs batem. Edge já adiciona esta mesma
            # flag sozinho quando relança -- passar de propósito evita o
            # relançamento acontecer.
            "--edge-skip-compat-layer-relaunch",
        ]
        if self._headless:
            # Achado real (validado nesta mesma pasta, TASK-106): headless
            # correlaciona com bloqueio 403 em Magalu/Mercado Livre que não
            # acontece com janela real. O collection_worker principal
            # (EdgeCdpSupervisor, TASK-109) nunca usa headless -- só
            # --start-minimized, mesma escolha aqui por padrão.
            arguments.append("--headless=new")
        else:
            arguments.append("--start-minimized")
        arguments.append("about:blank")

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            self._process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as e:
            raise EdgeLaunchError(f"não consegui iniciar o Edge dedicado: {e}") from e

        # Achado real (2026-09-09): sem isto, um `--once` cortado por
        # timeout externo (ou qualquer kill abrupto deste processo Python)
        # deixava o Edge órfão preso na porta -- toda rodada seguinte
        # falhava com "porta já responde como CDP" até alguém matar o
        # processo travado manualmente. Job Object garante, no nível do
        # kernel, que a árvore inteira do Edge morre junto com este
        # processo Python, por qualquer motivo -- mesmo mecanismo já
        # corrigido no `collection_worker` principal (ver
        # `coupons/job_object.py`). Best-effort: falha aqui nunca impede
        # o Edge de subir, só perde a proteção extra.
        try:
            job = EdgeLifecycleJob()
            job.assign(self._process.pid)
        except JobObjectError:
            logger.warning("edge_job_object_setup_failed", exc_info=True)
        else:
            self._job = job

        try:
            await self._wait_until_ready()
        except Exception:
            await self.stop()
            raise
        logger.info("edge_cdp_ready porta=%s pid=%s", self._port, self._process.pid)

    async def stop(self) -> None:
        """Encerra o processo diretamente -- este objeto sempre é quem
        iniciou o processo (nunca adota um Edge pré-existente), então não
        precisa de ``Browser.close`` via CDP antes: ``terminate()`` no
        próprio handle do subprocesso já é suficiente e mais simples.

        O fechamento do Job Object roda em ``finally`` -- precisa
        acontecer mesmo quando o processo já está morto (early return),
        senão o handle do job vaza. Redundante com a garantia de kernel
        (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), mas fechar aqui também
        depois de um `terminate()`/`kill()` bem-sucedido é higiene normal
        de recurso, não a proteção principal."""
        process = self._process
        self._process = None
        job = self._job
        self._job = None
        try:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                logger.info("edge_cdp_encerrado porta=%s", self._port)
        finally:
            if job is not None:
                job.close()

    async def _wait_until_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._startup_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self._cdp_ready():
                return
            await asyncio.sleep(self._probe_interval_seconds)
        raise EdgeLaunchError("Edge dedicado não respondeu ao CDP dentro do timeout.")

    async def _cdp_ready(self) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=1.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self._cdp_url}/json/version") as resp:
                    if resp.status != 200:
                        return False
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False
        browser = str(payload.get("Browser") or "")
        ws_url = str(payload.get("webSocketDebuggerUrl") or "")
        return browser.startswith("Edg/") and ws_url.startswith(
            ("ws://127.0.0.1:", "ws://localhost:", "ws://[::1]:")
        )
