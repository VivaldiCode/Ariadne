#!/bin/sh
# Entrypoint do kiwix-serve do Ariadne (Fase 12).
#
# O kiwix-serve corre com `--library --monitorLibrary` sobre um library.xml que ESTE script mantém
# igual à pasta de ZIM (/data, montada só de leitura): ZIM novo -> `kiwix-manage add`; ficheiro que
# desapareceu (arquivado ou apagado no dashboard) -> `kiwix-manage remove`. Assim um ZIM que o
# dashboard descarrega passa a ser servido em segundos, sem reiniciar nem tocar em nada. O library.xml
# vive em /library (um tmpfs gravável): reconstrói-se do zero em cada arranque, por isso não há estado
# que se perca, e os ZIM nunca são gravados pelo kiwix. Os nomes dos livros mantêm-se (o nome do
# ficheiro sem «.zim»), que é o que o Ariadne usa nos URL e na pesquisa.
set -u

DATA="${ZIM_DATA_DIR:-/data}"
LIB_DIR="${LIBRARY_DIR:-/library}"
LIB="$LIB_DIR/library.xml"
INTERVAL="${LIBRARY_SYNC_SECONDS:-5}"
PORT="${PORT:-8080}"

SEEN="$LIB_DIR/.tentados"   # «caminho tamanho-mtime» já acrescentados (ou recusados) nesta vida do contentor
mkdir -p "$LIB_DIR"
: > "$SEEN"
printf '<?xml version="1.0" encoding="UTF-8"?>\n<library version="20110515"></library>\n' > "$LIB"

# «id<TAB>caminho» de cada livro da biblioteca
listing() {
  kiwix-manage "$LIB" show 2>/dev/null | awk '
    /^id:/   { id = $2 }
    /^path:/ { p = $0; sub(/^path:[ \t]*/, "", p); print id "\t" p }'
}

sync_library() {
  current="$(listing)"
  # ZIM novos
  for file in "$DATA"/*.zim; do
    [ -f "$file" ] || continue
    printf '%s\n' "$current" | cut -f2 | grep -qxF "$file" && continue
    # Duas cópias do mesmo ZIM (mesmo uuid) ocupam a mesma entrada: cada versão do ficheiro só se tenta
    # uma vez, senão as duas andariam a substituir-se de 5 em 5 segundos.
    signature="$file $(stat -c '%s-%Y' "$file" 2>/dev/null)"
    grep -qxF "$signature" "$SEEN" && continue
    printf '%s\n' "$signature" >> "$SEEN"
    if kiwix-manage "$LIB" add "$file" >/dev/null 2>&1; then
      echo "ariadne-kiwix: + $file"
    else
      echo "ariadne-kiwix: could not add $file (still copying? corrupt?)" >&2
    fi
  done
  # ZIM que desapareceram
  printf '%s\n' "$current" | while IFS="$(printf '\t')" read -r id path; do
    [ -n "$id" ] || continue
    [ -f "$path" ] && continue
    kiwix-manage "$LIB" remove "$id" >/dev/null 2>&1 && echo "ariadne-kiwix: - $path"
  done
}

sync_library
(
  while sleep "$INTERVAL"; do sync_library; done
) &

exec kiwix-serve --port="$PORT" --library --monitorLibrary "$LIB"
