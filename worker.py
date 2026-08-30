"""Worker principal do Coupon Collector (Windows Server, Edge/CDP dedicado).

Loop: carrega a janela promocional persistida -> calcula o modo (normal /
promotion) com base em *agora* em America/Sao_Paulo -> calcula o próximo slot
de relógio -> dorme até lá (acordável pelo endpoint de controle) -> executa a
rodada de varredura com SINGLE-FLIGHT (nunca duas varreduras simultâneas; se o
slot chegar enquanto outra ainda roda, não abre um segundo Edge) -> repete.

Uso:
  python worker.py            # daemon (loop) -- Scheduled Task encaminha logs
  python worker.py --once     # uma única rodada imediata (smoke test)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from cadence import Window, mode_for, next_slot, now_sp, parse_promo_window
from control_server import ControlServer
from coupons.persistence import CouponStore, open_coupon_store
from coupons.scanner import Scanner, StoreBackoff
from coupons.stores import load_stores
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("coupons.worker")

SCAN_LOCK = asyncio.Lock()  # single-flight: garante nunca duas varreduras juntas


def load_config() -> Dict[str, Any]:
    path = BASE_DIR / "config.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def run_scan(config: Dict[str, Any], store: CouponStore) -> None:
    """Executa UMA rodada completa, respeitando o single-flight lock."""
    if SCAN_LOCK.locked():
        logger.info("Rodada anterior ainda em execução; pulando este slot (single-flight).")
        return
    async with SCAN_LOCK:
        stores = load_stores(config)
        scanner = Scanner(
            stores=stores,
            store=store,
            config=config,
            backoff=StoreBackoff(config.get("backoff_block_hours", 4)),
        )
        logger.info("Iniciando rodada de varredura (%d lojas)...", len(stores))
        summary = await scanner.scan_all()
        logger.info("Rodada concluída: %s", summary)


async def main_loop(config: Dict[str, Any], store: CouponStore, wake: asyncio.Event) -> None:
    tz = config.get("timezone", "America/Sao_Paulo")

    while True:
        promo = store.get_promo_window()
        window: Optional[Window] = parse_promo_window(promo)
        now = now_sp()
        mode = mode_for(now, window)
        slot = next_slot(now, mode)

        logger.info(
            "agora=%s modo=%s próximo_slot=%s janela=%s",
            now.isoformat(), mode, slot.isoformat(),
            (window.start.isoformat(), window.end.isoformat()) if window else None,
        )

        # Dorme até o próximo slot, acordável pelo endpoint de controle.
        delay = (slot - now).total_seconds()
        try:
            await asyncio.wait_for(wake.wait(), timeout=max(delay, 0))
            wake.clear()
            # Acordou por comando de controle: recomputa já (loop acima).
            logger.info("Acordado por controle; recomputando cadência.")
            continue
        except asyncio.TimeoutError:
            pass  # chegou o slot: executa

        try:
            await run_scan(config, store)
        except Exception as e:
            logger.error("Falha na rodada: %s", e)


async def amain(once: bool) -> None:
    # NOTA: NÃO forçamos WindowsSelectorEventLoopPolicy no Windows. Tanto o
    # EdgeCdpProcess (lança o Edge dedicado via subprocess) quanto o driver
    # do Playwright (connect_over_cdp) precisam de asyncio.subprocess, que o
    # loop selector do Windows não suporta -- usamos o loop padrão (Proactor
    # no Windows, epoll no Linux).
    load_dotenv(BASE_DIR / ".env")
    config = load_config()

    token = os.getenv("AUTH_TOKEN", "")
    if not token:
        logger.error("AUTH_TOKEN não definido: crie o .env a partir do .env.example. Abortando.")
        sys.exit(1)

    db_path = str((BASE_DIR / config.get("data_db", "data/worker.db")).resolve())
    store = open_coupon_store(db_path)

    if once:
        await run_scan(config, store)
        store.close()
        return

    ctrl_cfg = config.get("control", {})
    wake = asyncio.Event()
    control = ControlServer(
        host=ctrl_cfg.get("host", "127.0.0.1"),
        port=int(ctrl_cfg.get("port", 8090)),
        store=store,
        wake=wake,
        auth_token=token,
    )
    await control.start()
    try:
        await main_loop(config, store, wake)
    finally:
        await control.stop()
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coupon Collector Worker")
    parser.add_argument("--once", action="store_true", help="executa uma única rodada e sai")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.once))
    except KeyboardInterrupt:
        logger.info("Interrompido.")