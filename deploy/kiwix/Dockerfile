# kiwix-serve com o skin do Ariadne (o mesmo design system do dashboard e do tema do Wiki.js).
#
# O skin do kiwix-serve vem compilado no libkiwix (static/skin, static/templates, static/viewer.html), por isso
# a imagem compila o libkiwix com os ficheiros de deploy/kiwix/static por cima e liga-lhe o kiwix-serve. O resto
# (kiwix-manage, start.sh, dumb-init, utilizador, portas) vem da imagem oficial, na MESMA versão que o stack usa
# (docker-compose.yml), para ser um substituto direto: mesmos volumes, porta (8080) e argumentos.
#
# Contexto de build = deploy/  →  docker buildx build -f deploy/kiwix/Dockerfile deploy
# (o deploy/kiwix/build.sh faz isto e publica no registo).
#
# Hardware (CLAUDE.md, secção 9): tudo compilado para x86-64 genérico, nunca com -march=native; o NAS
# tem Xeon X5550 (sem AVX).

ARG KIWIX_TOOLS_VERSION=3.8.2

FROM ghcr.io/kiwix/kiwix-serve:${KIWIX_TOOLS_VERSION} AS build
USER root
ARG KIWIX_TOOLS_VERSION
ARG LIBKIWIX_VERSION=14.2.1
ARG LIBZIM_VERSION=9.8.2
ARG DOCOPT_VERSION=v0.6.3
RUN apk add --no-cache build-base meson ninja python3 pkgconf git cmake linux-headers \
      icu-dev icu-static xapian-core-dev zstd-dev xz-dev util-linux-dev zlib-dev \
      pugixml-dev curl-dev libmicrohttpd-dev
ENV CFLAGS="-O2" CXXFLAGS="-O2"

WORKDIR /src
# docopt.cpp (a linha de comandos do kiwix-serve)
RUN git clone -q --depth 1 --branch ${DOCOPT_VERSION} https://github.com/docopt/docopt.cpp.git docopt \
 && cmake -S docopt -B docopt/build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX=/usr/local \
 && cmake --build docopt/build -j"$(nproc)" && cmake --install docopt/build
# libzim (reader)
RUN git clone -q --depth 1 --branch ${LIBZIM_VERSION} https://github.com/openzim/libzim.git libzim \
 && meson setup libzim/build libzim --prefix=/usr/local --libdir=lib --buildtype=release --default-library=static \
      -Dtests=false -Dexamples=false -Ddoc=false \
 && ninja -C libzim/build -j"$(nproc)" install
# mustache.hpp (cabeçalho único, o motor de templates do libkiwix)
RUN git clone -q --depth 1 --branch v4.1 https://github.com/kainjow/Mustache.git mustache \
 && install -D mustache/mustache.hpp /usr/local/include/mustache.hpp
# libkiwix, com o skin do Ariadne por cima do original
RUN git clone -q --depth 1 --branch ${LIBKIWIX_VERSION} https://github.com/kiwix/libkiwix.git libkiwix
COPY kiwix/static/ libkiwix/static/
COPY tema-ariadne/tokens.css tema-ariadne/fonts.css tema-ariadne/favicon.svg tema-ariadne/logo-mark.svg /tmp/ariadne/
COPY tema-ariadne/fonts/*.woff2 /tmp/ariadne/fonts/
RUN set -eu; cd libkiwix/static \
 # tokens + @font-face num só ficheiro, servido em /skin/ariadne/ariadne.css (o Wiki.js carrega-o daqui)
 && mkdir -p skin/ariadne/fonts \
 && cat /tmp/ariadne/tokens.css /tmp/ariadne/fonts.css > skin/ariadne/ariadne.css \
 && cp /tmp/ariadne/favicon.svg /tmp/ariadne/logo-mark.svg skin/ariadne/ && cp /tmp/ariadne/fonts/*.woff2 skin/ariadne/fonts/ \
 # os recursos novos entram na lista de recursos compilados; as fontes antigas (Poppins, Roboto, DM Sans) saem
 && sed -i '/^skin\/fonts\//d' resources_list.txt \
 && { echo skin/ariadne/ariadne.css; echo skin/ariadne/favicon.svg; echo skin/ariadne/logo-mark.svg; for f in skin/ariadne/fonts/*.woff2; do echo "$f"; done; } >> resources_list.txt \
 && rm -rf skin/fonts \
 # .woff2 é um recurso binário e tem de sair com o tipo MIME certo
 && cd .. \
 && sed -i "s/BINARY_RESOURCE_EXTENSIONS = {'.ico', '.png', '.ttf'}/BINARY_RESOURCE_EXTENSIONS = {'.ico', '.png', '.ttf', '.woff2'}/" scripts/kiwix-compile-resources \
 && grep -q "'.woff2'" scripts/kiwix-compile-resources \
 && sed -i 's|^ { "woff", "application/font-woff"},|&\n { "woff2", "font/woff2"},|' src/tools/pathTools.cpp \
 && grep -q '"woff2"' src/tools/pathTools.cpp
# os testes do libkiwix não interessam (e não ligam em estático)
RUN sed -i "/^subdir('test')/d" libkiwix/meson.build \
 && meson setup libkiwix/build libkiwix --prefix=/usr/local --libdir=lib --buildtype=release --default-library=static \
 && ninja -C libkiwix/build -j"$(nproc)" install
# kiwix-tools (só o kiwix-serve interessa)
RUN git clone -q --depth 1 --branch ${KIWIX_TOOLS_VERSION} https://github.com/kiwix/kiwix-tools.git kiwix-tools \
 && meson setup kiwix-tools/build kiwix-tools --prefix=/usr/local --libdir=lib --buildtype=release \
 && ninja -C kiwix-tools/build -j"$(nproc)" \
 && install -D kiwix-tools/build/src/server/kiwix-serve /out/kiwix-serve

FROM ghcr.io/kiwix/kiwix-serve:${KIWIX_TOOLS_VERSION}
ARG KIWIX_TOOLS_VERSION
LABEL org.opencontainers.image.title="kiwix-serve (skin Ariadne)" \
      org.opencontainers.image.description="kiwix-serve ${KIWIX_TOOLS_VERSION} com o skin do design system do Ariadne" \
      org.opencontainers.image.source="https://github.com/VivaldiCode/Ariadne"
USER root
# bibliotecas dinâmicas do kiwix-serve (o libzim e o libkiwix vão estáticos dentro do binário)
RUN apk add --no-cache libstdc++ libgcc icu-libs icu-data-full xapian-core libmicrohttpd libcurl pugixml zstd-libs xz-libs libuuid
COPY --from=build /out/kiwix-serve /usr/local/bin/kiwix-serve
COPY --from=build /usr/local/lib/libdocopt.so* /usr/local/lib/
RUN /usr/local/bin/kiwix-serve --version
USER user
