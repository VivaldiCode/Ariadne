-- O papel Postgres do gpu-gateway (o processo que o túnel da Cloudflare expõe).
-- Só vê e escreve o que o túnel precisa em `batch_run`: não lê utilizadores, chaves, documentos,
-- curadoria nem nada mais. Correr uma vez, como administrador, depois das migrações:
--
--   psql -h <host> -U ariadne -d ariadne -v dbname=ariadne -v password="'<palavra-passe>'" \
--        -f deploy/postgres/gpu-gateway.sql
--
-- e pôr no stack `GPU_GATEWAY_DATABASE_URL=postgresql+psycopg://ariadne_gateway:<palavra-passe>@postgres/ariadne`.
CREATE ROLE ariadne_gateway LOGIN PASSWORD :password NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE :"dbname" TO ariadne_gateway;
GRANT USAGE ON SCHEMA public TO ariadne_gateway;
GRANT SELECT ON batch_run TO ariadne_gateway;
GRANT UPDATE (status, running_at, finished_at, results_gz, progress, last_heartbeat_at,
              token_hash, token_expires_at) ON batch_run TO ariadne_gateway;
