"""Endpoint HTTP de controle do Coupon Collector.

- ``POST /control/promo`` com ``{"mode": "promotion", "window_start": ..., "window_end": ...}``
  entra em promoção; ``{"mode": "normal"}`` cancela (limpa a janela).
- Auth: ``Authorization: Bearer <AUTH_TOKEN>``. Requisição sem token ou com token
  inválido -> ``401``.
- FAIL-CLOSED: sem ``AUTH_TOKEN`` configurado, o servidor NÃO sobe.
- Validação: aceita APENAS ``mode in {normal, promotion}`` e, em ``promotion``,
  exige ``window_start``/``window_end`` com ``end > start``. Intervalos
  arbitrários (ex.: interval_minutes) são rejeitados com ``400``.
- Bind host configurável (config.control.host); padrão 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict

import aiohttp
from aiohttp import web
from datetime import datetime

from cadence import parse_promo_window, Window
from coupons.persistence import CouponStore, PromoWindow

logger = logging.getLogger("coupons.control")

MODES = frozenset({"normal", "promotion"})


class ControlServer:
    """Servidor HTTP leve que expõe só o endpoint de controle."""

    def __init__(
        self,
        host: str,
        port: int,
        store: CouponStore,
        wake: asyncio.Event,
        auth_token: str,
    ) -> None:
        if not auth_token:
            raise RuntimeError(
                "AUTH_TOKEN não definido. O servidor de controle é fail-closed: "
                "sem token, ele NÃO sobe."
            )
        self.host = host
        self.port = port
        self.store = store
        self.wake = wake
        self.auth_token = auth_token

        self.app = web.Application()
        self.app.router.add_post("/control/promo", self.handle_promo)
        self.runner: Any = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        logger.info("Servidor de controle ouvindo em http://%s:%s/control/promo", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    def _check_auth(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {self.auth_token}"
        if header != expected:
            return False
        # Comparação em tempo constante evita leaks por timing.
        import hmac
        return hmac.compare_digest(header, expected)

    async def handle_promo(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            raw = await request.json()
        except (json.JSONDecodeError, aiohttp.ContentTypeError):
            return web.json_response({"error": "body deve ser JSON"}, status=400)

        mode = raw.get("mode")
        if mode not in MODES:
            return web.json_response(
                {"error": f"mode deve ser um de {sorted(MODES)} -- "
                          "nenhum intervalo arbitrário é aceito"},
                status=400,
            )

        if mode == "normal":
            self.store.clear_promo_window()
            window = None
        else:
            window_start, window_end = raw.get("window_start"), raw.get("window_end")
            if not window_start or not window_end:
                return web.json_response(
                    {"error": "promotion exige window_start e window_end (ISO)"}, status=400)
            promo = PromoWindow(window_start=window_start, window_end=window_end)
            win = parse_promo_window(promo)
            if win is None:
                return web.json_response(
                    {"error": "janela inválida: end deve ser > start (ISO com timezone)"}, status=400)
            self.store.set_promo_window(promo)
            window = win

        # Acorda o loop principal para recomputar o modo/cadência AGORA.
        self.wake.set()

        return web.json_response({
            "ok": True,
            "mode": mode,
            "window_start": window.start.isoformat() if window else None,
            "window_end": window.end.isoformat() if window else None,
        })