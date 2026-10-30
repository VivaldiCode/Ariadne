-- Esquema de restauro da release (ariadne_release). Vem do código do Ariadne e do restore.sh,
-- nunca do arquivo: o arquivo só traz DADOS (db/*.csv), carregados com COPY ... FROM STDIN.
CREATE SCHEMA ariadne_release;
CREATE TABLE ariadne_release.info (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE ariadne_release.source (
  id text PRIMARY KEY, kind text NOT NULL, title text NOT NULL, language text,
  version text NOT NULL, wing text NOT NULL, status text NOT NULL,
  license_class text NOT NULL CHECK (license_class = 'open'), meta jsonb NOT NULL);
CREATE TABLE ariadne_release.wiki_page (
  document_id text PRIMARY KEY, source_id text NOT NULL, path text NOT NULL UNIQUE,
  title text NOT NULL, language text, source_version text NOT NULL,
  fingerprint text NOT NULL, body text NOT NULL);
CREATE TABLE ariadne_release.course (
  slug text NOT NULL, version text NOT NULL, source_id text NOT NULL, PRIMARY KEY (slug, version));
