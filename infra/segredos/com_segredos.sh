#!/bin/sh
# Entrypoint dos consumidores: espera o arquivo entregue pelo serviço "segredos" (volume em
# memória montado em /run/segredos), exporta as variáveis e executa o comando original.
# Uso no compose: entrypoint: ["sh", "/usr/local/bin/com_segredos.sh", <entrypoint original>...]
set -e
arquivo=/run/segredos/segredos.env
espera=0
until [ -s "$arquivo" ]; do
  if [ "$espera" -ge 120 ]; then
    echo "com_segredos: $arquivo não apareceu em 120 s (serviço segredos no ar?)" >&2
    exit 1
  fi
  sleep 2
  espera=$((espera + 2))
done
set -a
. "$arquivo"
set +a
exec "$@"
