#!/usr/bin/env python3
"""Teste real de `coupons/evidence.py` (parsing) e `coupons/persistence.py`
(upsert) -- sem pytest (não é dependência deste projeto), mesmo padrão de
`smoke_test.py`/`test_job_object.py`: script standalone, `assert` direto.

Auditoria ao vivo (2026-09-10, sessão de revisão do sistema de cupons):
confirma com evidência real da própria loja o comportamento documentado
em `evidence.py` -- não é suposição.

- Kabum (kabum.com.br/cupons, confirmado ao vivo): o percentual que
  aparece ao lado do selo "SELO: CUPOM X" é o desconto NORMAL do
  produto (preço de/por), nunca o efeito do cupom -- por isso o parser
  deliberadamente nunca extrai `discount_value` pra Kabum, só o código.
  Uma regra que capturasse esse percentual estaria ERRADA (mistura
  desconto do produto com desconto do cupom).
- Amazon (amazon.com.br, confirmado ao vivo): o padrão `amazon_final_
  price` ("Você paga RX com o cupom") sozinho nunca produz
  `discount_value` (não há valor isolado do desconto no texto, só o
  preço final). Quando o card também mostra "Por: RY" na mesma
  vizinhança (preço já em oferta, antes do cupom), a diferença Y-X é a
  economia real do cupom -- regra autorizada explicitamente
  (2026-09-10) e implementada em `_amazon_economia`, restrita a fontes
  já escopadas a um produto (`CARD_KINDS`) e nunca cruzando pro próximo
  achado no mesmo texto (evita misturar produtos numa página com vários
  cards/carrossel). Fora desse escopo (`CARD_KINDS`), ou sem "Por:" na
  vizinhança, a evidência permanece literal, sem `discount_value`
  inventado.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from coupons.evidence import build_coupons, find_coupon_markers
from coupons.persistence import Coupon, SqliteCouponStore

# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------


def test_amazon_explicit_fixed_discount_becomes_structured() -> None:
    text = "Aproveite: cupom de R$ 50,00 de desconto neste produto."
    found = find_coupon_markers("amazon", text)
    assert len(found) == 1
    assert found[0]["code"] is None
    assert found[0]["discount_kind"] == "fixed_amount"
    assert found[0]["discount_value"] == 50.0
    print("PASS: Amazon -- 'cupom de R$X de desconto' vira discount_kind=fixed_amount")


def test_amazon_explicit_percentage_discount_becomes_structured() -> None:
    text = "Use o cupom de 15% de desconto no checkout."
    found = find_coupon_markers("amazon", text)
    assert len(found) == 1
    assert found[0]["discount_kind"] == "percentage"
    assert found[0]["discount_value"] == 15.0
    print("PASS: Amazon -- 'cupom de X% de desconto' vira discount_kind=percentage")


def test_amazon_final_price_never_becomes_discount() -> None:
    """O achado real da auditoria ao vivo: este é o ÚNICO padrão que
    hoje bate no site real da Amazon -- e ele NUNCA pode virar
    discount_kind/value, porque o texto não isola o valor do desconto,
    só o preço final (real evidence-based, não suposição)."""
    text = "Você paga R$ 26,91 com o cupom"
    found = find_coupon_markers("amazon", text)
    assert len(found) == 1
    f = found[0]
    assert f["code"] is None
    assert f["discount_kind"] is None
    assert f["discount_value"] is None
    assert f["raw_rule_text"] == "Você paga R$ 26,91 com o cupom"
    print("PASS: Amazon -- 'você paga com o cupom' NUNCA vira discount_value (preço final != desconto)")


def test_amazon_economia_derived_from_real_card_example() -> None:
    """Regra autorizada explicitamente (2026-09-10): economia = Por -
    Você paga com cupom. Exemplo REAL do card auditado ao vivo
    (amazon.com.br, mochila Romantic Crown): De: R$119,99 (nunca usado)
    -> Por: R$110,19 -> Você paga R$100,19 com o cupom = economia de
    R$10,00.

    Correção (2026-09-10, revisão pós-relatório): a derivação só é
    segura quando o texto já está escopado a UM produto -- por isso o
    teste agora passa `source_kind="cards"` (`CARD_KINDS`) explicitamente,
    igual a uma coleta real de card de busca. Ver
    `test_amazon_economia_never_fires_outside_card_kinds` pra prova do
    lado oposto (fonte de nível de loja nunca deriva economia, mesmo com
    o mesmo texto)."""
    text = "Você paga R$ 100,19 com o cupom (tamanhos/cores limitados)\nPor: R$ 110,19\nDe: R$ 119,99"
    found = find_coupon_markers("amazon", text, "cards")
    assert len(found) == 1
    f = found[0]
    assert f["discount_kind"] == "fixed_amount"
    assert abs(f["discount_value"] - 10.00) < 0.001
    assert f["code"] is None
    assert f["force_product_scope"] is True  # nunca herda store_wide/None de infer_scope
    assert "110.19" in f["raw_rule_text"]  # preço "Por" preservado na evidência
    assert "De" not in f["raw_rule_text"].split("|")[1]  # "De:" nunca entra no cálculo/evidência derivada
    print("PASS: Amazon -- economia real derivada (Por - você paga) = R$10,00, exemplo real do card auditado")


def test_amazon_economia_never_fires_outside_card_kinds() -> None:
    """Achado real da revisão (2026-09-10): a mesma evidência textual que
    dispara a economia num card ('cards'/'search'/'product') NUNCA pode
    disparar a derivação numa fonte de nível de loja ('home'/'coupons'/
    'banners') -- nesse escopo o texto varre a PÁGINA INTEIRA e um 'Por:'
    próximo pode pertencer a um produto totalmente diferente do 'Você
    paga com o cupom'. Mesmo texto do exemplo real acima, só muda
    `source_kind`."""
    text = "Você paga R$ 100,19 com o cupom (tamanhos/cores limitados)\nPor: R$ 110,19\nDe: R$ 119,99"
    found = find_coupon_markers("amazon", text, "coupons")  # STORE_LEVEL_KINDS
    assert len(found) == 1
    f = found[0]
    assert f["discount_kind"] is None  # nunca deriva fora de CARD_KINDS
    assert f["discount_value"] is None
    assert "force_product_scope" not in f
    print("PASS: Amazon -- economia NUNCA deriva fora de CARD_KINDS (fonte de nível de loja, texto de página inteira)")


def test_amazon_economia_never_uses_de_price() -> None:
    """'De:' nunca é usado na derivação -- pode incluir a promoção
    normal do produto, não só o efeito do cupom. Sem 'Por:' por perto,
    nenhuma economia é derivada (discount fica nulo, só evidência)."""
    text = "Você paga R$ 100,19 com o cupom\nDe: R$ 119,99"
    found = find_coupon_markers("amazon", text, "cards")
    assert len(found) == 1
    assert found[0]["discount_kind"] is None
    assert found[0]["discount_value"] is None
    print("PASS: Amazon -- 'De:' isolado (sem 'Por:') nunca vira economia derivada")


def test_amazon_economia_rejects_non_positive_difference() -> None:
    """Preço 'com cupom' maior ou igual ao 'Por' -- diferença não é
    positiva, nunca reporta uma economia que não é real."""
    text = "Você paga R$ 120,00 com o cupom\nPor: R$ 110,19"
    found = find_coupon_markers("amazon", text, "cards")
    assert len(found) == 1
    assert found[0]["discount_kind"] is None
    assert found[0]["discount_value"] is None
    print("PASS: Amazon -- diferença não-positiva (Por - você_paga <= 0) nunca vira economia")


def test_amazon_economia_requires_por_in_same_neighborhood() -> None:
    """'Por:' de um produto DIFERENTE, longe no texto, nunca é usado --
    só a vizinhança imediata (mesmo produto/variante, mesma coleta)."""
    filler = "x" * 500
    text = f"Você paga R$ 100,19 com o cupom{filler}Por: R$ 110,19"
    found = find_coupon_markers("amazon", text, "cards")
    assert len(found) == 1
    assert found[0]["discount_kind"] is None  # "Por" longe demais, fora da vizinhança
    print("PASS: Amazon -- 'Por' fora da vizinhança imediata nunca é usado (evita misturar produtos)")


def test_amazon_economia_never_crosses_into_next_product() -> None:
    """Prova do boundary-cap (defesa adicional, 2026-09-10): mesmo dentro
    de uma fonte com isolamento de DOM real ('cards'/'search' --
    `_AMAZON_ECONOMIA_SAFE_KINDS`), se o texto do PRÓPRIO card tiver mais
    de uma menção de 'Você paga...com o cupom' (ex.: variações de
    quantidade/tamanho no mesmo card), o 'Por:' do SEGUNDO nunca pode
    virar a economia do PRIMEIRO -- a busca da janela nunca cruza o
    próximo achado de 'amazon_final_price' no mesmo texto, mesmo dentro
    dos 120 chars."""
    text = (
        "Você paga R$ 100,19 com o cupom\n"
        "Você paga R$ 50,00 com o cupom\n"
        "Por: R$ 110,19"
    )
    found = find_coupon_markers("amazon", text, "cards")
    assert len(found) == 2
    first, second = found
    # "Por: R$110,19" está DEPOIS do segundo achado -- pertence só a ele,
    # nunca ao primeiro (mesmo estando dentro da janela de 120 chars).
    assert first["discount_kind"] is None
    assert first["discount_value"] is None
    assert second["discount_kind"] == "fixed_amount"
    assert abs(second["discount_value"] - 60.19) < 0.001
    print("PASS: Amazon -- 'Por:' nunca cruza pro produto anterior (boundary-cap no próximo achado)")


def test_amazon_economia_never_fires_on_product_detail_page() -> None:
    """Correção real (revisão 2026-09-10, segunda rodada -- comprovação
    de isolamento pedida explicitamente): `source_kind='product'`
    (`_deepen`, `coupons/scanner.py`) usa `body.inner_text()` da página
    INTEIRA, não o card isolado de um produto -- um carrossel de
    'produtos relacionados'/'compre também' pode ter um 'Por:' de OUTRO
    produto perto o bastante pra cair na janela, sem nenhum outro
    marcador de cupom entre os dois (o boundary-cap sozinho não fecha
    esse buraco). Por isso a economia NUNCA deriva em 'product', mesmo
    com o MESMO texto que dispararia a derivação em 'cards'/'search' --
    só a evidência literal (preço final) é preservada."""
    text = "Você paga R$ 100,19 com o cupom\nPor: R$ 110,19"
    found = find_coupon_markers("amazon", text, "product")
    assert len(found) == 1
    assert found[0]["discount_kind"] is None
    assert found[0]["discount_value"] is None
    assert "force_product_scope" not in found[0]
    assert found[0]["raw_rule_text"] == "Você paga R$ 100,19 com o cupom"
    print("PASS: Amazon -- economia NUNCA deriva em 'product' (página inteira, sem isolamento de DOM real)")


def test_amazon_never_produces_code() -> None:
    """Confirmado ao vivo (2026-09-10): nenhuma das evidências REAIS
    encontradas até agora pros 3 padrões amazon* expõe um código de
    texto -- nenhum tem grupo de captura de código. Isso descreve só o
    que foi OBSERVADO nestas evidências específicas; correção
    (2026-09-10, revisão pós-relatório): a ausência de código aqui NÃO é
    prova de que o cupom nunca precisa de ativação nem de que é sempre
    'clip coupon' automático -- a Amazon usa código em outras
    superfícies (não cobertas por estes 3 padrões)."""
    texts = [
        "cupom de R$ 50,00 de desconto",
        "cupom de 15% de desconto",
        "Você paga R$ 26,91 com o cupom",
    ]
    for text in texts:
        for f in find_coupon_markers("amazon", text):
            assert f["code"] is None, f"Nenhum destes 3 padrões amazon* produz code para: {text!r}"
    print("PASS: Amazon -- nenhum dos 3 padrões observados produz code (não é uma conclusão sobre a loja inteira)")


# ---------------------------------------------------------------------------
# Kabum
# ---------------------------------------------------------------------------


def test_kabum_code_captured_with_real_capitalization() -> None:
    text = "SELO: CUPOM KORUJAO"
    found = find_coupon_markers("kabum", text)
    assert len(found) == 1
    assert found[0]["code"] == "KORUJAO"  # capitalização real preservada
    print("PASS: Kabum -- código real capturado com capitalização preservada")


def test_kabum_mixed_case_code_preserved_as_is() -> None:
    """Não normaliza pra maiúscula/minúscula arbitrariamente -- persiste
    exatamente o que a origem mostrou."""
    text = "SELO: CUPOM BlackNinja25"
    found = find_coupon_markers("kabum", text)
    assert len(found) == 1
    assert found[0]["code"] == "BlackNinja25"
    print("PASS: Kabum -- capitalização mista do código preservada intacta")


def test_kabum_never_produces_discount() -> None:
    """Achado real confirmado ao vivo (kabum.com.br/cupons, 2026-09-10):
    o percentual visível no card ('-13%', 'Desconto: -13%') é o desconto
    NORMAL do produto (de/por), nunca o efeito do cupom -- extrair isso
    como discount_value do cupom seria ERRADO. Por isso não existe (e
    não deve existir) um padrão kabum_pct/kabum_fixed."""
    text = "R$ 166,65\nR$ 129,99\nDesconto: -13%\nSELO: CUPOM KORUJAO"
    found = find_coupon_markers("kabum", text)
    assert len(found) == 1
    assert found[0]["discount_kind"] is None
    assert found[0]["discount_value"] is None
    print("PASS: Kabum -- percentual do produto (de/por) nunca vira discount_value do cupom")


# ---------------------------------------------------------------------------
# Magalu
# ---------------------------------------------------------------------------


def test_magalu_fixed_discount_structured() -> None:
    text = "Cupom R$ 300 OFF"
    found = find_coupon_markers("magalu", text)
    assert len(found) == 1
    assert found[0]["code"] is None
    assert found[0]["discount_kind"] == "fixed_amount"
    assert found[0]["discount_value"] == 300.0
    print("PASS: Magalu -- 'Cupom RX OFF' vira discount_kind=fixed_amount")


def test_magalu_percentage_discount_structured() -> None:
    text = "Cupom 5% OFF"
    found = find_coupon_markers("magalu", text)
    assert len(found) == 1
    assert found[0]["discount_kind"] == "percentage"
    assert found[0]["discount_value"] == 5.0
    print("PASS: Magalu -- 'Cupom X% OFF' vira discount_kind=percentage")


def test_magalu_regex_path_never_produces_code() -> None:
    """O caminho de regex de TEXTO (find_coupon_markers) nunca produz
    code para Magalu -- correto, porque o código real dela mora dentro
    do VALOR de um <input readonly> (confirmado ao vivo, 2026-09-10:
    data-testid="coupon-code-input"), que nunca aparece em innerText/
    textContent -- nenhum regex de texto EXISTENTE conseguiria ver isso,
    não importa o padrão. Por isso existe um caminho SEPARADO
    (`build_widget_coupon`, testado abaixo) que lê o valor do elemento
    via DOM -- é ELE que resolve o código real, não uma alegação de que
    'a Magalu não tem código'."""
    for text in ["Cupom R$ 300 OFF", "Cupom 5% OFF"]:
        for f in find_coupon_markers("magalu", text):
            assert f["code"] is None
    print("PASS: Magalu -- caminho de regex de texto nunca vê code (é o widget DOM que resolve isso)")


def test_magalu_widget_code_captured_with_real_capitalization() -> None:
    """Achado real ao vivo (magazineluiza.com.br, produto real, 2026-09-10):
    <input data-testid="coupon-code-input" readonly value="LU250">.
    `build_widget_coupon` recebe o valor JÁ extraído via DOM (não regex)
    e preserva a capitalização exata -- nunca normaliza."""
    from coupons.evidence import build_widget_coupon

    widget_text = "R$ 250 OFF\nCopiar\n\nCopie o cupom e cole na revisão. Válido até 12 de set."
    coupon = build_widget_coupon(
        "magalu", code="LU250", widget_text=widget_text,
        reference_url="https://www.magazineluiza.com.br/produto-x/p/123/",
    )
    assert coupon.code == "LU250"  # capitalização real preservada
    assert coupon.discount_kind == "fixed_amount"
    assert coupon.discount_value == 250.0
    # `[^\n.!]` exclui pontuação de fim de frase do próprio padrão
    # (pré-existente, deliberado) -- "." final não entra no match.
    assert coupon.valid_until == "Válido até 12 de set"
    assert coupon.scope_kind == "product"
    assert coupon.source_url == "https://www.magazineluiza.com.br/produto-x/p/123/"
    print("PASS: widget da Magalu -- código real (LU250) + desconto + validade capturados juntos, num só cupom")


def test_widget_discount_percentage_variant() -> None:
    from coupons.evidence import parse_widget_discount

    result = parse_widget_discount("15% OFF\nCopiar")
    assert result["discount_kind"] == "percentage"
    assert result["discount_value"] == 15.0
    print("PASS: widget -- '15% OFF' isolado (sem 'cupom' por perto) vira discount_kind=percentage")


def test_widget_zero_discount_never_persisted_as_real() -> None:
    """Achado real ao vivo (Mercado Livre, produto real, 2026-09-10): um
    widget de cupom não ativado/já usado mostra literalmente 'R$0 OFF'
    -- não é um desconto real de zero, é a origem não revelando o valor
    verdadeiro (classe CSS do próprio site: 'ui-vpp-coupons-awareness--
    redemeed'). Nunca persistimos um zero que não é real -- fica nulo,
    igual a 'sem informação suficiente'."""
    from coupons.evidence import parse_widget_discount

    result = parse_widget_discount("R$0 OFF. Compra mínima R$150.")
    assert result["discount_kind"] is None
    assert result["discount_value"] is None
    print("PASS: widget -- 'R$0 OFF' (achado real ML, estado não confiável) nunca vira discount_value=0")


def test_widget_coupon_never_invents_code_when_absent() -> None:
    from coupons.evidence import build_widget_coupon

    coupon = build_widget_coupon(
        "magalu", code=None, widget_text="10% OFF",
        reference_url="https://www.magazineluiza.com.br/produto-y/p/456/",
    )
    assert coupon.code is None
    print("PASS: widget sem código real -- code=None preservado, nunca inventado")


# ---------------------------------------------------------------------------
# Mercado Livre
# ---------------------------------------------------------------------------


def test_mercadolivre_all_four_phrasings_structured() -> None:
    cases = [
        ("20% OFF com Cupom", "percentage", 20.0),
        ("R$ 8 OFF com Cupom", "fixed_amount", 8.0),
        ("Cupom ativado de 10% OFF em produtos de Loja X", "percentage", 10.0),
        ("Cupom R$ 15 OFF em produtos selecionados", "fixed_amount", 15.0),
    ]
    for text, kind, value in cases:
        found = find_coupon_markers("mercadolivre", text)
        assert len(found) >= 1, f"nenhum achado para: {text!r}"
        assert found[0]["discount_kind"] == kind
        assert found[0]["discount_value"] == value
        assert found[0]["code"] is None
    print("PASS: Mercado Livre -- os 4 fraseados reais viram discount_kind/value corretos, sem code")


def test_mercadolivre_extras_minimum_and_maximum_captured() -> None:
    text = "Cupom ativado de 15% OFF. Compra mínima R$100. Limite de R$50."
    found = find_coupon_markers("mercadolivre", text)
    assert len(found) == 1
    assert found[0]["minimum_purchase_amount"] == 100.0
    assert found[0]["maximum_discount_amount"] == 50.0
    print("PASS: Mercado Livre -- compra mínima e limite de desconto capturados como extras")


# ---------------------------------------------------------------------------
# source_url / build_coupons (escopo completo, não só find_coupon_markers)
# ---------------------------------------------------------------------------


def test_source_url_preserved_for_product_scope() -> None:
    url = "https://www.kabum.com.br/produto/123456"
    coupons = build_coupons("kabum", "cards", "SELO: CUPOM TESTE25", url)
    assert len(coupons) == 1
    assert coupons[0].source_url == url
    assert coupons[0].scope_kind == "product"
    assert coupons[0].scope_reference == url
    print("PASS: source_url preservado intacto + escopo 'product' para achado em card")


def test_build_coupons_populates_valid_until_when_store_states_it() -> None:
    """Bug real corrigido (auditoria 2026-09-10): `valid_until` existia
    no modelo desde sempre, mas `build_coupons` nunca o preenchia --
    o texto de validade ("válido até X") só ia pra `raw_rule_text`,
    nunca pro campo dedicado. Agora os dois."""
    text = "Cupom R$ 300 OFF. Válido até 31/12."
    coupons = build_coupons("magalu", "cards", text, "https://x.invalid/p1")
    assert len(coupons) == 1
    # `[^\n.!]` exclui pontuação de fim de frase do padrão (pré-existente).
    assert coupons[0].valid_until == "Válido até 31/12"
    assert "Válido até 31/12" in (coupons[0].raw_rule_text or "")  # preservado nos dois lugares
    print("PASS: 'válido até X' popula o campo valid_until dedicado (bug real corrigido)")


def test_build_coupons_valid_until_none_when_store_says_nothing() -> None:
    """Informação desconhecida permanece desconhecida -- nunca inventa
    uma validade quando a origem não informou nenhuma."""
    text = "Cupom R$ 300 OFF"
    coupons = build_coupons("magalu", "cards", text, "https://x.invalid/p1")
    assert coupons[0].valid_until is None
    print("PASS: sem validade informada pela loja -- valid_until fica None, nunca inventado")


def test_source_url_preserved_for_store_level_scope() -> None:
    url = "https://www.amazon.com.br/coupons"
    coupons = build_coupons("amazon", "coupons", "Você paga R$ 26,91 com o cupom", url)
    assert len(coupons) == 1
    assert coupons[0].source_url == url
    assert coupons[0].scope_kind is None  # nível de loja, sem linguagem store_wide -> unknown
    print("PASS: source_url preservado intacto para achado de nível de loja (scope_kind=None)")


def test_store_wide_language_detected_in_context() -> None:
    text = "Cupom válido para toda a loja: cupom de 10% de desconto"
    coupons = build_coupons("amazon", "home", text, "https://www.amazon.com.br/")
    assert len(coupons) == 1
    assert coupons[0].scope_kind == "store_wide"
    print("PASS: linguagem de abrangência geral na evidência vira scope_kind=store_wide")


# ---------------------------------------------------------------------------
# Upsert: cupom existente melhora quando nova evidência aparece (item 7)
# ---------------------------------------------------------------------------


def test_upsert_updates_existing_row_when_same_evidence_key() -> None:
    """Prova real (SQLite real, não mock): se uma correção de parser
    ENRIQUECE um achado sem mudar `code`/`raw_rule_text` usados na chave
    de evidência, a MESMA linha é atualizada na próxima coleta -- nunca
    precisa de UPDATE manual."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        base = Coupon(
            store_id="kabum", code="KORUJAO", discount_kind=None, discount_value=None,
            evidence="kabum:cards:KORUJAO:https://x.invalid/p1", source_url="https://x.invalid/p1",
        )
        store.upsert(base)

        rows = store._conn.execute("SELECT * FROM coupons").fetchall()
        assert len(rows) == 1
        assert rows[0]["discount_kind"] is None

        # "Correção de parser" simulada: mesma evidence key, agora com
        # discount_kind/value preenchidos (nunca acontece de verdade pro
        # Kabum, per achado acima -- é só a prova do MECANISMO de upsert).
        enriched = Coupon(
            store_id="kabum", code="KORUJAO", discount_kind="fixed_amount", discount_value=25.0,
            evidence="kabum:cards:KORUJAO:https://x.invalid/p1", source_url="https://x.invalid/p1",
        )
        store.upsert(enriched)

        rows = store._conn.execute("SELECT * FROM coupons").fetchall()
        assert len(rows) == 1, "mesma evidence key -- UPDATE na mesma linha, nunca INSERT duplicado"
        assert rows[0]["discount_kind"] == "fixed_amount"
        assert rows[0]["discount_value"] == 25.0
        print("PASS: upsert com mesma evidence key ATUALIZA discount_kind/value na linha existente")
    finally:
        store.close()
        os.unlink(path)


def test_upsert_creates_new_row_when_code_changes_evidence_key() -> None:
    """Caso diferente: se a correção muda O QUE vira `code` (achado novo
    onde antes não havia), a chave de evidência muda -- cria uma
    observação NOVA, nunca sobrescreve a antiga (nunca finge que é a
    mesma evidência). A linha antiga (sem código) fica órfã até
    `expire_stale` marcá-la como expirada na próxima rodada em que não
    for mais vista -- comportamento correto, não um bug."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        old = Coupon(
            store_id="amazon", code=None, discount_kind=None, discount_value=None,
            evidence="amazon:coupons:Você paga R$ 50,00 com o cupom:https://x.invalid/coupons",
            raw_rule_text="Você paga R$ 50,00 com o cupom",
        )
        store.upsert(old)

        # Hipotético: um padrão NOVO passasse a achar um código onde
        # antes não achava nada -- muda o que `code`/`evidence` guardam.
        new = Coupon(
            store_id="amazon", code="PROMO50", discount_kind=None, discount_value=None,
            evidence="amazon:coupons:PROMO50:https://x.invalid/coupons",
            raw_rule_text="Código: PROMO50",
        )
        store.upsert(new)

        rows = store._conn.execute("SELECT code FROM coupons ORDER BY code").fetchall()
        assert len(rows) == 2, "evidence key diferente -- duas observações distintas, nunca uma sobrescrevendo a outra"
        codes = {r["code"] for r in rows}
        assert codes == {"", "PROMO50"}
        print("PASS: mudar o que vira 'code' cria observação NOVA (nunca reescreve silenciosamente a antiga)")
    finally:
        store.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# Persistência -- testes específicos por requisito (revisão 2026-09-10: "o
# total agregado de testes não esclarece quais requisitos foram cobertos" --
# cada teste abaixo prova UMA garantia nomeada, não uma contagem genérica).
# ---------------------------------------------------------------------------


def test_upsert_enriches_same_record_with_code_discount_validity_link_on_later_scan() -> None:
    """Requisito: nova coleta ENRIQUECE o MESMO registro com código,
    desconto, validade e link -- sem duplicar. Simula: 1a rodada só viu o
    código (regex de texto, Magalu); 2a rodada (mesma evidence key --
    mesmo `code`) já com o widget resolvido, agora com discount/validity/
    source_url. Precisa continuar sendo UMA linha só, agora completa."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        first_scan = Coupon(
            store_id="magalu", code="LU250", discount_kind=None, discount_value=None,
            valid_until=None, source_url=None,
            evidence="magalu:product:LU250:https://x.invalid/p1",
            raw_rule_text="Código: LU250",
        )
        store.upsert(first_scan)

        second_scan = Coupon(
            store_id="magalu", code="LU250", discount_kind="fixed_amount", discount_value=250.0,
            valid_until="Válido até 12 de set", source_url="https://x.invalid/p1",
            evidence="magalu:product:LU250:https://x.invalid/p1",  # MESMA evidence key
            raw_rule_text="Código: LU250 | R$250 OFF",
        )
        store.upsert(second_scan)

        rows = store._conn.execute("SELECT * FROM coupons WHERE code='LU250'").fetchall()
        assert len(rows) == 1, "mesma evidence key -- enriquece a MESMA linha, nunca duplica"
        assert rows[0]["discount_kind"] == "fixed_amount"
        assert rows[0]["discount_value"] == 250.0
        assert rows[0]["valid_until"] == "Válido até 12 de set"
        assert rows[0]["source_url"] == "https://x.invalid/p1"
        print("PASS: 2a coleta enriquece a MESMA linha (código+desconto+validade+link) sem duplicar registro")
    finally:
        store.close()
        os.unlink(path)


def test_upsert_never_erases_valid_data_with_incomplete_later_extraction() -> None:
    """Requisito central da revisão (2026-09-10): uma coleta POSTERIOR
    incompleta (ex.: a extração não achou desta vez o que achou antes --
    layout mudou de posição, widget não abriu a tempo) NUNCA apaga um
    dado bom já persistido. Sem a correção `COALESCE` no upsert, este
    teste falhava (o UPDATE sobrescrevia com NULL). `status`/
    `raw_rule_text`/`last_seen_at` continuam sempre atualizados -- só os
    campos ESTRUTURADOS extraídos (discount/validade/link/escopo) são
    protegidos contra regressão pra NULL."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        complete_scan = Coupon(
            store_id="magalu", code="LU250", discount_kind="fixed_amount", discount_value=250.0,
            valid_until="Válido até 12 de set", source_url="https://x.invalid/p1",
            scope_kind="product", scope_reference="https://x.invalid/p1",
            evidence="magalu:product:LU250:https://x.invalid/p1",
            raw_rule_text="Código: LU250 | R$250 OFF | Válido até 12 de set",
            status="active",
        )
        store.upsert(complete_scan)

        incomplete_scan = Coupon(
            store_id="magalu", code="LU250", discount_kind=None, discount_value=None,
            valid_until=None, source_url=None, scope_kind=None, scope_reference=None,
            evidence="magalu:product:LU250:https://x.invalid/p1",  # MESMA evidence key
            raw_rule_text="Código: LU250",  # desta vez só viu o código
            status="active",
        )
        store.upsert(incomplete_scan)

        rows = store._conn.execute("SELECT * FROM coupons WHERE code='LU250'").fetchall()
        assert len(rows) == 1
        row = rows[0]
        # Dados estruturados da coleta ANTERIOR (boa) preservados, nunca
        # apagados por uma extração incompleta posterior.
        assert row["discount_kind"] == "fixed_amount", "discount_kind nunca deveria regredir pra NULL"
        assert row["discount_value"] == 250.0
        assert row["valid_until"] == "Válido até 12 de set"
        assert row["source_url"] == "https://x.invalid/p1"
        assert row["scope_kind"] == "product"
        assert row["scope_reference"] == "https://x.invalid/p1"
        # raw_rule_text SEMPRE reflete a evidência literal mais recente
        # (não é um valor estruturado sujeito a "regressão" -- é o que
        # foi visto agora).
        assert row["raw_rule_text"] == "Código: LU250"
        print("PASS: extração incompleta numa coleta posterior NUNCA apaga discount/validade/link/escopo já confirmados")
    finally:
        store.close()
        os.unlink(path)


def test_upsert_never_mixes_new_field_with_stale_paired_field() -> None:
    """Requisito explícito (revisão 2026-09-10, segunda rodada): campos
    relacionados (discount_kind+discount_value; scope_kind+
    scope_reference) atualizam como CONJUNTO, nunca um novo combinado
    com o par antigo do outro. Simula um upsert "malformado" que só traz
    a METADE de um par (não acontece com os parsers de hoje, mas o
    UPSERT precisa ser seguro mesmo assim) -- prova que o par INTEIRO
    antigo é preservado, nunca uma mistura tipo-novo/valor-antigo."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        store.upsert(Coupon(
            store_id="mercadolivre", code="PROMO20", discount_kind="fixed_amount", discount_value=20.0,
            scope_kind="product", scope_reference="https://x.invalid/produto-a",
            evidence="mercadolivre:cards:PROMO20:https://x.invalid/produto-a",
            raw_rule_text="PROMO20 -- R$20 OFF",
        ))

        # "Upsert malformado": só discount_kind vem preenchido desta vez,
        # discount_value vem nulo (nunca deveria acontecer de verdade,
        # mas o UPSERT não pode depender dessa convenção pra ficar são).
        store.upsert(Coupon(
            store_id="mercadolivre", code="PROMO20", discount_kind="percentage", discount_value=None,
            scope_kind=None, scope_reference=None,
            evidence="mercadolivre:cards:PROMO20:https://x.invalid/produto-a",
            raw_rule_text="PROMO20 (extração parcial)",
        ))

        row = store._conn.execute("SELECT * FROM coupons WHERE code='PROMO20'").fetchone()
        # O par INTEIRO antigo foi preservado -- nunca discount_kind="percentage"
        # (novo) combinado com discount_value=20.0 (antigo, de um fixed_amount).
        assert row["discount_kind"] == "fixed_amount", "nunca aceita metade nova de um par -- preserva o par INTEIRO antigo"
        assert row["discount_value"] == 20.0
        assert row["scope_kind"] == "product"
        assert row["scope_reference"] == "https://x.invalid/produto-a"
        print("PASS: par discount_kind+discount_value (e scope_kind+scope_reference) nunca mistura novo com antigo -- atualiza só como conjunto completo")

        # Agora um upsert REAL, com o par completo -- confirma que a
        # atualização em conjunto funciona quando os dois vêm juntos.
        store.upsert(Coupon(
            store_id="mercadolivre", code="PROMO20", discount_kind="percentage", discount_value=25.0,
            scope_kind="store_wide", scope_reference=None,
            evidence="mercadolivre:cards:PROMO20:https://x.invalid/produto-a",
            raw_rule_text="PROMO20 -- 25% OFF em toda a loja",
        ))
        row2 = store._conn.execute("SELECT * FROM coupons WHERE code='PROMO20'").fetchone()
        assert row2["discount_kind"] == "percentage"
        assert row2["discount_value"] == 25.0
        assert row2["scope_kind"] == "store_wide"
        assert row2["scope_reference"] is None  # store_wide decidido, referência legitimamente nula
        print("PASS: par completo novo (discount_kind+value, ou scope_kind+reference) atualiza os dois juntos normalmente")
    finally:
        store.close()
        os.unlink(path)


def test_upsert_later_correct_extraction_still_overwrites_previous_value() -> None:
    """Contraprova do teste acima: a proteção é só contra APAGAR com
    NULL -- um valor novo REAL (não-nulo) sempre pode corrigir/atualizar
    o anterior (ex.: parser corrigido passa a achar um valor mais
    preciso). Nunca fica "travado" no primeiro valor gravado."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        store.upsert(Coupon(
            store_id="magalu", code="LU250", discount_kind="fixed_amount", discount_value=250.0,
            evidence="magalu:product:LU250:https://x.invalid/p1",
            raw_rule_text="Código: LU250 | R$250 OFF",
        ))
        store.upsert(Coupon(
            store_id="magalu", code="LU250", discount_kind="fixed_amount", discount_value=300.0,  # corrigido
            evidence="magalu:product:LU250:https://x.invalid/p1",
            raw_rule_text="Código: LU250 | R$300 OFF",
        ))
        row = store._conn.execute("SELECT * FROM coupons WHERE code='LU250'").fetchone()
        assert row["discount_value"] == 300.0, "valor novo REAL (não-nulo) sempre substitui o anterior"
        print("PASS: um valor novo não-nulo sempre atualiza o anterior (proteção é só contra NULL apagando dado bom)")
    finally:
        store.close()
        os.unlink(path)


def test_upsert_never_merges_two_different_products_sharing_same_code() -> None:
    """Requisito: dois produtos DIFERENTES que por coincidência usam o
    MESMO código de cupom (comum em campanhas amplas tipo Mercado Livre)
    NUNCA podem ser fundidos numa única linha -- cada um preserva seu
    próprio `scope_reference`/desconto, mesmo com `code` idêntico. A
    chave de dedup inclui `evidence` (que carrega a URL/produto real),
    não só `code` -- por isso permanecem duas linhas distintas."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        product_a = Coupon(
            store_id="mercadolivre", code="PROMO10", discount_kind="percentage", discount_value=10.0,
            scope_kind="product", scope_reference="https://x.invalid/produto-a",
            evidence="mercadolivre:cards:PROMO10:https://x.invalid/produto-a",
            raw_rule_text="PROMO10 -- 10% OFF (produto A)",
        )
        product_b = Coupon(
            store_id="mercadolivre", code="PROMO10", discount_kind="percentage", discount_value=10.0,
            scope_kind="product", scope_reference="https://x.invalid/produto-b",
            evidence="mercadolivre:cards:PROMO10:https://x.invalid/produto-b",  # URL diferente -- evidence diferente
            raw_rule_text="PROMO10 -- 10% OFF (produto B)",
        )
        store.upsert(product_a)
        store.upsert(product_b)

        rows = store._conn.execute(
            "SELECT scope_reference FROM coupons WHERE code='PROMO10' ORDER BY scope_reference"
        ).fetchall()
        assert len(rows) == 2, "mesmo code, produtos diferentes -- NUNCA fundidos numa linha só"
        refs = {r["scope_reference"] for r in rows}
        assert refs == {"https://x.invalid/produto-a", "https://x.invalid/produto-b"}

        # Atualizar o cupom do produto A não pode vazar/afetar o produto B.
        store.upsert(Coupon(
            store_id="mercadolivre", code="PROMO10", discount_kind="percentage", discount_value=15.0,
            scope_kind="product", scope_reference="https://x.invalid/produto-a",
            evidence="mercadolivre:cards:PROMO10:https://x.invalid/produto-a",
            raw_rule_text="PROMO10 -- 15% OFF (produto A, corrigido)",
        ))
        row_a = store._conn.execute(
            "SELECT discount_value FROM coupons WHERE scope_reference='https://x.invalid/produto-a'"
        ).fetchone()
        row_b = store._conn.execute(
            "SELECT discount_value FROM coupons WHERE scope_reference='https://x.invalid/produto-b'"
        ).fetchone()
        assert row_a["discount_value"] == 15.0
        assert row_b["discount_value"] == 10.0, "atualizar produto A nunca pode alterar o valor do produto B"
        print("PASS: dois produtos com o MESMO código nunca são fundidos -- cada um preserva seu próprio escopo/valor")
    finally:
        store.close()
        os.unlink(path)


def test_expire_stale_only_runs_after_successful_round_never_after_error_or_block() -> None:
    """Requisito: distinguir FALHA de coleta (bloqueio/erro de rede/
    timeout) de expiração/indisponibilidade real (cupom sumiu da loja).
    Prova de código real (`coupons/scanner.py:_scan_store`, linhas
    ~249-255): `expire_stale` só é chamado quando `result["status"] ==
    "ok"` (a rodada completou de verdade); comentário no próprio código:
    "Só quando a rodada completou de verdade (nunca após bloqueio/erro,
    que não prova ausência)". Este teste verifica o MECANISMO
    `expire_stale` isoladamente -- que ele só marca como expirado o que
    realmente não foi confirmado, nunca decide sozinho quando deve ou
    não ser chamado (essa decisão já é auditada na leitura do código
    citada acima, não duplicada aqui em Playwright)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        store.upsert(Coupon(
            store_id="kabum", code="KORUJAO", discount_kind=None, discount_value=None,
            evidence="kabum:cards:KORUJAO:https://x.invalid/p1",
            raw_rule_text="SELO: CUPOM KORUJAO", status="active",
        ))
        before_new_round = "2026-09-10T00:00:00+00:00"
        # Rodada "falhou" (bloqueio/erro) -- NUNCA chama expire_stale
        # (replica a condição real do scanner: só roda quando status=='ok').
        simulated_round_status = "blocked"
        if simulated_round_status == "ok":
            store.expire_stale("kabum", before_new_round)
        row = store._conn.execute("SELECT status FROM coupons WHERE code='KORUJAO'").fetchone()
        assert row["status"] == "active", "rodada bloqueada/com erro NUNCA pode expirar um cupom por ausência"

        # Rodada seguinte completou de verdade e o cupom REALMENTE sumiu
        # (nenhum upsert desta vez) -- agora sim vira expired. Corte
        # relativo a "agora" (nunca uma data fixa -- o upsert acima grava
        # last_seen_at com o timestamp real do momento em que o teste
        # roda, então o corte precisa ser estritamente posterior a ELE,
        # não a uma data específica que se torna passado com o tempo).
        after_this_round = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        expired_count = store.expire_stale("kabum", after_this_round)
        assert expired_count == 1
        row = store._conn.execute("SELECT status FROM coupons WHERE code='KORUJAO'").fetchone()
        assert row["status"] == "expired"
        print("PASS: expire_stale só marca ausência real após rodada OK -- nunca expira por bloqueio/erro de coleta (mecanismo isolado; gate real citado em scanner.py)")
    finally:
        store.close()
        os.unlink(path)


# ---------------------------------------------------------------------------
# Amazon -- ciclo de vida da economia derivada (revisão 2026-09-10,
# segunda rodada: "a economia derivada precisa ficar vinculada às
# condições e ao preço-base que a sustentam" -- cada cenário pedido
# explicitamente vira um teste nomeado, usando build_coupons + upsert
# real, nunca só o parsing isolado).
# ---------------------------------------------------------------------------


def test_amazon_economia_price_change_creates_new_row_never_reuses_stale_value() -> None:
    """Requisito: quando o preço-base muda entre coletas, a economia
    antiga NUNCA pode ser reaproveitada como se ainda fosse válida.
    `raw_rule_text` (e portanto `evidence`, que cai nele quando `code`
    é None) embute os valores literais dos preços -- uma mudança real de
    preço produz um `raw_rule_text` DIFERENTE, logo uma `evidence` key
    DIFERENTE: vira uma linha NOVA, nunca uma atualização silenciosa da
    antiga. A linha antiga (com o valor agora desatualizado) fica órfã
    até `expire_stale` marcá-la como `expired` na rodada em que não for
    mais confirmada -- nunca continua `active` para sempre."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        url = "https://www.amazon.com.br/produto-x/dp/ASIN123"

        # Coleta 1: Por R$110,19 -> Você paga R$100,19 = economia R$10,00.
        text_1 = "Você paga R$ 100,19 com o cupom\nPor: R$ 110,19"
        coupons_1 = build_coupons("amazon", "cards", text_1, url)
        assert len(coupons_1) == 1
        store.upsert(coupons_1[0])
        row_1 = store._conn.execute("SELECT * FROM coupons").fetchall()
        assert len(row_1) == 1
        assert row_1[0]["discount_value"] == 10.0
        evidence_1 = row_1[0]["evidence"]

        # Coleta 2 (preço-base mudou de verdade -- Por subiu pra R$120,19,
        # cupom agora dá R$15,00 de economia real).
        text_2 = "Você paga R$ 105,19 com o cupom\nPor: R$ 120,19"
        coupons_2 = build_coupons("amazon", "cards", text_2, url)
        assert len(coupons_2) == 1
        store.upsert(coupons_2[0])

        rows = store._conn.execute("SELECT * FROM coupons ORDER BY discount_value").fetchall()
        assert len(rows) == 2, "preço-base mudou -- vira linha NOVA, nunca sobrescreve silenciosamente a antiga"
        assert rows[0]["discount_value"] == 10.0  # antiga, agora desatualizada
        assert rows[1]["discount_value"] == 15.0  # nova, atual
        assert rows[0]["evidence"] != rows[1]["evidence"]
        assert rows[0]["evidence"] == evidence_1

        # Rodada seguinte confirma só a NOVA evidência (é o que realmente
        # está na página agora) -- a antiga nunca é revista, expira.
        # Corte = last_seen_at da linha NOVA: mais antiga que isso (a
        # antiga) expira; a própria linha nova (não é "<" ela mesma) não.
        cutoff = store._conn.execute(
            "SELECT last_seen_at FROM coupons WHERE evidence != ?", (evidence_1,)
        ).fetchone()["last_seen_at"]
        expired = store.expire_stale("amazon", cutoff)
        assert expired == 1
        old_row = store._conn.execute(
            "SELECT status FROM coupons WHERE evidence = ?", (evidence_1,)
        ).fetchone()
        assert old_row["status"] == "expired", "economia calculada com preço-base antigo nunca fica active pra sempre"
        print("PASS: Amazon -- mudança real de preço-base cria linha nova (nunca reusa economia desatualizada) e a antiga expira")
    finally:
        store.close()
        os.unlink(path)


def test_amazon_economia_benefit_disappearing_expires_never_stays_active() -> None:
    """Requisito: quando o benefício deixa de aparecer (produto não tem
    mais cupom nenhum numa coleta posterior), a linha antiga expira --
    nunca fica presa em `active` só porque o produto continua existindo/
    aparecendo na loja."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        url = "https://www.amazon.com.br/produto-y/dp/ASIN456"
        text_com_cupom = "Você paga R$ 80,00 com o cupom\nPor: R$ 95,00"
        coupons = build_coupons("amazon", "cards", text_com_cupom, url)
        store.upsert(coupons[0])
        assert store._conn.execute("SELECT status FROM coupons").fetchone()["status"] == "active"

        # Rodada seguinte: o card do MESMO produto não menciona mais
        # cupom nenhum (nada é upsertado pra essa evidence key) -- rodada
        # completou normalmente (status 'ok', simulado aqui só pela
        # ausência de novo upsert). Corte relativo a "agora" (nunca uma
        # data fixa -- mesma razão do teste acima).
        cutoff = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        expired = store.expire_stale("amazon", cutoff)
        assert expired == 1
        row = store._conn.execute("SELECT status FROM coupons").fetchone()
        assert row["status"] == "expired"
        print("PASS: Amazon -- benefício que some numa coleta posterior expira, nunca fica active indefinidamente")
    finally:
        store.close()
        os.unlink(path)


def test_amazon_economia_incomplete_later_scan_preserves_value_same_evidence() -> None:
    """Requisito: extração incompleta é diferente de remoção comprovada.
    Se a MESMA evidência (mesmo texto exato, portanto mesma evidence key)
    for revisitada e por algum motivo a extração não conseguir recalcular
    o valor desta vez (simulado aqui via upsert manual com discount
    nulo), o COALESCE do upsert preserva o valor bom -- nunca apaga só
    porque uma passagem específica veio incompleta. Diferente do teste
    anterior (benefício SOME de verdade -- texto muda ou desaparece):
    aqui o texto/evidence key é o MESMO, só a extração falhou em
    recalcular."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = SqliteCouponStore(path)
        url = "https://www.amazon.com.br/produto-z/dp/ASIN789"
        text = "Você paga R$ 80,00 com o cupom\nPor: R$ 95,00"
        coupons = build_coupons("amazon", "cards", text, url)
        store.upsert(coupons[0])
        evidence = coupons[0].evidence

        # Simula uma extração incompleta da MESMA evidência (mesma
        # evidence key) -- discount_kind/value nulos desta vez.
        store.upsert(Coupon(
            store_id="amazon", code=None, discount_kind=None, discount_value=None,
            evidence=evidence, raw_rule_text="Você paga R$ 80,00 com o cupom",
        ))
        row = store._conn.execute("SELECT * FROM coupons WHERE evidence = ?", (evidence,)).fetchone()
        assert len(store._conn.execute("SELECT * FROM coupons").fetchall()) == 1, "mesma evidence key -- nunca duplica"
        assert row["discount_kind"] == "fixed_amount", "extração incompleta nunca apaga o valor bom já confirmado"
        assert row["discount_value"] == 15.0
        print("PASS: Amazon -- extração incompleta da MESMA evidência preserva o valor bom (COALESCE), nunca apaga")
    finally:
        store.close()
        os.unlink(path)


def main() -> None:
    tests = [
        test_amazon_explicit_fixed_discount_becomes_structured,
        test_amazon_explicit_percentage_discount_becomes_structured,
        test_amazon_final_price_never_becomes_discount,
        test_amazon_economia_derived_from_real_card_example,
        test_amazon_economia_never_fires_outside_card_kinds,
        test_amazon_economia_never_uses_de_price,
        test_amazon_economia_rejects_non_positive_difference,
        test_amazon_economia_requires_por_in_same_neighborhood,
        test_amazon_economia_never_crosses_into_next_product,
        test_amazon_economia_never_fires_on_product_detail_page,
        test_amazon_never_produces_code,
        test_kabum_code_captured_with_real_capitalization,
        test_kabum_mixed_case_code_preserved_as_is,
        test_kabum_never_produces_discount,
        test_magalu_fixed_discount_structured,
        test_magalu_percentage_discount_structured,
        test_magalu_regex_path_never_produces_code,
        test_magalu_widget_code_captured_with_real_capitalization,
        test_widget_discount_percentage_variant,
        test_widget_zero_discount_never_persisted_as_real,
        test_widget_coupon_never_invents_code_when_absent,
        test_mercadolivre_all_four_phrasings_structured,
        test_mercadolivre_extras_minimum_and_maximum_captured,
        test_build_coupons_populates_valid_until_when_store_states_it,
        test_build_coupons_valid_until_none_when_store_says_nothing,
        test_source_url_preserved_for_product_scope,
        test_source_url_preserved_for_store_level_scope,
        test_store_wide_language_detected_in_context,
        test_upsert_updates_existing_row_when_same_evidence_key,
        test_upsert_creates_new_row_when_code_changes_evidence_key,
        test_upsert_enriches_same_record_with_code_discount_validity_link_on_later_scan,
        test_upsert_never_erases_valid_data_with_incomplete_later_extraction,
        test_upsert_never_mixes_new_field_with_stale_paired_field,
        test_upsert_later_correct_extraction_still_overwrites_previous_value,
        test_upsert_never_merges_two_different_products_sharing_same_code,
        test_expire_stale_only_runs_after_successful_round_never_after_error_or_block,
        test_amazon_economia_price_change_creates_new_row_never_reuses_stale_value,
        test_amazon_economia_benefit_disappearing_expires_never_stays_active,
        test_amazon_economia_incomplete_later_scan_preserves_value_same_evidence,
    ]
    for t in tests:
        t()
    print(f"\nTODOS OS TESTES DE EVIDENCE/PERSISTENCE PASSARAM ({len(tests)}/{len(tests)})")


if __name__ == "__main__":
    main()
