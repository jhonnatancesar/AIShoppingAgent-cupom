"""Windows Job Object: amarra o ciclo de vida de um processo filho (e toda
a árvore que ele spawnar depois -- herança nativa do Windows para membros
de job) ao processo Python atual.

Por que isto existe (achado real, 2026-09-09, mesmo achado já corrigido no
`collection_worker` principal do AIShoppingAgent, `DEC-131` daquele
repositório): o Windows não tem equivalente a SIGKILL capturável --
quando o processo do worker é morto abruptamente (`Stop-Process -Force`,
crash, kill externo, `--once` cortado por timeout de quem chamou) nenhum
código Python roda pra fechar o Edge que ele lançou
(`EdgeCdpProcess.stop` nunca é alcançado). Um Job Object com
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` resolve isso no nível do kernel, sem
depender de nenhum código rodar: quando o ÚLTIMO handle do job fecha
(automaticamente, pelo próprio SO, quando o processo dono termina -- por
QUALQUER motivo), o Windows mata todo processo ainda vivo atribuído ao
job. Nunca mata processos de outro job/usuário -- só os explicitamente
atribuídos a ESTE job específico.

Só `ctypes` contra `kernel32.dll` (3 chamadas Win32) -- sem depender de
`pywin32`, mantendo este pacote autocontido (ver docstring de
`edge_transport.py`: "sem nenhuma dependência do repositório principal
AIShoppingAgent"). Requer nested job objects, suportado nativamente desde
Windows 8 / Windows Server 2012, sem nenhuma flag extra de configuração.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging

logger = logging.getLogger("coupons.job_object")

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObjectError(RuntimeError):
    """Falha real numa chamada Win32 de Job Object -- nunca engolida em
    silêncio pelo módulo; o chamador (`EdgeCdpProcess`) decide se trata
    como fatal ou só registra e segue sem a proteção extra."""


class EdgeLifecycleJob:
    """Um Job Object por processo Edge lançado. `assign(pid)` atribui o
    processo raiz do Edge (`msedge.exe --remote-debugging-address=...`) --
    o Windows propaga automaticamente para toda a árvore que ELE spawnar
    depois (GPU/renderer/utility/crashpad), sem precisar atribuir cada
    filho manualmente. `close()` fecha o handle deste lado -- só mata
    processos-membro ainda vivos nesse momento (idempotente/inofensivo se
    já estiverem mortos, ex.: depois de `EdgeCdpProcess.stop` já ter
    encerrado tudo de forma graciosa). O caso real que este objeto existe
    para cobrir nunca chama `close()`: o processo Python morre primeiro
    (ou é morto), e o próprio Windows fecha o handle (e mata a árvore) sem
    nenhum código rodar -- é exatamente o que deixava a porta 9224 presa
    para a próxima rodada."""

    def __init__(self) -> None:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise JobObjectError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise JobObjectError(f"SetInformationJobObject failed: {error}")
        self._kernel32 = kernel32
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        """Atribui `pid` a este job -- levanta `JobObjectError` se o
        processo já tiver terminado ou a chamada falhar; o chamador
        decide o que fazer (nunca decidido aqui)."""
        process_handle = self._kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if not process_handle:
            raise JobObjectError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        try:
            ok = self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
            if not ok:
                raise JobObjectError(
                    f"AssignProcessToJobObject({pid}) failed: {ctypes.get_last_error()}"
                )
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None
