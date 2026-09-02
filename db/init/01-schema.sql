-- obs-lab inventory database
-- (pg_stat_statements e criado em 00-dbm.sql, nos dois databases)

CREATE TABLE IF NOT EXISTS products (
    id          VARCHAR(20) PRIMARY KEY,
    name        VARCHAR(100) NOT NULL,
    price       NUMERIC(10,2) NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS orders (
    id           VARCHAR(40) PRIMARY KEY,
    customer_id  VARCHAR(50),
    amount       NUMERIC(10,2),
    status       VARCHAR(20),
    payment_id   VARCHAR(50),
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
    id          SERIAL PRIMARY KEY,
    order_id    VARCHAR(40) REFERENCES orders(id),
    product_id  VARCHAR(20) REFERENCES products(id),
    qty         INTEGER,
    price       NUMERIC(10,2)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- Seed: mesmo catálogo do gateway (5 produtos TCG)
INSERT INTO products (id, name, price, stock) VALUES
    ('prod-001', 'Pokémon TCG Booster', 19.99, 500),
    ('prod-002', 'Yu-Gi-Oh! Deck',      34.99, 300),
    ('prod-003', 'MTG Draft Set',       129.99, 80),
    ('prod-004', 'Deck Sleeves 100pk',  12.99, 1000),
    ('prod-005', 'Playmat Premium',     49.99, 150),
    ('prod-006', 'Single Card Common',   2.99, 2000)
ON CONFLICT (id) DO NOTHING;

-- Auth + auditoria de seguranca (fluxos de fraude)
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(200) NOT NULL,
    last_login_ip VARCHAR(45),
    last_login_at TIMESTAMP,
    failed_logins INTEGER DEFAULT 0,
    locked        BOOLEAN DEFAULT FALSE,
    -- quando travou: o gateway destrava sozinho depois de LOCK_TTL_MS
    locked_at     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS security_events (
    id          SERIAL PRIMARY KEY,
    event_type  VARCHAR(50),
    threat      VARCHAR(50),
    username    VARCHAR(50),
    client_ip   VARCHAR(45),
    risk_score  INTEGER,
    detail      TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_secevents_type ON security_events(event_type);
CREATE INDEX IF NOT EXISTS idx_secevents_threat ON security_events(threat);
-- users demo criados pela app no boot (hash real)
