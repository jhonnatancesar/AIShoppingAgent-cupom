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
    # cupom"), nunca o valor do desconto isolado. Sem código nesta
    # evidência -- correção (2026-09-10): isso descreve só o que foi
    # OBSERVADO nesta fonte, nunca uma conclusão de que o cupom é sempre
    # auto-aplicado ou nunca precisa de ativação; a Amazon também usa
    # códigos/ativações em outras superfícies, só não apareceram aqui.
    # Nunca inferimos o valor do desconto comparando com outro preço do
    # card (ambíguo, teria preço "à vista"/parcelado/etc. misturados) --
    # só a evidência literal do preço final vira `raw_rule_text`.
    "amazon_final_price": re.compile(r"voc[eê]\s+paga\s+r\$\s*([\d.,]+)\s+com\s+o\s+cupom", re.I),
    # Achado real (auditoria 2026-09-10, autorizado pelo usuário): o card
    # real também mostra "Por: R$X" (preço já em oferta, ANTES do cupom)
    # perto de "Você paga com o cupom" -- a diferença é a economia REAL
    # do cupom. Nunca usamos "De:" (pode incluir a promoção normal do
    # produto, não só o efeito do cupom) -- ver `_amazon_economia`.
    "amazon_por_price": re.compile(r"\bpor:?\s*r\$\s*([\d.,]+)", re.I),
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


# Correção real (revisão 2026-09-10, segunda rodada): nem todo `CARD_KINDS`
# tem isolamento de DOM real. `cards`/`search` (`scanner.py:_examine_cards`)
# extraem o texto via `card.inner_text()` -- escopado ao elemento HTML de
# UM produto só, texto de outro produto não pode vazar pra dentro por
# construção. `product` (deepening, `scanner.py:_deepen`) usa `body.
# inner_text()` da página INTEIRA -- inclui carrosséis de "produtos
# relacionados"/"compre também", cujos preços entram no MESMO texto. Um
# boundary-cap textual (limitar até o próximo marcador de cupom) não
# fecha esse buraco: um "Por:" de um produto relacionado, sem NENHUM outro
# marcador de cupom entre os dois, ainda cairia dentro da janela. Por
# isso a derivação de economia fica restrita a fontes com isolamento de
# DOM comprovado -- nunca `product`.
_AMAZON_ECONOMIA_SAFE_KINDS = frozenset({"cards", "search"})


def _amazon_economia(
    text: str, final_price_match_end: int, final_price: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    """Economia real do cupom = Por - Você paga com cupom (autorizado
    explicitamente, 2026-09-10) -- NUNCA usa "De:" (pode incluir a
    promoção normal do produto, não só o efeito do cupom).

    Chamada SOMENTE quando `source_kind` já garante isolamento de DOM
    real (`_AMAZON_ECONOMIA_SAFE_KINDS` -- ver `_parse_store`), nunca
    numa fonte de página inteira. Mesmo dentro desse escopo, a janela de
    busca nunca cruza pro próximo achado de "você paga...com o cupom" no
    mesmo texto -- defesa adicional caso um único card contenha mais de
    uma menção (ex.: variações de quantidade), nunca o mecanismo
    principal de isolamento (esse é o `source_kind` restrito acima).

    Só retorna valor quando:
    - "Por:" aparece pouco depois do preço final, ANTES de qualquer
      próxima ocorrência de "você paga...com o cupom" no mesmo texto;
    - a diferença é positiva (preço final com cupom realmente menor que
      o preço sem cupom já em oferta).
    Devolve (preco_por, economia) -- os dois `None` quando as condições
    não forem atendidas (nunca inventa, nunca usa "De")."""
    if final_price is None:
        return None, None
    next_match = STORE_MARKER["amazon_final_price"].search(text, final_price_match_end)
    boundary = next_match.start() if next_match else len(text)
    window_end = min(final_price_match_end + 120, boundary)
    window = text[final_price_match_end:window_end]
    m = STORE_MARKER["amazon_por_price"].search(window)
    if not m:
        return None, None
    por_price = _to_float(m.group(1))
    if por_price is None:
        return None, None
    economia = round(por_price - final_price, 2)
    if economia <= 0:
        # Preço "final com cupom" não é realmente menor que "Por" --
        # nunca reporta economia negativa/zero como se fosse real.
        return None, None
    return por_price, economia


def _parse_store(store_id: str, text: str, source_kind: Optional[str] = None) -> List[Dict]:
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
            final_price = _to_float(m.group(1))
            # Correção real (revisão 2026-09-10, segunda rodada): "estar
            # em CARD_KINDS" NÃO é o mesmo que "estar isolado no card/
            # componente do produto". `cards`/`search` (`scanner.py:
            # _examine_cards`) usam `_card_text(card)` -- `card.
            # inner_text()` do elemento HTML de UM produto só, isolamento
            # de DOM real, texto de outro produto não pode vazar pra
            # dentro. `product` (deepening, `scanner.py:_deepen`) usa
            # `_body_text(page)` -- a página INTEIRA (`body.inner_text()`),
            # que inclui carrosséis de "produtos relacionados"/"compre
            # também" com preços de OUTROS produtos no mesmo texto. Nessa
            # fonte, nem o boundary-cap (limitar até o próximo "Você
            # paga...com o cupom") protege contra um "Por:" de um produto
            # relacionado que apareça perto sem nenhum outro marcador de
            # cupom entre os dois -- só o isolamento de DOM real evita
            # isso, e só `cards`/`search` têm essa garantia hoje. Por
            # isso a derivação fica restrita a `_AMAZON_ECONOMIA_SAFE_KINDS`
            # (subconjunto de `CARD_KINDS`, nunca `product`) -- fora
            # dela, mantém só a evidência literal (ver bloco `else`
            # abaixo), nunca expõe a diferença como `fixed_amount`.
            por_price, economia = (
                _amazon_economia(text, m.end(), final_price)
                if source_kind in _AMAZON_ECONOMIA_SAFE_KINDS
                else (None, None)
            )
            if economia is not None:
                # Autorizado explicitamente (2026-09-10): economia = Por
                # - Você paga com cupom, só quando "Por:" aparece na
                # MESMA vizinhança (mesmo produto, nunca cruza pro
                # próximo achado no texto), nunca "De:". `raw_rule_text`
                # preserva os DOIS preços (evidência literal), nunca só o
                # número derivado. `force_product_scope`: este achado só
                # tem sentido restrito a ESTE produto -- nunca herda
                # `store_wide` de uma linguagem de abrangência geral que
                # porventura apareça em outro trecho da mesma página
                # (ver `build_coupons`, força `scope_kind="product"`
                # direto, ignora `infer_scope` pra este achado específico).
                found.append({
                    "code": None, "discount_kind": "fixed_amount",
                    "discount_value": economia,
                    "raw_rule_text": (
                        f"{m.group(0)} | Por: R$ {por_price:.2f} "
                        f"(economia de R$ {economia:.2f} com o cupom, restrita a este produto)"
                    ),
                    "force_product_scope": True,
                })
            else:
                # Sem "Por:" confiável por perto, OU fora de
                # `_AMAZON_ECONOMIA_SAFE_KINDS` (inclui `product` --
                # página inteira, sem isolamento de DOM) -- preserva só
                # a evidência literal (preço final), nunca inventa
                # desconto comparando com outro preço ambíguo do card ou
                # de um produto relacionado na mesma página. Observação,
                # não conclusão: a ausência
                # de código aqui é só o que foi encontrado nesta
                # evidência -- não afirma que o cupom nunca precisa de
                # ativação nem que é sempre automático.
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


def find_coupon_markers(store_id: str, text: str, source_kind: Optional[str] = None) -> List[Dict]:
    """Achados de cupom no texto capturado (vazio se não houver 'cupom').

    `source_kind` (ver `coupons/stores.py`, `CARD_KINDS`/`STORE_LEVEL_KINDS`)
    é repassado até `_parse_store` pra restringir derivações que só são
    seguras quando o texto já está escopado a UM produto (ex.: economia
    da Amazon -- ver `_amazon_economia`). Opcional por compatibilidade;
    quando ausente, nenhuma derivação escopo-dependente é ativada."""
    if not text or not _HAS_COUPON.search(text):
        return []
    return _parse_store(store_id, text, source_kind)


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
# Validade explícita informada pela LOJA -- diferente de `last_seen_at`
# (só prova que o worker viu o cupom recentemente, nunca até quando ele
# continua válido). Texto LITERAL, nunca parseado em data (mesmo
# princípio de sempre: "vence amanhã" não vira uma data relativa
# inferida).
_VALIDITY_HINT_PATTERNS = (
    re.compile(r"vence\s+[^\n.!]{0,40}", re.I),
    re.compile(r"v[aá]lido\s+at[eé]\s+[^\n.!]{0,40}", re.I),
)
_STATUS_HINT_PATTERNS = (
    re.compile(r"est[aá]\s+esgotando\S*", re.I),
    _EXHAUSTED_PATTERN,
    *_VALIDITY_HINT_PATTERNS,
)


def _find_status_hint(text: str) -> Optional[str]:
    for pattern in _STATUS_HINT_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0).strip()
    return None


def _find_validity_hint(text: str) -> Optional[str]:
    """Achado real (auditoria 2026-09-10): `_find_status_hint` já
    encontrava "vence X"/"válido até X" no texto, mas o resultado só
    era anexado a `raw_rule_text` -- o campo dedicado `valid_until`
    (que já existe no modelo) nunca era preenchido em lugar nenhum.
    Bug real corrigido aqui, não uma lacuna de modelo."""
    for pattern in _VALIDITY_HINT_PATTERNS:
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
    validity_hint = _find_validity_hint(text)  # subconjunto de status_hint
    exhausted = _is_exhausted(text, source_kind=source_kind)
    for f in find_coupon_markers(store_id, text, source_kind):
        if f.get("force_product_scope"):
            # Achado que só faz sentido restrito a ESTE produto (ex.:
            # economia derivada da Amazon, `_amazon_economia`) -- nunca
            # herda `store_wide`/`None` de `infer_scope`, mesmo que
            # linguagem de abrangência geral apareça em outro trecho da
            # mesma página (proteção extra além da restrição por
            # `CARD_KINDS` já aplicada em `_parse_store`).
            scope_kind, scope_ref = "product", reference_url
        else:
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
            valid_until=validity_hint,
            source_url=reference_url or "",
            # Reaproveita "expired" (nenhum status novo): a fonte já
            # informou literalmente que esgotou, só quando o escopo é
            # confiável o bastante para acreditar que é ESTE cupom
            # (ver `_is_exhausted`).
            status="expired" if exhausted else "active",
        ))
    return coupons


# ---------------------------------------------------------------------------
# Widget estruturado (achado real, auditoria 2026-09-10): algumas lojas
# (confirmado ao vivo: Magalu) só expõem o código dentro do VALOR de um
# <input readonly> -- nunca aparece em innerText/textContent, então nenhum
# regex de texto genérico (find_coupon_markers/STORE_MARKER) consegue
# achar. O scanner faz a consulta de DOM (fora deste módulo, que é só
# parsing puro) e chama esta função com o texto JÁ ISOLADO do widget --
# aqui dentro não precisa da palavra "cupom" como âncora de segurança
# (o chamador já confirmou que é um widget de cupom real, não texto solto
# da página).
# ---------------------------------------------------------------------------

_WIDGET_FIXED_OFF = re.compile(r"r\$\s*([\d.,]+)\s*off", re.I)
_WIDGET_PCT_OFF = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*off", re.I)


def parse_widget_discount(widget_text: str) -> Dict[str, Optional[float]]:
    """Desconto a partir do texto isolado de um widget de cupom já
    confirmado (nunca da página inteira -- ver docstring acima). `R$0
    OFF`/`0% OFF` são tratados como AUSÊNCIA de valor confiável (achado
    real, Mercado Livre: um cupom "esgotado"/não ativado pelo usuário
    mostra literalmente "R$0 OFF" -- não é um desconto real de zero,
    é a origem não revelando o valor verdadeiro; nunca persistimos um
    zero que não é real)."""
    m = _WIDGET_FIXED_OFF.search(widget_text)
    if m:
        value = _to_float(m.group(1))
        if value and value > 0:
            return {"discount_kind": "fixed_amount", "discount_value": value}
    m = _WIDGET_PCT_OFF.search(widget_text)
    if m:
        value = _to_float(m.group(1))
        if value and value > 0:
            return {"discount_kind": "percentage", "discount_value": value}
    return {"discount_kind": None, "discount_value": None}


def build_widget_coupon(
    store_id: str, *, code: Optional[str], widget_text: str,
    reference_url: Optional[str],
) -> Coupon:
    """Cupom construído a partir de um widget estruturado (DOM), nunca de
    regex solto na página. `code` já vem do valor real do `<input>`
    (preservado intacto, capitalização incluída -- nunca normalizado).
    Escopo sempre `product` (o widget só existe na página de UM produto
    específico -- nunca inferimos abrangência maior a partir dele)."""
    discount = parse_widget_discount(widget_text)
    validity = _find_validity_hint(widget_text)
    evidence_discriminator = code or widget_text.strip()
    return Coupon(
        store_id=store_id,
        code=code,
        discount_kind=discount["discount_kind"],
        discount_value=discount["discount_value"],
        evidence=f"{store_id}:widget:{evidence_discriminator}:{reference_url or 'noref'}",
        scope_kind="product",
        scope_reference=reference_url,
        valid_until=validity,
        raw_rule_text=widget_text.strip() or None,
        source_url=reference_url or "",
        status="active",
    )


def has_coupon_hint(text: str) -> bool:
    """True se o texto contém alguma âncora de cupom (para decidir aprofundar)."""
    return bool(text) and bool(_HAS_COUPON.search(text))
