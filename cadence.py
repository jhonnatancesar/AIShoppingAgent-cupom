"""Política de cadência por relógio, em America/Sao_Paulo.

Modos:
- ``normal``: varre a cada 1h, em HH:00 (começando à meia-noite).
- ``promotion``: varre a cada 30min, em HH:00 e HH:30.

O modo é função de *agora* dentro da janela promocional
``window_start <= now < window_end`` -> promotion; senão normal. Assim o worker
volta sozinho ao normal quando a janela acaba, sem novo comando.

A cada iteração calcula o PRÓXIMO slot de relógio a partir de ``now`` (robusto a
drift / relógio parado). ``parse_promo_window`` valida apenas a janela
(end > start); nenhum intervalo arbitrário é aceito.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from coupons.persistence import PromoWindow

# Timezone explícita exigida pelo projeto (TASK-106 / decisão do usuário).
try:
    TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - tzdata ausente
    TZ = ZoneInfo("UTC")

NORMAL_INTERVAL = timedelta(hours=1)
PROMO_INTERVAL = timedelta(minutes=30)


def now_sp() -> datetime:
    """Agora em America/Sao_Paulo, timezone-aware."""
    return datetime.now(TZ)


@dataclass
class Window:
    start: Optional[datetime] = None
    end: Optional[datetime] = None


def parse_promo_window(window: PromoWindow) -> Optional[Window]:
    """Converte a janela persistida; retorna None se vazia/inválida."""
    if not window.window_start or not window.window_end:
        return None
    try:
        start = datetime.fromisoformat(window.window_start)
        end = datetime.fromisoformat(window.window_end)
    except ValueError:
        return None
    if end <= start:
        return None
    return Window(start=start, end=end)


def mode_for(now: datetime, window: Optional[Window]) -> str:
    """normal ou promotion, baseado em *agora* dentro da janela."""
    if window and window.start <= now < window.end:
        return "promotion"
    return "normal"


def next_slot(now: datetime, mode: str) -> datetime:
    """Próximo slot de relógio estritamente depois de ``now``.

    Se ``now`` cair exatamente num slot (minuto==0 com hora cheia, ou
    minuto==30), avança para o slot seguinte para nunca recomputar o mesmo.
    """
    cur = now.replace(second=0, microsecond=0)

    if mode == "promotion":
        # Slots a cada 30min (HH:00 e HH:30).
        candidate = cur.replace(minute=30) if cur.minute < 30 else (cur + timedelta(hours=1)).replace(minute=0)
        while candidate <= now:
            candidate += timedelta(minutes=30)
        return candidate
    else:
        # Slots a cada 1h, em HH:00.
        candidate = (cur + timedelta(hours=1)).replace(minute=0)
        while candidate <= now:
            candidate += timedelta(hours=1)
        return candidate
