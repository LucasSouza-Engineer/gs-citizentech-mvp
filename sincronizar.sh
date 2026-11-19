#!/usr/bin/env bash
# Copia para este repositório as duas peças que o MVP reaproveita SEM alterar:
#   visao/    <- visao-enderecos (OCR do endereço + API de cadastro + tela)
#   pipeline/ <- pipeline-dados  (regras de risco por CEP e fila de alertas)
# Rode de novo sempre que um dos dois projetos mudar.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
VISAO="${1:-$AQUI/../visao-enderecos}"
PIPE="${2:-$AQUI/../pipeline-dados}"
rm -rf "$AQUI/visao" "$AQUI/pipeline"
mkdir -p "$AQUI/visao/api" "$AQUI/pipeline"
cp -r "$VISAO/src" "$AQUI/visao/"
cp "$VISAO/api/app.py" "$AQUI/visao/api/"
cp -r "$VISAO/api/static" "$AQUI/visao/api/"
touch "$AQUI/visao/api/__init__.py"
cp -r "$PIPE/dags/citizentech" "$AQUI/pipeline/"
cp -r "$PIPE/dados/amostra" "$AQUI/pipeline/amostra"
find "$AQUI/visao" "$AQUI/pipeline" -name __pycache__ -prune -exec rm -rf {} +
echo "ok: visao/ e pipeline/ atualizados"
