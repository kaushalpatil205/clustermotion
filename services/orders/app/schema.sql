-- Applied at startup by orders-svc under an advisory lock (idempotent).
CREATE TABLE IF NOT EXISTS orders (
    id               UUID PRIMARY KEY,
    idempotency_key  TEXT        NOT NULL UNIQUE,
    sku              TEXT        NOT NULL,
    qty              INTEGER     NOT NULL CHECK (qty > 0),
    status           TEXT        NOT NULL DEFAULT 'PENDING',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT        NOT NULL,          -- cluster that accepted the write
    fulfilled_at     TIMESTAMPTZ,
    fulfilled_by     TEXT                           -- cluster whose worker fulfilled it
);

CREATE INDEX IF NOT EXISTS orders_pending_idx
    ON orders (created_at) WHERE status = 'PENDING';

-- One row per message processed. applied = false means the message was a
-- duplicate delivery and the idempotent UPDATE changed nothing.
CREATE TABLE IF NOT EXISTS fulfillment_log (
    id           BIGSERIAL PRIMARY KEY,
    order_id     UUID        NOT NULL,
    cluster      TEXT        NOT NULL,
    applied      BOOLEAN     NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per sweeper *attempt*. outcome is one of:
--   ran                the attempt that did the work for this slot
--   duplicate-skipped  another attempt already ran this slot (idempotent claim)
--   fenced             this cluster did not hold the singleton lease
-- Reconciliation expects exactly one 'ran' row per slot and no missing slots.
CREATE TABLE IF NOT EXISTS sweeper_runs (
    id          BIGSERIAL PRIMARY KEY,
    slot        TIMESTAMPTZ NOT NULL,
    cluster     TEXT        NOT NULL,
    outcome     TEXT        NOT NULL,
    republished INTEGER     NOT NULL DEFAULT 0,
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS sweeper_one_run_per_slot
    ON sweeper_runs (slot) WHERE outcome = 'ran';
