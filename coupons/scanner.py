"""Varredura por loja com Edge + Playwright (async), orientada por fontes.

Cada loja tem uma lista ORDENADA de fontes de descoberta (``sources`` em
config.json). O scanner percorre na ordem configurada: home -> área de cupons
-> banners/carrosséis -> cards -> busca -> detalhe do produto.

Princípios:
- Nenhuma fonte é obrigatória; uma fonte sem cupom (404, sem marcador) é
  registrada e o fluxo segue para a próxima.
- Aprofundar (detalhe do produto) SOMENTE quando um card/oferta indicar cupom,
  limitado a ``scanner.max_product_details_per_store`` (economiza RAM/tempo).
- Só persiste cupom com evidência oficial real da loja (regra absoluta).
- Um único ``page`` reutilizado por loja dentro da rodada.
- Edge dedicado (``edge_transport.EdgeCdpProcess``) sobe UMA vez por rodada,
  reutilizado entre lojas, encerrado no finally -- nunca
  ``playwright.chromium.launch()`` gerenciado.
- Bloqueio (403/429 ou marcadores) -> backoff daquela loja; falha isolada NÃO
  derruba as demais.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from .edge_transport import EdgeCdpProcess, EdgeLaunchError
from .evidence import build_coupons, has_coupon_hint
from .persistence import CouponStore
from .stores import (
    CARD_KINDS,
    STORE_LEVEL_KINDS,
    SourceSpec,
    StoreSpec,
    navigating_urls,
)

logger = logging.getLogger("coupons.scanner")

_BLOCK_STATUSES = frozenset({403, 429})


class StoreBackoff:
    """Backoff de bloqueio por loja (runtime apenas, não persistido)."""

    def __init__(self, block_hours: int):
        self.block_hours = block_hours
        self.blocked_until: Dict[str, float] = {}

    def is_blocked(self, store_id: str, now: float) -> bool:
        return now < self.blocked_until.get(store_id, 0)

    def mark_blocked(self, store_id: str) -> None:
        self.blocked_until[store_id] = time.time() + self.block_hours * 3600


class Scanner:
    def __init__(self, stores: Dict[str, StoreSpec], store: CouponStore,
                 config: Dict[str, Any], backoff: StoreBackoff) -> None:
        self.stores = stores
        self.store = store
        self.pw_cfg = config.get("playwright", {})
        self.block_markers = config.get("block_markers", [])
        self.scancfg = config.get("scanner", {})
        self.backoff = backoff
        self.max_details = int(self.scancfg.get("max_product_details_per_store", 3))

    # ---- orquestração da rodada -------------------------------------------

    async def scan_all(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {}
        try:
            async with EdgeCdpProcess(
                port=int(self.pw_cfg.get("edge_cdp_port", 9224)),
                executable=self.pw_cfg.get("edge_executable"),
                headless=self.pw_cfg.get("headless", True),
                startup_timeout_seconds=float(
                    self.pw_cfg.get("edge_startup_timeout_seconds", 30.0)
                ),
            ) as cdp_url:
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(cdp_url)
                    try:
                        for store_id, spec in self.stores.items():
                            summary[store_id] = await self._scan_store(browser, spec)
                    finally:
                        try:
                            # Desconecta a sessão Playwright; o processo do
                            # Edge é encerrado pelo `EdgeCdpProcess` acima
                            # (connect_over_cdp NUNCA mata o processo real).
                            await browser.close()
                        except Exception:
                            logger.warning("Falha ao desconectar do Edge (processo será encerrado de qualquer forma).")
        except EdgeLaunchError as e:
            logger.error("Não consegui iniciar/conectar ao Edge dedicado: %s", e)
            raise
        except Exception as e:
            logger.error("Playwright falhou na rodada inteira: %s", e)
            raise
        summary["total_coupons_persisted"] = sum(
            v.get("coupons_persisted_count", 0)
            for k, v in summary.items() if isinstance(v, dict)
        )
        return summary

    async def _scan_store(self, browser: Any, spec: StoreSpec) -> Dict[str, Any]:
        store_id = spec.id
        result: Dict[str, Any] = {
            "store_id": store_id,
            "status": "ok",
            "sources": [],
            "coupons_persisted_count": 0,
            "product_details_opened": 0,
        }

        if self.backoff.is_blocked(store_id, time.time()):
            result["status"] = "backed_off"
            logger.info("Loja %s em backoff de bloqueio; pulando.", store_id)
            return result

        context = page = None
        try:
            context = await browser.new_context(
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            product_hints: List[Tuple[str, str]] = []  # (url, texto_do_card)

            for src in spec.sources:
                if src.kind == "product":
                    continue  # aprofundamento: tratado ao final, sob demanda
                records = await self._scan_source(page, spec, src, product_hints)
                result["sources"].extend(records)
                if any(r.get("blocked") for r in records):
                    result["status"] = "blocked"
                    break

            # Aprofunda SOMENTE se houve indício de cupom em cards.
            if result["status"] != "blocked" and product_hints:
                await self._deepen(page, spec, product_hints, result)

            # Agrega cupons persistidos de TODAS as fontes (incluindo deepening)
            result["coupons_persisted_count"] = sum(
                s.get("persisted", 0) for s in result.get("sources", [])
            )
        except Exception as e:
            logger.error("Loja %s: erro na varredura: %s", store_id, e)
            result["status"] = "error"
            result["error"] = str(e)
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    logger.warning("Loja %s: contexto não pôde ser fechado.", store_id)

        return result

    # ---- fonte individual -------------------------------------------------

    async def _scan_source(self, page: Any, spec: StoreSpec, src: SourceSpec,
                           product_hints: List[Tuple[str, str]]) -> List[Dict]:
        """Varre uma fonte; devolve um registro por URL visitada (ou um único
        para a fonte que reusa a página atual)."""
        urls = navigating_urls(spec, src)
        if urls:
            records = []
            for url in urls:
                records.append(await self._examine_url(page, spec, src, url, product_hints))
            return records
        # Fontes sem URL (banners/cards) reusam a página atual.
        return [await self._examine_current(page, spec, src, product_hints)]

    async def _examine_url(self, page: Any, spec: StoreSpec, src: SourceSpec,
                           url: str, product_hints: List[Tuple[str, str]]) -> Dict:
        """Navega até ``url`` e examina a fonte ali."""
        rec = self._new_record(src, url)
        timeout = self.pw_cfg.get("navigation_timeout_ms", 30000)
        try:
            resp = await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            rec["status"] = resp.status if resp else None
            if await self._mark_blocked(page, spec.id, rec):
                return rec
            # Nível de página inteira
            await self._examine_body_level(page, spec, src.kind, url, rec)
            # Se a fonte tem seletor (busca, cards, etc.), extrai os cards/resultados
            if src.selector and src.kind in ("search", "cards", "banners"):
                await self._examine_cards(page, spec, src, url, rec, product_hints)
        except Exception as e:
            rec["status"] = "error"
            rec["note"] = f"{type(e).__name__}: {e}"
            logger.info("Loja %s: fonte %s: %s", spec.id, src.label, e)
        return rec

    async def _examine_current(self, page: Any, spec: StoreSpec, src: SourceSpec,
                               product_hints: List[Tuple[str, str]]) -> Dict:
        """Examina a fonte sobre a página atual (banners/cards sem URL própria)."""
        url = self._page_url(page)
        rec = self._new_record(src, url, current=True)
        if src.kind in CARD_KINDS and src.selector:
            await self._examine_cards(page, spec, src, url, rec, product_hints)
        else:
            await self._examine_body_level(page, spec, src.kind, url, rec)
        return rec

    def _new_record(self, src: SourceSpec, url: str, current: bool = False) -> Dict:
        return {
            "kind": src.kind, "label": src.label, "url": url,
            "status": "current" if current else None,
            "coupon_hint": False, "deepened": False,
            "evidence": [], "persisted": 0, "blocked": False,
        }

    def _page_url(self, page: Any) -> str:
        try:
            return page.url or ""
        except Exception:
            return ""

    # ---- examinação por nível ---------------------------------------------

    async def _examine_body_level(self, page: Any, spec: StoreSpec, src_kind: str,
                                  url: str, rec: Dict) -> None:
        """Nível de página inteira (home/coupons/banners): escopo da loja."""
        body = await self._body_text(page)
        if has_coupon_hint(body):
            rec["coupon_hint"] = True
        for coupon in build_coupons(spec.id, src_kind, body, url):
            rec["evidence"].append(coupon.raw_rule_text or coupon.evidence)
            self._persist(coupon, rec)

    async def _examine_cards(self, page: Any, spec: StoreSpec, src: SourceSpec,
                             url: str, rec: Dict,
                             product_hints: List[Tuple[str, str]]) -> None:
        """Nível de cards: cada card com indicação de cupom vira observação de
        escopo 'product' e candidato a aprofundamento."""
        try:
            cards = await page.query_selector_all(src.selector)
        except Exception:
            cards = []
        for card in cards:
            text = await self._card_text(card)
            if not has_coupon_hint(text):
                continue
            rec["coupon_hint"] = True
            href = await self._card_href(card, url)
            for coupon in build_coupons(spec.id, src.kind, text, href or url or None):
                rec["evidence"].append(coupon.raw_rule_text or coupon.evidence)
                self._persist(coupon, rec)
            if href and not any(h == href for h, _ in product_hints):
                product_hints.append((href, text))

    # ---- aprofundamento ---------------------------------------------------

    async def _deepen(self, page: Any, spec: StoreSpec,
                      product_hints: List[Tuple[str, str]], result: Dict) -> None:
        """Abre detalhe do produto SOMENTE para indícios de cupom, limitado."""
        store_id = spec.id
        opened = 0
        for href, card_text in product_hints:
            if opened >= self.max_details:
                break
            rec = {
                "kind": "product", "label": "detalhe-produto", "url": href,
                "status": None, "coupon_hint": has_coupon_hint(card_text),
                "deepened": True, "evidence": [], "persisted": 0, "blocked": False,
            }
            timeout = self.pw_cfg.get("navigation_timeout_ms", 30000)
            try:
                resp = await page.goto(href, timeout=timeout, wait_until="domcontentloaded")
                rec["status"] = resp.status if resp else None
                if await self._mark_blocked(page, store_id, rec):
                    result["sources"].append(rec)
                    break
                body = await self._body_text(page)
                for coupon in build_coupons(store_id, "product", body, href):
                    rec["evidence"].append(coupon.raw_rule_text or coupon.evidence)
                    self._persist(coupon, rec)
                result["sources"].append(rec)
                opened += 1
            except Exception as e:
                rec["status"] = "error"
                rec["note"] = f"{type(e).__name__}: {e}"
                result["sources"].append(rec)
                logger.info("Loja %s: detalhe %s: %s", store_id, href, e)
        result["product_details_opened"] = opened

    # ---- bloqueio / utilidades ---------------------------------------------

    async def _mark_blocked(self, page: Any, store_id: str, rec: Dict) -> bool:
        status = rec.get("status")
        if status in _BLOCK_STATUSES:
            self.backoff.mark_blocked(store_id)
            rec["blocked"] = True
            rec["note"] = f"status {status}"
            logger.warning("Loja %s: bloqueio por status %s.", store_id, status)
            return True
        title = await self._page_title(page)
        body = await self._body_text(page)
        if self._page_has_block_marker(title, body):
            self.backoff.mark_blocked(store_id)
            rec["blocked"] = True
            rec["note"] = "marcador de bloqueio na página"
            logger.warning("Loja %s: bloqueio por marcador anti-bot.", store_id)
            return True
        return False

    def _page_has_block_marker(self, title: str, body: str) -> bool:
        haystack = (title + "\n" + body).lower()
        return any(m in haystack for m in self.block_markers)

    async def _page_title(self, page: Any) -> str:
        try:
            return (await page.title()) or ""
        except Exception:
            return ""

    async def _body_text(self, page: Any) -> str:
        try:
            return (await page.inner_text("body")) or ""
        except Exception:
            return ""

    async def _card_text(self, card: Any) -> str:
        try:
            return (await card.inner_text()) or ""
        except Exception:
            return ""

    async def _card_href(self, card: Any, base: str) -> Optional[str]:
        try:
            href = await card.get_attribute("href")
        except Exception:
            href = None
        if not href:
            try:
                link = await card.query_selector("a[href]")
                href = await link.get_attribute("href") if link else None
            except Exception:
                href = None
        if href and not href.startswith("http"):
            try:
                href = urljoin(base, href)
            except Exception:
                pass
        return href or None

    def _persist(self, coupon, rec: Dict) -> None:
        self.store.upsert(coupon)
        rec["persisted"] += 1
