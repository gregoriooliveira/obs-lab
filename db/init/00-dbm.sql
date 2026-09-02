-- DBM / Database Query Performance (Splunk O11y + AppD Database Monitoring)
--
-- O receiver postgresql abre a conexao de top_query no database DEFAULT
-- (postgres), nao no database de negocio. Sem a extensao la, o scrape falha com
--   pq: relation "pg_stat_statements" does not exist
-- Por isso a extensao e criada nos DOIS databases.
--
-- Requer shared_preload_libraries=pg_stat_statements no servidor (ver docker-compose.yml).

\connect postgres
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- o database de negocio vem do .env (DB_NAME), nao e fixo em "inventory"
\set dbname `echo "${POSTGRES_DB:-inventory}"`
\connect :"dbname"
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
