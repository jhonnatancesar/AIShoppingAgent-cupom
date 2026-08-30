#!/usr/bin/env python3
"""Smoke test do Coupon Collector: valida a DESCOBERTA de cupons.

O centro NÃO é "conseguiu buscar produtos". O critério de sucesso é: a loja
percorreu suas fontes oficiais de descoberta de cupom (em ordem), sem bloqueio,
identificou sinais relevantes, aprofundou para o detalhe do produto quando
houve indício e aplicou a regra de evidência.

Para cada fonte visitada registra: URL, status HTTP, indício de cupom,
aprofundamento, evidência literal e se persistiu (ou o motivo de não persistir).

Uso:
  python smoke_test.py [--stores amazon,kabum,magalu,mercadolivre]
  AUTH_TOKEN deve estar no .env (exigência fail-closed do worker).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from coupons.persistence import open_coupon_store
from coupons.scanner import Scanner, StoreBackoff
from coupons.stores import load_stores

BASE_DIR = Path(__file__).resolve().parent
logging.basicConfig(level=logging.WARNING)

# ---- medição de RAM (opcional) --------------------------------------------
try:
    import psutil  # type: ignore
    HAS_PSUTIL = True
except Exception:
    HAS_PSUTIL = False


def load_config() -> dict:
    with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


def sample_edge_mb() -> int:
    if not HAS_PSUTIL:
        return 0
    total = 0
    for proc in psutil.process_iter(["name"]):
        try:
            nm = (proc.info.get("name") or "").lower()
            if "msedge" in nm:
                total += proc.memory_info().rss
        except Exception:
            pass
    return total // (1024 * 1024)


async def measure_peak(stop: asyncio.Event) -> int:
    peak = 0
    while not stop.is_set():
        peak = max(peak, sample_edge_mb())
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    return peak


def render_report(store_id: str, summary: dict) -> None:
    print(f"\n===== {store_id.upper()} :: status={summary.get('status')} "
          f"persistidos={summary.get('coupons_persisted_count')} "
          f"detalhes_abertos={summary.get('product_details_opened')} =====")
    if summary.get("status") == "backed_off":
        print("  [SALTO] loja em backoff de bloqueio (não varrida).")
        return
    if summary.get("error"):
        print(f"  [ERRO] {summary.get('error')}")
    for src in summary.get("sources", []):
        hint = "+" if src.get("coupon_hint") else "."
        deep = f" aprofundou->{src['url']}" if src.get("deepened") else ""
        blocked = " [BLOQUEADO]" if src.get("blocked") else ""
        note = f" ({src.get('note')})" if src.get("note") else ""
        print(f"  [{src.get('label')}] status={src.get('status')} "
              f"cupom={hint} persistiu={src.get('persisted')}{deep}{blocked}{note}")
        if src.get("status") == "current":
            print(f"      fonte: {src.get('url')}")
        for ev in src.get("evidence", []):
            print(f"      evidência: {ev}")


def store_passed(summary: dict) -> tuple[bool, str]:
    st = summary.get("status")
    if st == "blocked":
        return True, "bloqueada por anti-bot (comportamento esperado em alguns ambientes)"
    if st == "backed_off":
        return True, "não varrida (backoff de bloqueio ativo)"
    if st == "error":
        return False, "erro durante a varredura"
    sources = summary.get("sources", [])
    if not sources:
        return False, "nenhuma fonte visitada"
    # Sucesso: percorreu fontes sem bloqueio; se houve indício, aprofundou.
    had_hint = any(s.get("coupon_hint") for s in sources)
    deepened = summary.get("product_details_opened", 0) > 0
    if had_hint and not deepened:
        return False, "houve indício de cupom mas não aprofundou"
    return True, "ok"


async def run_once(store_ids: list[str]) -> int:
    load_dotenv(BASE_DIR / ".env")
    if not os.getenv("AUTH_TOKEN"):
        print("ERRO: AUTH_TOKEN ausente. Crie .env a partir de .env.example.")
        return 2
    config = load_config()
    all_specs = load_stores(config)
    specs = {k: v for k, v in all_specs.items() if k in store_ids}
    if not specs:
        print("ERRO: nenhuma loja selecionada é habilitada em config.json.")
        return 2

    # DB temporário do smoke test: garante estado limpo para validar
    # persistência e zero-persistência sem evidência.
    tmpdb = tempfile.mkdtemp(prefix="coupon_smoke_") + "/smoke.db"
    store = open_coupon_store(tmpdb)
    scanner = Scanner(
        stores=specs,
        store=store,
        config=config,
        backoff=StoreBackoff(config.get("backoff_block_hours", 4)),
    )

    stop = asyncio.Event()
    peak_task = asyncio.create_task(measure_peak(stop)) if HAS_PSUTIL else None

    print(f"Smoke test de descoberta de cupom para: {', '.join(specs)}")
    summary = await scanner.scan_all()
    stop.set()
    peak = (await peak_task) if peak_task else None

    all_pass = True
    for store_id in specs:
        render_report(store_id, summary.get(store_id, {}))
        ok, why = store_passed(summary.get(store_id))
        print(f"  -> {store_id}: {'PASSOU' if ok else 'FALHOU'} ({why})")
        all_pass = all_pass and ok

    print("\n===== CONSOLIDADO =====")
    print(f"total de cupons persistidos: {summary.get('total_coupons_persisted')}")
    if peak is not None:
        print(f"pico de RAM do Edge dedicado (todos os processos msedge.exe): ~{peak} MB")
    else:
        print("RAM: psutil não instalado; pulei a medição. (opcional)")

    store.close()
    return 0 if all_pass else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test de descoberta de cupons")
    parser.add_argument("--stores", default="amazon,kabum,magalu,mercadolivre",
                        help="lojas separadas por vírgula")
    args = parser.parse_args()
    ids = [s.strip().lower() for s in args.stores.split(",") if s.strip()]
    # NOTA: usamos o loop padrão; o WindowsSelectorEventLoopPolicy não suporta
    # subprocess (EdgeCdpProcess lança o Edge dedicado, e o driver do
    # Playwright usa subprocess para connect_over_cdp), então NÃO o forçamos.
    sys.exit(asyncio.run(run_once(ids)))
