#!/usr/bin/env python3
"""Prova da regra de descoberta automática de fontes de cupom
(`Scanner._CANDIDATE_LINK_JS`, `coupons/scanner.py`) -- mesmo padrão de
`test_evidence.py`/`test_cadence.py`, sem pytest, `assert` direto.

Achado real (2026-09-10/11, investigação de lentidão anormal da Amazon):
o padrão numérico `\\d{1,2}\\.\\d{1,2}` (pensado pra campanha tipo "9.9"/
"11.11") também batia em especificação técnica (USB 3.2 Gen 2, Bluetooth
5.3, Wi-Fi 6.0) e avaliação por estrelas ("4.4 de 5 estrelas") -- cada
falso positivo abria uma aba nova pra verificar um link de tracking/
filtro, lento e sempre rejeitado. Numa rodada real, 103 desses falsos
positivos foram adotados ao longo de dias (`source_candidates` real de
PROD, 2026-09-08 em diante) e continuaram sendo revisitados pra sempre,
inflando uma rodada da Amazon de ~60s pra 13m44s -- confirmado ao vivo:
zerar `source_candidates` da Amazon + rodar com a regra corrigida voltou
pra 58s, zero candidatos novos.

Este arquivo espelha em Python, 1:1, a MESMA lógica de
`Scanner._CANDIDATE_LINK_JS` (explicitKeyword / campaignNumber /
techOrRating) -- não é uma reimplementação divergente, é a prova de que
a regra aceita campanha real e rejeita especificação técnica/avaliação,
sem precisar abrir um navegador real pra cada caso de teste."""
from __future__ import annotations

import re

EXPLICIT_KEYWORD = re.compile(r"cupom|liquida|promo[çc][ãa]o", re.I)
CAMPAIGN_NUMBER = re.compile(r"\b\d{1,2}\.\d{1,2}\b")
TECH_OR_RATING = re.compile(
    r"\b(usb|bluetooth|wi-?fi|hdmi|displayport|thunderbolt|pcie|nvme|sata|"
    r"ddr\d?|hz|ghz|mhz|gera[cç][aã]o|vers[aã]o|version|polegadas?|gen)\b"
    r"|de\s+5\s+estrelas|\bestrelas?\b|\bstars?\b",
    re.I,
)


def is_candidate(text: str) -> bool:
    """Espelha a exclusão de `Scanner._CANDIDATE_LINK_JS`: número de
    campanha só conta quando NENHUM termo técnico/avaliação aparece --
    palavra explícita (cupom/liquida/promoção) sempre passa, mesmo
    citando um termo técnico."""
    has_explicit = bool(EXPLICIT_KEYWORD.search(text))
    has_campaign_number = bool(CAMPAIGN_NUMBER.search(text))
    if not has_explicit and not has_campaign_number:
        return False
    if not has_explicit and has_campaign_number and TECH_OR_RATING.search(text):
        return False
    return True


def test_accepts_real_campaign_patterns() -> None:
    """"9.9"/"11.11" (Mercado Livre, "AQUI TEM 9.9") continuam aceitos --
    nenhum termo técnico/avaliação por perto."""
    for text in ("9.9", "AQUI TEM 9.9", "11.11", "Liquida 9.9"):
        assert is_candidate(text), f"deveria aceitar campanha real: {text!r}"
    print("PASS: campanhas reais (9.9/11.11/variantes) continuam aceitas")


def test_accepts_explicit_keyword_even_with_technical_term() -> None:
    """"cupom"/"liquida"/"promoção" explícito sempre vence, mesmo citando
    um termo técnico -- nunca reprime uma evidência real de cupom só
    porque o produto também é, por acaso, um item com specs técnicas."""
    assert is_candidate("Cupom de 15% de desconto")
    assert is_candidate("Cupom Bluetooth 5.3 OFF")
    print("PASS: texto explicitamente promocional continua aceito mesmo citando termo técnico")


def test_rejects_technical_specs_and_ratings() -> None:
    """Achado real (Amazon, 2026-09-10/11): estes textos NUNCA são
    cupom -- são especificação técnica (facet de busca) ou avaliação por
    estrelas (link de tracking/anúncio). Confirmados ao vivo como a
    causa real da lentidão (13m44s -> 58s depois desta regra)."""
    rejected_cases = [
        "USB 3.2 Gen 2",
        "USB 3.2 Gen 1",
        "USB 3.2 Gen 2×2",
        "Bluetooth 5.3",
        "Wi-Fi 6.0",
        "WiFi 6.0",
        "4.4 de 5 estrelas",
        "4.4\n\xa0\n4.4 de 5 estrelas.\n\xa0\n(31)",  # texto real capturado ao vivo
        "HDMI 2.1",
        "DDR5 6.4",
        "Versão 2.1",
    ]
    for text in rejected_cases:
        assert not is_candidate(text), f"deveria rejeitar especificação técnica/avaliação: {text!r}"
    print("PASS: especificação técnica (USB/Bluetooth/Wi-Fi/HDMI/versão) e avaliação por estrelas nunca viram candidato")


def test_neutral_text_without_any_signal_is_not_a_candidate() -> None:
    """Texto qualquer, sem nenhum sinal (nem palavra explícita, nem
    número de campanha) nunca é candidato -- comportamento inalterado."""
    assert not is_candidate("Ver detalhes do produto")
    assert not is_candidate("Adicionar ao carrinho")
    print("PASS: texto neutro (sem cupom/liquida/promoção nem número de campanha) nunca é candidato")


def main() -> None:
    tests = [
        test_accepts_real_campaign_patterns,
        test_accepts_explicit_keyword_even_with_technical_term,
        test_rejects_technical_specs_and_ratings,
        test_neutral_text_without_any_signal_is_not_a_candidate,
    ]
    for t in tests:
        t()
    print(f"\nTODOS OS TESTES DE DESCOBERTA DE CANDIDATOS PASSARAM ({len(tests)}/{len(tests)})")


if __name__ == "__main__":
    main()
