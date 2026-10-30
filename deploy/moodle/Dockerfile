# Moodle 4.5 LTS (PHP 8.3, Apache) + CodeRunner, para o Ariadne (STRUCTURE.md, 6.2 e 14.3).
#
# A base é a imagem oficial de desenvolvimento do Moodle HQ (moodlehq/moodle-php-apache, multi-arch,
# sem as restrições de licença das imagens Bitnami de 2025). O código do Moodle e dos plugins entra
# por tarball de uma tag fixa; o plugin local `local_ariadne` publica cursos a partir do formato
# canónico. Contexto de build: este diretório.
FROM moodlehq/moodle-php-apache:8.3-bookworm

ARG MOODLE_VERSION=v4.5.15
ARG CODERUNNER_VERSION=v5.10.6
ARG CODERUNNER_BEHAVIOUR_VERSION=v1.4.8

RUN set -eux; \
    fetch() { curl -fsSL "$1" | tar -xz --strip-components=1 -C "$2"; }; \
    fetch "https://github.com/moodle/moodle/archive/refs/tags/${MOODLE_VERSION}.tar.gz" /var/www/html; \
    mkdir -p /var/www/html/question/type/coderunner /var/www/html/question/behaviour/adaptive_adapted_for_coderunner; \
    fetch "https://github.com/trampgeek/moodle-qtype_coderunner/archive/refs/tags/${CODERUNNER_VERSION}.tar.gz" /var/www/html/question/type/coderunner; \
    fetch "https://github.com/trampgeek/moodle-qbehaviour_adaptive_adapted_for_coderunner/archive/refs/tags/${CODERUNNER_BEHAVIOUR_VERSION}.tar.gz" /var/www/html/question/behaviour/adaptive_adapted_for_coderunner; \
    # O behaviourtype.php do CodeRunner estende uma classe do Moodle sem a carregar (falha na revisão de
    # tentativas pelos web services: «Class qbehaviour_adaptive_type not found»): carrega-a.
    sed -i "s#^defined('MOODLE_INTERNAL') || die();#&\nglobal \$CFG;\nrequire_once(\$CFG->dirroot . '/question/behaviour/adaptive/behaviourtype.php');#" \
        /var/www/html/question/behaviour/adaptive_adapted_for_coderunner/behaviourtype.php; \
    chown -R www-data:www-data /var/www/html

COPY --chown=www-data:www-data config.php /var/www/html/config.php
COPY --chown=www-data:www-data local_ariadne /var/www/html/local/ariadne
COPY php-ariadne.ini /usr/local/etc/php/conf.d/99-ariadne.ini
COPY install-database.php /usr/local/share/ariadne/install-database.php
COPY docker-entrypoint.d/10-ariadne.sh /docker-entrypoint.d/10-ariadne.sh
RUN chmod +x /docker-entrypoint.d/10-ariadne.sh

VOLUME /var/www/moodledata
HEALTHCHECK --interval=20s --timeout=10s --start-period=600s --retries=5 \
    CMD curl -fsS -o /dev/null -H "Host: moodle" http://localhost/login/index.php || exit 1
