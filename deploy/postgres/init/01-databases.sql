-- Corre só na primeira inicialização do volume pgdata (docker-entrypoint-initdb.d).
-- Cria as bases do Ariadne, do Wiki.js e do Moodle no mesmo servidor (secção 10).
-- Idempotente: o POSTGRES_USER=ariadne já cria a base 'ariadne' por omissão.

SELECT 'CREATE DATABASE ariadne OWNER ariadne'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ariadne')\gexec

SELECT 'CREATE DATABASE wikijs OWNER ariadne'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'wikijs')\gexec

SELECT 'CREATE DATABASE moodle OWNER ariadne'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'moodle')\gexec
