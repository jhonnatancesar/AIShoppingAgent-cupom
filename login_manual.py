"""Abre o Edge dedicado do Coupon Collector -- o MESMO perfil que
coupons/edge_transport.py usa em automação -- pra você logar manualmente
numa loja (ex.: Mercado Livre) com uma conta de pesquisa, nunca a
pessoal.

A sessão (cookies) fica salva nesse perfil em disco e é reaproveitada
automaticamente pelas próximas rodadas do worker/smoke_test -- nenhuma
credencial é digitada por este script, ele só abre a janela numa URL
pública (igual clicar num favorito).

Uso:
  python login_manual.py                                # abre o Mercado Livre
  python login_manual.py https://www.magazineluiza.com.br/  # outra loja
"""
from __future__ import annotations

import subprocess
import sys

from coupons.edge_transport import default_edge_profile_dir, discover_edge_executable

DEFAULT_URL = "https://www.mercadolivre.com.br/"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    executable = discover_edge_executable()
    profile_dir = default_edge_profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"Perfil dedicado: {profile_dir}")
    print(f"Abrindo Edge em {url} -- faça login com uma conta de pesquisa,")
    print("NUNCA sua conta pessoal. Confirme no canto da tela que apareceu")
    print("logado antes de fechar. A sessão fica salva neste perfil.")

    subprocess.Popen([
        str(executable),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
