#!/bin/sh
# Constrói o kiwix-serve com o skin do Ariadne para linux/amd64 e publica-o num registo (REGISTRY).
# Uso (na raiz do repositório): deploy/kiwix/build.sh [tag]   (por omissão: o commit atual)
# Publica também a tag móvel MOVING_TAG (por omissão `latest`). REGISTRY é obrigatório para publicar,
# ex.: REGISTRY=docker.io/<namespace>.
#   PUSH=0 deploy/kiwix/build.sh   →  constrói só para a máquina local (PLATFORM, por omissão a do Docker)
set -eu
REGISTRY=${REGISTRY:-}
TAG=${1:-$(git rev-parse --short HEAD)}
MOVING_TAG=${MOVING_TAG:-latest}
if [ "${PUSH:-1}" = 1 ] && [ -z "$REGISTRY" ]; then echo "Set REGISTRY (e.g. REGISTRY=docker.io/<namespace>) or use PUSH=0." >&2; exit 2; fi
IMAGE="${REGISTRY:-ariadne}/kiwix-serve"
if [ "${PUSH:-1}" = 1 ]; then
  echo "== $IMAGE:$TAG"
  docker buildx build --platform linux/amd64 -f deploy/kiwix/Dockerfile \
    -t "$IMAGE:$TAG" -t "$IMAGE:$MOVING_TAG" --push deploy
  echo "Image with tags $TAG and $MOVING_TAG in $IMAGE"
else
  docker buildx build ${PLATFORM:+--platform $PLATFORM} -f deploy/kiwix/Dockerfile -t "$IMAGE:$TAG" --load deploy
fi
