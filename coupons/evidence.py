"""Detecção e escopo de evidência de cupom a partir do texto capturado.

Só extrai o que está literalmente na página/oferta (regras por loja da
auditoria da TASK-106). O parsing (``find_coupon_markers``) devolve ACHADOS
brutos (código, valor, %, texto literal); a atribuição de ESCOPO é separada
(``build_coupon``) e depende do contexto da fonte onde o achado apareceu.

REGRA ABSOLUTA DE ESCOPO: aparecer em vários cards NÃO significa store_wide.
Só usamos ``store_wide`` quando o próprio texto da evidência indica abrangência
geral. Caso contrário, o escopo é o comprovado pelo contexto (product, quando a
evidência está num card/detalhe de produto) ou ``None`` (unknown).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .persistence import Coupon

# Marcadores literais de texto de cupom por loja (ASCII + re.I; pt-BR escreve
# "cupom" sem acento).
STORE_MARKER = {
    "amazon": re.compile(r"cupom\s+de\s+r\$\s*([\d.,]+)\s+de\s+desconto", re.I),
    "amazon_pct": re.compile(r"cupom\s+de\s+(\d+(?:[.,]\d+)?)\s*%\s+de\s+desconto", re.I),
    "kabum": re.compile(r"\bSELO\s*:\s*CUPOM\s+(\w+)\b", re.I),
    "magalu": re.compile(r"cupom\s+r\$\s*([\d.,]+)\s+OFF", re.I),
    "mercadolivre": re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*OFF\s+com\s+(?:c[oO]upom|cupom\b)", re.I),
}

_HAS_COUPON = re.compile(r"cupom", re.I)

# Linguagem que indica abrangência GERAL na própria evidência (semântica store-wide).
STORE_WIDE_MARKERS = (
    "toda a loja", "loja inteira", "todo o site", "site inteiro",
    "vale para tudo", "válido em toda a loja", "válido para toda a loja",
    "vale para toda a loja", "válido para todos os produtos",
    "vale para todos os produtos", "promoção geral", "storewide", "store wide",
    "vale para a loja inteira", "todas as lojas",
)


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _parse_store(store_id: str, text: str) -> List[Dict]:
    """Parsing lite do texto: devolve achados cru (sem escopo ainda)."""
    found: List[Dict] = []

    if store_id == "amazon":
        for m in STORE_MARKER["amazon"].finditer(text):
            value = _to_float(m.group(1))
            if value is None:
                continue
            found.append({
                "code": None, "discount_kind": "fixed_amount",
                "discount_value": value, "raw_rule_text": m.group(0),
            })
        for m in STORE_MARKER["amazon_pct"].finditer(text):
            value = _to_float(m.group(1))
            if value is None:
                continue
            found.append({
                "code": None, "discount_kind": "percentage",
                "discount_value": value, "raw_rule_text": m.group(0),
            })
    elif store_id == "kabum":
        for m in STORE_MARKER["kabum"].finditer(text):
            found.append({
                "code": m.group(1), "discount_kind": None,
                "discount_value": None, "raw_rule_text": m.group(0),
            })
    elif store_id == "magalu":
        for m in STORE_MARKER["magalu"].finditer(text):
            value = _to_float(m.group(1))
            if value is None:
                continue
            found.append({
                "code": None, "discount_kind": "fixed_amount",
                "discount_value": value, "raw_rule_text": m.group(0),
            })
    elif store_id == "mercadolivre":
        for m in STORE_MARKER["mercadolivre"].finditer(text):
            value = _to_float(m.group(1))
            if value is None:
                continue
            found.append({
                "code": None, "discount_kind": "percentage",
                "discount_value": value, "raw_rule_text": m.group(0),
            })
    return found


def _evidence_key(store_id: str, source_kind: str, finding: Dict, \
                  reference_url: Optional[str]) -> str:
    """Chave de evidência (para dedup no SQLite).

    Inclui a URL de referência: a MESMA promoção em produtos diferentes vira
    observações distintas (escopo product), em vez de serem fundidas e
    erroneamente tratadas como store_wide.
    """
    code = finding.get("code") or (finding.get("raw_rule_text") or "").strip()
    return f"{store_id}:{source_kind}:{code}:{reference_url or 'noref'}"


def find_coupon_markers(store_id: str, text: str) -> List[Dict]:
    """Achados de cupom no texto capturado (vazio se não houver 'cupom')."""
    if not text or not _HAS_COUPON.search(text):
        return []
    return _parse_store(store_id, text)


def _context_window(text: str, raw: str, pad: int = 140) -> str:
    """Janela de texto ao redor do achado, para capturar linguagem de
    abrangência geral que viva na MESMA frase/sentença do cupom."""
    idx = text.lower().find((raw or "").lower())
    if idx < 0:
        return text[:500]
    return text[max(0, idx - pad): idx + len(raw) + pad]


def infer_scope(source_kind: str, context_text: str, reference_url: Optional[str]):
    """Define (scope_kind, scope_reference) a partir do contexto da fonte.

    - ``store_wide``: SOMENTE se o texto (a frase ao redor do achado) indicar
      abrangência geral na própria evidência oficial.
    - ``product``: evidência encontrada num card/busca/detalhe de produto,
      referenciada à URL daquele produto.
    - ``None`` (unknown): contexto de nível da loja (home/banner/área de cupons)
      sem linguagem de abrangência geral.
    """
    low = (context_text or "").lower()
    for marker in STORE_WIDE_MARKERS:
        if marker in low:
            return "store_wide", None
    if source_kind in ("cards", "search", "product"):
        return "product", reference_url
    return None, None


def build_coupons(store_id: str, source_kind: str, text: str,
                  reference_url: Optional[str]) -> List[Coupon]:
    """Constrói Coupons com escopo correto a partir do texto de uma fonte."""
    coupons: List[Coupon] = []
    for f in find_coupon_markers(store_id, text):
        scope_kind, scope_ref = infer_scope(
            source_kind, _context_window(text, f.get("raw_rule_text")), reference_url)
        coupons.append(Coupon(
            store_id=store_id,
            code=f.get("code"),
            discount_kind=f.get("discount_kind"),
            discount_value=f.get("discount_value"),
            evidence=_evidence_key(store_id, source_kind, f, reference_url),
            scope_kind=scope_kind,
            scope_reference=scope_ref,
            raw_rule_text=f.get("raw_rule_text"),
            source_url=reference_url or "",
        ))
    return coupons


def has_coupon_hint(text: str) -> bool:
    """True se o texto contém alguma âncora de cupom (para decidir aprofundar)."""
    return bool(text) and bool(_HAS_COUPON.search(text))
