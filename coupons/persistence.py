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
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("coupons.persistence")


class CouponStoreIntegrationError(RuntimeError):
    """Erro de integração entre o Coupon Worker e o Postgres do GG Oferta
    -- nunca um erro de dado do próprio cupom. Levantado quando algo que
    o worker presume já existir no GG (hoje: a loja) não existe -- nunca
    inventa/cria automaticamente do lado do worker."""


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

    def record_candidate(self, store_id: str, label_text: str, url: str) -> str:
        raise NotImplementedError

    def mark_candidate_status(self, store_id: str, label_text: str, status: str,
                              resolved_url: Optional[str] = None) -> None:
        raise NotImplementedError

    def get_adopted_candidates(self, store_id: str):
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

    -- Chave por label_text (texto do badge/botão, estável), não por url:
    -- achado real (Mercado Livre) -- o mesmo banner "AQUI TEM 9.9" gera um
    -- link de RASTREAMENTO diferente a cada carregamento de página
    -- (click1.mercadolivre.com.br/.../count?a=<token>), o que faria o
    -- mesmo candidato ser "descoberto" de novo a cada rodada se a chave
    -- fosse a url. `url` guarda o destino FINAL já resolvido (depois do
    -- redirecionamento), atualizado quando o candidato é adotado.
    CREATE TABLE IF NOT EXISTS source_candidates (
        store_id      TEXT NOT NULL,
        label_text    TEXT NOT NULL,
        url           TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at  TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'new',
        PRIMARY KEY (store_id, label_text)
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

    def record_candidate(self, store_id: str, label_text: str, url: str) -> str:
        """Registra/atualiza um link candidato a fonte de cupom (achado por
        palavra-chave, nunca confirmado sozinho como cupom real). Chave é
        ``(store_id, label_text)`` -- não a url, que pode ser um link de
        rastreamento com token novo a cada carregamento (achado real,
        Mercado Livre). Devolve o status ATUAL depois da operação:
        ``"new"`` só na primeira vez que esse label aparece (chamador
        decide o que fazer -- ex.: verificar e talvez adotar);
        ``"adopted"``/``"rejected"`` nas vezes seguintes, conforme a
        última verificação real."""
        now = _utcnow().isoformat()
        cur = self._conn.execute(
            "SELECT status FROM source_candidates WHERE store_id = ? AND label_text = ?",
            (store_id, label_text),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                """
                INSERT INTO source_candidates
                    (store_id, label_text, url, first_seen_at, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?, 'new')
                """,
                (store_id, label_text, url, now, now),
            )
            self._conn.commit()
            return "new"
        # Atualiza a url (pode ter mudado -- token de rastreamento novo)
        # só enquanto ainda não foi adotada; depois de adotada, a url
        # guardada é o destino final já resolvido -- não sobrescrever com
        # um link de rastreamento efêmero de uma nova varredura.
        if row["status"] != "adopted":
            self._conn.execute(
                "UPDATE source_candidates SET last_seen_at = ?, url = ? WHERE store_id = ? AND label_text = ?",
                (now, url, store_id, label_text),
            )
        else:
            self._conn.execute(
                "UPDATE source_candidates SET last_seen_at = ? WHERE store_id = ? AND label_text = ?",
                (now, store_id, label_text),
            )
        self._conn.commit()
        return row["status"]

    def mark_candidate_status(self, store_id: str, label_text: str, status: str,
                              resolved_url: Optional[str] = None) -> None:
        """Grava o resultado REAL de uma verificação (adotado com evidência
        confirmada, ou rejeitado por não ter achado nada dessa vez --
        continua elegível pra reverificação em rodadas futuras, a
        promoção pode simplesmente ainda não ter começado). Ao adotar,
        ``resolved_url`` é o destino FINAL já resolvido (depois de
        qualquer redirecionamento) -- substitui o link de rastreamento
        original, que pode não ser mais válido na próxima rodada."""
        if resolved_url:
            self._conn.execute(
                "UPDATE source_candidates SET status = ?, url = ? WHERE store_id = ? AND label_text = ?",
                (status, resolved_url, store_id, label_text),
            )
        else:
            self._conn.execute(
                "UPDATE source_candidates SET status = ? WHERE store_id = ? AND label_text = ?",
                (status, store_id, label_text),
            )
        self._conn.commit()

    def get_adopted_candidates(self, store_id: str):
        """URLs já promovidas a fonte automática (evidência real confirmada
        numa verificação anterior) -- passam a ser escaneadas toda rodada,
        sem precisar de config.json."""
        cur = self._conn.execute(
            "SELECT url, label_text FROM source_candidates WHERE store_id = ? AND status = 'adopted'",
            (store_id,),
        )
        return [(r["url"], r["label_text"] or "") for r in cur.fetchall()]

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


class PostgresCouponStore(CouponStore):
    """Decisão de arquitetura de 2026-09-06: o Coupon Worker roda na
    MESMA máquina do GG Oferta e persiste `coupons` diretamente no MESMO
    PostgreSQL dele -- sem sync de SQLite, sem API intermediária, sem
    segundo banco para integração. Só a tabela `coupons` (dado
    compartilhado real) migra pra cá; `source_candidates`/`control`
    (bookkeeping interno de descoberta, que o GG nunca consome)
    continuam no SQLite local, delegados a uma instância interna de
    `SqliteCouponStore` -- "nenhum outro módulo muda" continua valendo:
    `scanner.py`/`worker.py` só enxergam a interface `CouponStore`.

    `store_id` textual do worker (ex.: `"amazon"`) é resolvido pra FK
    real de `stores.id` a cada upsert (cache em processo). Se o código
    não existir no GG, isso é um ERRO DE INTEGRAÇÃO explícito
    (`CouponStoreIntegrationError`) -- o worker NUNCA cria uma loja nova
    sozinho.
    """

    def __init__(self, postgres_dsn: str, sqlite_db_path: str) -> None:
        import psycopg  # import local: só quando Postgres é configurado

        # Falha EXPLÍCITA e IMEDIATA aqui (na construção, nunca só no
        # primeiro upsert) -- quando o ambiente está configurado para
        # Postgres (`COUPONS_POSTGRES_DSN` presente), o worker NUNCA cai
        # de volta pro SQLite silenciosamente por trás de uma conexão
        # ruim. `SELECT 1` prova que a conexão é usável de verdade, não
        # só que o TCP conectou.
        try:
            self._conn = psycopg.connect(postgres_dsn, autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception as error:
            raise CouponStoreIntegrationError(
                "COUPONS_POSTGRES_DSN está configurada, mas a conexão com "
                f"o Postgres do GG Oferta falhou: {error!r}. O worker "
                "NUNCA cai para SQLite silenciosamente quando o ambiente "
                "está configurado para Postgres -- corrija a credencial/"
                "conexão antes de rodar de novo (nenhum cupom seria "
                "visível ao GG Oferta enquanto isso não for corrigido)."
            ) from error
        self._sqlite = SqliteCouponStore(sqlite_db_path)
        self._store_uuid_by_code: dict[str, str] = {}

    def _resolve_store_uuid(self, store_code: str) -> str:
        cached = self._store_uuid_by_code.get(store_code)
        if cached is not None:
            return cached
        with self._conn.cursor() as cur:
            cur.execute("SELECT id FROM stores WHERE code = %s", (store_code,))
            row = cur.fetchone()
        if row is None:
            raise CouponStoreIntegrationError(
                f"store code desconhecido pelo GG Oferta: {store_code!r} -- "
                "o worker nunca cria uma loja nova; cadastre em `stores` "
                "no GG Oferta antes de habilitar esta loja no worker."
            )
        resolved = str(row[0])
        self._store_uuid_by_code[store_code] = resolved
        return resolved

    def upsert(self, coupon: Coupon) -> None:
        store_uuid = self._resolve_store_uuid(coupon.store_id)
        # Mesmo dedup do SqliteCouponStore: código nulo (clip automático)
        # recai na chave de evidência, nunca `NULL` (`coupons.code` é
        # `NOT NULL DEFAULT ''` no schema do GG).
        dedup_code = coupon.code or ""
        last_seen_at = datetime.fromisoformat(coupon.last_seen_at)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO coupons (
                    id, store_id, code, discount_kind, discount_value,
                    minimum_purchase_amount, maximum_discount_amount,
                    scope_kind, scope_reference, valid_until, raw_rule_text,
                    source_url, evidence, last_seen_at, status
                ) VALUES (
                    gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (store_id, code, evidence) DO UPDATE SET
                    last_seen_at            = EXCLUDED.last_seen_at,
                    status                  = EXCLUDED.status,
                    raw_rule_text           = EXCLUDED.raw_rule_text,
                    source_url              = EXCLUDED.source_url,
                    valid_until             = EXCLUDED.valid_until,
                    discount_kind           = EXCLUDED.discount_kind,
                    discount_value          = EXCLUDED.discount_value,
                    minimum_purchase_amount = EXCLUDED.minimum_purchase_amount,
                    maximum_discount_amount = EXCLUDED.maximum_discount_amount,
                    scope_kind              = EXCLUDED.scope_kind,
                    scope_reference         = EXCLUDED.scope_reference
                """,
                (
                    store_uuid, dedup_code, coupon.discount_kind,
                    coupon.discount_value, coupon.minimum_purchase_amount,
                    coupon.maximum_discount_amount, coupon.scope_kind,
                    coupon.scope_reference, coupon.valid_until,
                    coupon.raw_rule_text, coupon.source_url, coupon.evidence,
                    last_seen_at, coupon.status,
                ),
            )

    def expire_stale(self, store_id: str, before_iso: str) -> int:
        store_uuid = self._resolve_store_uuid(store_id)
        before = datetime.fromisoformat(before_iso)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE coupons SET status = 'expired'
                WHERE store_id = %s AND status = 'active' AND last_seen_at < %s
                """,
                (store_uuid, before),
            )
            return cur.rowcount

    # ---- bookkeeping interno (nunca lido pelo GG) -- delega ao SQLite local

    def record_candidate(self, store_id: str, label_text: str, url: str) -> str:
        return self._sqlite.record_candidate(store_id, label_text, url)

    def mark_candidate_status(self, store_id: str, label_text: str, status: str,
                              resolved_url: Optional[str] = None) -> None:
        self._sqlite.mark_candidate_status(store_id, label_text, status, resolved_url)

    def get_adopted_candidates(self, store_id: str):
        return self._sqlite.get_adopted_candidates(store_id)

    def get_control(self, key: str) -> Optional[str]:
        return self._sqlite.get_control(key)

    def set_control(self, key: str, value: str) -> None:
        self._sqlite.set_control(key, value)

    def get_promo_window(self) -> PromoWindow:
        return self._sqlite.get_promo_window()

    def set_promo_window(self, window: PromoWindow) -> None:
        self._sqlite.set_promo_window(window)

    def clear_promo_window(self) -> None:
        self._sqlite.clear_promo_window()

    def close(self) -> None:
        self._conn.close()
        self._sqlite.close()


def open_coupon_store(
    db_path: str, *, postgres_dsn: Optional[str] = None
) -> CouponStore:
    """Factory: `postgres_dsn` fornecida -> `PostgresCouponStore` (cupons
    no Postgres do GG Oferta, bookkeeping interno no SQLite local
    apontado por `db_path`); ausente -> `SqliteCouponStore` (tudo local --
    modo intencional pra uso/teste local, nunca um "fallback" de um modo
    Postgres mal configurado: se `postgres_dsn` FOR fornecida e a conexão
    falhar, `PostgresCouponStore.__init__` levanta
    `CouponStoreIntegrationError` em vez de cair pra cá).

    Loga explicitamente qual backend foi escolhido -- nunca fica
    implícito/silencioso qual dos dois está realmente em uso."""
    if postgres_dsn:
        logger.info(
            "Coupon store: PostgresCouponStore -- cupons vão direto pro "
            "Postgres do GG Oferta (mesma máquina); bookkeeping interno "
            "continua no SQLite local (%s).",
            db_path,
        )
        return PostgresCouponStore(postgres_dsn, db_path)
    logger.warning(
        "Coupon store: SqliteCouponStore -- TUDO local (%s), incluindo "
        "`coupons`. O GG Oferta NÃO recebe nenhum cupom enquanto "
        "COUPONS_POSTGRES_DSN não for configurada. Modo esperado só para "
        "uso/teste local do worker isolado.",
        db_path,
    )
    return SqliteCouponStore(db_path)
