"""Lojas monitoradas: fontes de descoberta de cupom por loja (configuráveis).

Nenhuma loja tem um único ponto fixo de descoberta. Cada provider conhece uma
lista ORDENADA de ``sources`` (home -> area de cupons -> banners/carrosseis ->
cards -> busca -> detalhe do produto). A ordem importa: o scanner percorre na
ordem configurada e só aprofunda (detalhe do produto) quando há indício de
cupom.

Cada source tem um ``kind``:
- home    : página inicial (navega a ``url`` e varre o corpo).
- coupons : área/página oficial de cupons (navega a ``url``).
- banners : banners/carrosséis promocionais (reusa a página atual; varre o seletor).
- cards   : cards de produtos/ofertas (reusa a página atual; varre o seletor).
- search  : busca por termos (gera uma URL por termo; varre o seletor).
- product : detalhe do produto (só quando um card indicou cupom).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, quote_plus

# Kinds de source que NAVEGAM para uma URL própria (as demais reusam a página atual).
NAVIGATING_KINDS = frozenset({"home", "coupons", "search"})
# Kinds cujo contexto é 'de nível da loja' (não ligado a um produto específico).
STORE_LEVEL_KINDS = frozenset({"home", "coupons", "banners"})
# Kinds que produzem evidência ligada a um produto/card (escopo 'product').
CARD_KINDS = frozenset({"cards", "search", "product"})


@dataclass
class SourceSpec:
    kind: str
    label: str
    url: Optional[str] = None
    selector: Optional[str] = None
    terms: List[str] = field(default_factory=list)
    # Achado real (2026-09-10, auditoria completa do sistema de cupons):
    # algumas lojas mostram o código do cupom só dentro do VALOR de um
    # <input readonly> (ex.: Magalu, data-testid="coupon-code-input") --
    # nunca aparece em `innerText`/`textContent`, então nenhum regex de
    # texto consegue achar. Quando configurado, o scanner faz uma consulta
    # de DOM adicional (`element.value`) nessa fonte, além da varredura de
    # texto normal -- opcional, só para as lojas onde já foi confirmado ao
    # vivo que o código mora nesse tipo de elemento.
    code_selector: Optional[str] = None


@dataclass
class StoreSpec:
    id: str
    enabled: bool
    sources: List[SourceSpec] = field(default_factory=list)


def build_search_url(store_id: str, term: str) -> Optional[str]:
    """Monta a URL de busca (mesma forma do provider de coleta do projeto)."""
    term = term.strip()
    if not term:
        return None
    slug = quote(term.replace(" ", "-"))
    if store_id == "kabum":
        facet = "eyJrYWJ1bV9wcm9kdWN0IjpbInRydWUiXX0="
        return f"https://www.kabum.com.br/busca/{slug}?facet_filters={facet}"
    if store_id == "amazon":
        return f"https://www.amazon.com.br/s?k={quote_plus(term)}"
    if store_id == "magalu":
        return f"https://www.magazineluiza.com.br/busca/{quote_plus(term)}/"
    return None


def _parse_source(raw: Dict[str, Any]) -> SourceSpec:
    kind = raw.get("kind")
    if kind not in {"home", "coupons", "banners", "cards", "search", "product"}:
        raise ValueError(f"kind de source inválido: {kind!r}")
    return SourceSpec(
        kind=kind,
        label=raw.get("label", kind),
        url=raw.get("url"),
        selector=raw.get("selector"),
        terms=list(raw.get("terms", [])),
        code_selector=raw.get("code_selector"),
    )


def load_stores(config: Dict[str, Any]) -> Dict[str, StoreSpec]:
    """Carrega as lojas habilitadas com suas fontes de descoberta ordenadas."""
    specs: Dict[str, StoreSpec] = {}
    raw = config.get("stores", {})
    for store_id, s in raw.items():
        if not s.get("enabled", True):
            continue
        sources = [_parse_source(x) for x in s.get("sources", [])]
        if not sources:
            raise ValueError(f"loja {store_id!r} sem fontes de descoberta (sources)")
        if not any(x.kind == "product" for x in sources):
            # Garante que o aprofundamento esteja definido, mesmo sem config explícita.
            sources.append(SourceSpec(kind="product", label="detalhe-produto"))
        specs[store_id] = StoreSpec(id=store_id, enabled=True, sources=sources)
    return specs


def navigating_urls(spec: StoreSpec, src: SourceSpec) -> List[str]:
    """URLs que uma fonte NAVEGA (home/coupons = url; search = uma por termo).

    Fontes sem URL (banners/cards) retornam vazio -> o scanner reusa a página
    atual e apenas varre o seletor sobre ela.
    """
    if src.kind == "search":
        urls = []
        for t in src.terms:
            u = build_search_url(spec.id, t)
            if u:
                urls.append(u)
        return urls
    if src.url:
        return [src.url]
    return []
