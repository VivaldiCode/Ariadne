#!/bin/sh
# Descarrega o ZIM de teste pequeno (1,8 MB) para a pasta dos ZIM.
# Uso: deploy/kiwix/get-test-zim.sh [pasta]   (por omissão: ./data/zim)
set -eu

ZIM=wikipedia_en_ray-charles_nopic_2026-08.zim
DEST=${1:-./data/zim}

mkdir -p "$DEST"
if [ -f "$DEST/$ZIM" ]; then
  echo "Already exists: $DEST/$ZIM"
  exit 0
fi
curl -fsSL --retry 3 -o "$DEST/$ZIM.part" "https://download.kiwix.org/zim/wikipedia/$ZIM"
mv "$DEST/$ZIM.part" "$DEST/$ZIM"
echo "Downloaded: $DEST/$ZIM"
