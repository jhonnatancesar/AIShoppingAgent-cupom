"""Persistência do Coupon Collector.

Define o modelo ``Coupon`` com EXATAMENTE os campos da TASK-106 e a interface
``CouponStore``. A implementação atual é ``SqliteCouponStore`` (SQLite local
através do stdlib sqlite3) -- leve, confiável e sem dependências externas.

Quando o usuário informar as credenciais, entra a ``PostgresCouponStore``
(mesma interface) apontando para o Postgres do AIShoppingAgent
(``stores.id`` / ``coupons``). Nenhum outro módulo muda: só o factory.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utcnow() -> datetime:
    """Timestamp UTC, timezone-aware (para armazenamento determinístico)."""
    return datetime.now(timezone.utc)


@dataclass
class Coupon:
    """Um cupom com evidência real capturada.

    Campos idênticos aos da TASK-106. Só é instanciado quando existe evidência;
    nunca se fabrica ``code`` / ``discount_*``.
    """
    store_id: str
    code: Optional[str]
    discount_kind: Optional[str]          # "fixed_amount" | "percentage" | None
    discount_value: Optional[float]
    evidence: str
    minimum_purchase_amount: Optional[float] = None
    maximum_discount_amount: Optional[float] = None
    scope_kind: Optional[str] = None       # "product" | "store_wide" | "category" | None (unknown)
    scope_reference: Optional[str] = None
    valid_until: Optional[str] = None
    raw_rule_text: Optional[str] = None
    source_url: Optional[str] = None
    last_seen_at: str = field(default_factory=lambda: _utcnow().isoformat())
    status: str = "active"


@dataclass
class PromoWindow:
    """Janela promocional persistida (módulo de controle)."""
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    updated_at: Optional[str] = None


class CouponStore:
    """Interface de persistência de cupons.

    Implementações: ``SqliteCouponStore`` (agora) e ``PostgresCouponStore``
    (futura, quando houver credenciais).
    """

    def upsert(self, coupon: Coupon) -> None:
        raise NotImplementedError

    def record_candidate(self, store_id: str, url: str, label_text: str) -> bool:
        raise NotImplementedError

    def expire_stale(self, store_id: str, before_iso: str) -> int:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SqliteCouponStore(CouponStore):
    """Persistência local em SQLite (stdlib), com duas tabelas:

    - ``coupons``: observações únicas; ``last_seen_at`` atualizado por upsert.
      Dedup por (store_id, code) -- quando não há código (ex.: Amazon clip),
      usa a chave de evidência (store_id + evidence).
    - ``control``: chave/valor do estado mutável (janela promocional etc.).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS coupons (
        store_id                TEXT NOT NULL,
        code                    TEXT,
        discount_kind           TEXT,
        discount_value          REAL,
        minimum_purchase_amount REAL,
        maximum_discount_amount REAL,
        scope_kind              TEXT,
        scope_reference         TEXT,
        valid_until             TEXT,
        raw_rule_text           TEXT,
        source_url              TEXT,
        evidence                TEXT NOT NULL,
        last_seen_at            TEXT NOT NULL,
        status                  TEXT NOT NULL DEFAULT 'active',
        PRIMARY KEY (store_id, code, evidence)
    );

    CREATE TABLE IF NOT EXISTS control (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS source_candidates (
        store_id      TEXT NOT NULL,
        url           TEXT NOT NULL,
        label_text    TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'new',
        PRIMARY KEY (store_id, url)
    );
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # Garante que o diretório pai exista para que o sqlite3 consiga criar
        # o arquivo (a pasta data/ pode não existir antes do primeiro run).
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def upsert(self, coupon: Coupon) -> None:
        row = asdict(coupon)
        # Para dedup: código nulo (clip Amazon) recai na chave de evidência.
        dedup_code = coupon.code or ""
        self._conn.execute(
            """
            INSERT INTO coupons (
                store_id, code, discount_kind, discount_value,
                minimum_purchase_amount, maximum_discount_amount, scope_kind,
                scope_reference, valid_until, raw_rule_text, source_url,
                evidence, last_seen_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (store_id, code, evidence) DO UPDATE SET
                last_seen_at       = excluded.last_seen_at,
                status             = excluded.status,
                raw_rule_text      = excluded.raw_rule_text,
                source_url         = excluded.source_url,
                valid_until        = excluded.valid_until,
                discount_kind      = excluded.discount_kind,
                discount_value     = excluded.discount_value,
                minimum_purchase_amount = excluded.minimum_purchase_amount,
                maximum_discount_amount = excluded.maximum_discount_amount,
                scope_kind         = excluded.scope_kind,
                scope_reference    = excluded.scope_reference
            """,
            (
                coupon.store_id, dedup_code, coupon.discount_kind,
                coupon.discount_value, coupon.minimum_purchase_amount,
                coupon.maximum_discount_amount, coupon.scope_kind,
                coupon.scope_reference, coupon.valid_until, coupon.raw_rule_text,
                coupon.source_url, coupon.evidence, coupon.last_seen_at,
                coupon.status,
            ),
        )
        self._conn.commit()

    # ---- descoberta de fontes (abas/botoes de cupom novos, ex.: promo) -----

    def record_candidate(self, store_id: str, url: str, label_text: str) -> bool:
        """Registra um link candidato a fonte de cupom (achado por palavra-
        chave, nunca confirmado como cupom real) -- pra você notar quando a
        loja abre uma área nova (ex.: badge "9.9") que ainda não está em
        config.json. Devolve True só na PRIMEIRA vez que essa URL aparece
        (pra logar sem repetir o alarme toda rodada)."""
        now = _utcnow().isoformat()
        cur = self._conn.execute(
            "SELECT 1 FROM source_candidates WHERE store_id = ? AND url = ?",
            (store_id, url),
        )
        is_new = cur.fetchone() is None
        self._conn.execute(
            """
            INSERT INTO source_candidates (store_id, url, label_text, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (store_id, url) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                label_text   = excluded.label_text
            """,
            (store_id, url, label_text, now, now),
        )
        self._conn.commit()
        return is_new

    # ---- expiracao por ausencia (cupom que sumiu de uma rodada pra outra) --

    def expire_stale(self, store_id: str, before_iso: str) -> int:
        """Marca como 'expired' todo cupom `active` dessa loja que não foi
        confirmado (upsert) desde `before_iso` -- ou seja, sumiu da loja
        nesta rodada. Nunca decide isso durante o parsing (evidência
        literal só confirma presença); comparação de histórico real no
        banco, sem IA. Devolve quantos foram marcados."""
        cur = self._conn.execute(
            """
            UPDATE coupons SET status = 'expired'
            WHERE store_id = ? AND status = 'active' AND last_seen_at < ?
            """,
            (store_id, before_iso),
        )
        self._conn.commit()
        return cur.rowcount

    # ---- controle mutável (janela promocional) -----------------------------

    def get_control(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM control WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_control(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO control (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_promo_window(self) -> PromoWindow:
        raw = self.get_control("promo_window")
        if not raw:
            return PromoWindow()
        try:
            data = json.loads(raw)
            return PromoWindow(**data)
        except (json.JSONDecodeError, TypeError):
            return PromoWindow()

    def set_promo_window(self, window: PromoWindow) -> None:
        window.updated_at = _utcnow().isoformat()
        self.set_control("promo_window", json.dumps(asdict(window)))

    def clear_promo_window(self) -> None:
        self.set_control("promo_window", json.dumps(asdict(PromoWindow())))

    def close(self) -> None:
        self._conn.close()


def open_coupon_store(db_path: str) -> CouponStore:
    """Factory: hoje só SQLite; Postgres entra quando houver credenciais."""
    return SqliteCouponStore(db_path)
