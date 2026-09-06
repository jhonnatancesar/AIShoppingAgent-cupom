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
from .stores import CARD_KINDS

# Marcadores literais de texto de cupom por loja (ASCII + re.I; pt-BR escreve
# "cupom" sem acento).
STORE_MARKER = {
    "amazon": re.compile(r"cupom\s+de\s+r\$\s*([\d.,]+)\s+de\s+desconto", re.I),
    "amazon_pct": re.compile(r"cupom\s+de\s+(\d+(?:[.,]\d+)?)\s*%\s+de\s+desconto", re.I),
    # Fraseado real confirmado ao vivo (2026-08-30): a Amazon mudou a UI de
    # cards de busca desde a auditoria original -- hoje mostra só o preço
    # final já com o clip coupon aplicado ("Você paga R$X,XX com o
    # cupom"), nunca o valor do desconto isolado. Sem código (clip coupon,
    # aplicado automaticamente, igual já registrado na auditoria original).
    # Nunca inferimos o valor do desconto comparando com outro preço do
    # card (ambíguo, teria preço "à vista"/parcelado/etc. misturados) --
    # só a evidência literal do preço final vira `raw_rule_text`.
    "amazon_final_price": re.compile(r"voc[eê]\s+paga\s+r\$\s*([\d.,]+)\s+com\s+o\s+cupom", re.I),
    "kabum": re.compile(r"\bSELO\s*:\s*CUPOM\s+(\w+)\b", re.I),
    "magalu": re.compile(r"cupom\s+r\$\s*([\d.,]+)\s+OFF", re.I),
    # Percentual (achado ao vivo 2026-08-30 em cards de busca reais -- o
    # padrão fixo acima nunca cobriu isso, só valor em R$).
    "magalu_pct": re.compile(r"cupom\s+(\d+(?:[.,]\d+)?)\s*%\s*OFF", re.I),
    # Mercado Livre tem 4 fraseados reais confirmados (diagnóstico ao vivo,
    # 2026-08-30, sessão autenticada): carrossel da home ("R$X OFF com
    # Cupom" / "X% OFF com Cupom", cupom DEPOIS) e cards da página /cupons
    # ("Cupom X% OFF ..." / "Cupom R$X OFF ...", cupom ANTES; card real
    # inclui "Cupom ativado de X% OFF em produtos de <vendedor>").
    "mercadolivre_pct_after": re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*OFF\s+com\s+cupom", re.I),
    "mercadolivre_fixed_after": re.compile(r"R\$\s*([\d.,]+)\s*OFF\s+com\s+cupom", re.I),
    "mercadolivre_pct_before": re.compile(r"cupom\s+(?:ativado\s+de\s+)?(\d+(?:[.,]\d+)?)\s*%\s*OFF", re.I),
    "mercadolivre_fixed_before": re.compile(r"cupom\s+r\$\s*([\d.,]+)\s*OFF", re.I),
    # Campos estruturados adicionais, presentes nos cards de /cupons --
    # nunca disparam achado sozinhos, só complementam um achado já feito.
    "mercadolivre_min_purchase": re.compile(r"compra\s+m[ií]nima\s*r\$\s*([\d.,]+)", re.I),
    "mercadolivre_limit": re.compile(r"limite\s+de\s*r\$\s*([\d.,]+)", re.I),
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
        for m in STORE_MARKER["amazon_final_price"].finditer(text):
            # Evidência real de clip coupon (preço final, sem valor de
            # desconto isolado declarado) -- nunca inventa discount_value
            # comparando com outro preço ambíguo do card.
            found.append({
                "code": None, "discount_kind": None,
                "discount_value": None, "raw_rule_text": m.group(0),
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
        for m in STORE_MARKER["magalu_pct"].finditer(text):
            value = _to_float(m.group(1))
            if value is None:
                continue
            found.append({
                "code": None, "discount_kind": "percentage",
                "discount_value": value, "raw_rule_text": m.group(0),
            })
    elif store_id == "mercadolivre":
        extras = _mercadolivre_extras(text)
        for key, kind in (
            ("mercadolivre_pct_after", "percentage"),
            ("mercadolivre_fixed_after", "fixed_amount"),
            ("mercadolivre_pct_before", "percentage"),
            ("mercadolivre_fixed_before", "fixed_amount"),
        ):
            for m in STORE_MARKER[key].finditer(text):
                value = _to_float(m.group(1))
                if value is None:
                    continue
                found.append({
                    "code": None, "discount_kind": kind,
                    "discount_value": value, "raw_rule_text": m.group(0),
                    **extras,
                })
    return found


def _mercadolivre_extras(text: str) -> Dict[str, Optional[float]]:
    """Campos estruturados adicionais já presentes nos cards de
    /cupons -- "Compra mínima R$X" e "Limite de R$X" -- aplicados a
    qualquer achado da MESMA fonte (card já é um único cupom, não há
    ambiguidade de qual achado eles pertencem)."""
    extras: Dict[str, Optional[float]] = {}
    m_min = STORE_MARKER["mercadolivre_min_purchase"].search(text)
    if m_min:
        extras["minimum_purchase_amount"] = _to_float(m_min.group(1))
    m_limit = STORE_MARKER["mercadolivre_limit"].search(text)
    if m_limit:
        extras["maximum_discount_amount"] = _to_float(m_limit.group(1))
    return extras


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


# Sinal de status/validade -- genérico, vale pra qualquer loja (não é
# fraseado específico de uma loja só). "Vence"/"válido até" nunca decidem
# status estruturado sozinhos (seria inferir data relativa tipo
# "amanhã") -- só anexam o texto LITERAL ao raw_rule_text, como sempre.
#
# "esgotad[oa]" (tempo passado/presente -- já esgotou) é diferente de
# "está esgotando" (ainda em andamento, cupom pode continuar válido
# hoje) -- só o primeiro é tratado como sinal DEFINITIVO de
# indisponibilidade (ver `_is_exhausted`), reaproveitando o status
# `"expired"` que já existe (nunca um estado novo).
_EXHAUSTED_PATTERN = re.compile(r"esgotad[oa]\S*", re.I)
_STATUS_HINT_PATTERNS = (
    re.compile(r"est[aá]\s+esgotando\S*", re.I),
    _EXHAUSTED_PATTERN,
    re.compile(r"vence\s+[^\n.!]{0,40}", re.I),
    re.compile(r"v[aá]lido\s+at[eé]\s+[^\n.!]{0,40}", re.I),
)


def _find_status_hint(text: str) -> Optional[str]:
    for pattern in _STATUS_HINT_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return None


def _is_exhausted(text: str, *, source_kind: str) -> bool:
    """Só confia no sinal "esgotado" quando o texto já está ESCOPADO a um
    card/produto específico (`CARD_KINDS` -- `cards`/`search`/`product`,
    `coupons/stores.py`). Fontes de nível de loja (`home`/`coupons`/
    `banners`, `STORE_LEVEL_KINDS`) varrem a PÁGINA INTEIRA -- "esgotado"
    ali pode se referir a um produto qualquer sem relação com o cupom
    encontrado na mesma página; nunca decide status a partir desse
    escopo largo demais (permanece só como texto em `raw_rule_text`,
    igual a antes desta correção)."""
    return source_kind in CARD_KINDS and bool(_EXHAUSTED_PATTERN.search(text))


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
    status_hint = _find_status_hint(text)  # mesmo card/texto, não por achado
    exhausted = _is_exhausted(text, source_kind=source_kind)
    for f in find_coupon_markers(store_id, text):
        scope_kind, scope_ref = infer_scope(
            source_kind, _context_window(text, f.get("raw_rule_text")), reference_url)
        raw_rule_text = f.get("raw_rule_text") or ""
        if status_hint:
            # Texto LITERAL, nunca inferido (ex.: "Está esgotando!",
            # "Vence amanhã", "Válido até 31/12") -- sempre anexado à
            # evidência, além de eventualmente decidir `status` abaixo.
            raw_rule_text = f"{raw_rule_text} | {status_hint}"
        coupons.append(Coupon(
            store_id=store_id,
            code=f.get("code"),
            discount_kind=f.get("discount_kind"),
            discount_value=f.get("discount_value"),
            minimum_purchase_amount=f.get("minimum_purchase_amount"),
            maximum_discount_amount=f.get("maximum_discount_amount"),
            evidence=_evidence_key(store_id, source_kind, f, reference_url),
            scope_kind=scope_kind,
            scope_reference=scope_ref,
            raw_rule_text=raw_rule_text,
            source_url=reference_url or "",
            # Reaproveita "expired" (nenhum status novo): a fonte já
            # informou literalmente que esgotou, só quando o escopo é
            # confiável o bastante para acreditar que é ESTE cupom
            # (ver `_is_exhausted`).
            status="expired" if exhausted else "active",
        ))
    return coupons


def has_coupon_hint(text: str) -> bool:
    """True se o texto contém alguma âncora de cupom (para decidir aprofundar)."""
    return bool(text) and bool(_HAS_COUPON.search(text))
