-- The benchmark table: a Zipf-like customer key and a cluster of recent
-- timestamps, each with 1% NULLs and its own B-tree index.
--
-- Run with: psql -v rows=15000000 -f bench/skewed_table.sql
-- The seed and the fixed reference time make the data the same on every run.
-- It never drops anything: if bench_orders already exists, it stops with an
-- error, so pointing it at a real database cannot destroy a table.

\set ON_ERROR_STOP on
\set QUIET on

CREATE TABLE bench_orders (
    order_id bigint NOT NULL,
    -- P(k) is proportional to 1/k on [1, 10^7): about 4% of the rows have
    -- customer 1, and the lowest 1% of the key range holds 71% of the rows.
    customer_id bigint,
    -- 60% of the rows fall in the last 7 days of a span of about 3 years.
    created_at timestamp,
    amount numeric(12, 2) NOT NULL,
    note text NOT NULL
);

SELECT setseed(0.42) \gset

INSERT INTO bench_orders
SELECT
    g,
    CASE WHEN random() >= 0.01 THEN floor(exp(random() * ln(10000000)))::bigint END,
    CASE
        WHEN random() < 0.01 THEN NULL
        WHEN random() < 0.6 / 0.99 THEN timestamp '2026-01-01' - random() * interval '7 days'
        ELSE timestamp '2025-12-25' - random() * interval '3 years'
    END,
    round((random() * 1000)::numeric, 2),
    md5(g::text)
FROM generate_series(1, :rows) AS g;

CREATE INDEX bench_orders_customer_id ON bench_orders (customer_id);
CREATE INDEX bench_orders_created_at ON bench_orders (created_at);
VACUUM ANALYZE bench_orders;
