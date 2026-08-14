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
    ('prod-005', 'Playmat Premium',     49.99, 150)
ON CONFLICT (id) DO NOTHING;
