#!/usr/bin/env bash
# Publica o MVP no Azure App Service (container Linux), direto do Azure Cloud Shell.
#
#   1. abra https://shell.azure.com (Bash) e faça upload do mvp-alertas.zip (ícone "Upload")
#   2. unzip mvp-alertas.zip -d mvp-alertas && cd mvp-alertas && bash azure/deploy.sh
#
# Não precisa de Docker: a imagem é montada na nuvem pelo Azure Container Registry (az acr build).
# Rodar de novo atualiza a mesma URL. Variáveis opcionais: LOCAL, NOME, SKU, RG.
set -euo pipefail
LOCAL="${LOCAL:-brazilsouth}"
RG="${RG:-rg-citizentech}"
SKU="${SKU:-B1}"
SUB_ID="$(az account show --query id -o tsv)"
SUFIXO="$(echo -n "$SUB_ID" | md5sum | cut -c1-6)"   # estável: rodar de novo reaproveita os recursos
NOME="${NOME:-alerta-enchente-calor-$SUFIXO}"
ACR="${ACR:-acrcitizentech$SUFIXO}"
PLANO="plan-citizentech"
IMAGEM="$ACR.azurecr.io/mvp-alertas:latest"
TAG="$ACR.azurecr.io/mvp-alertas:$(date +%Y%m%d%H%M)"

echo ">> assinatura: $(az account show --query name -o tsv) | região: $LOCAL | app: $NOME"
for p in Microsoft.ContainerRegistry Microsoft.Web; do az provider register -n "$p" --wait >/dev/null; done

az group create -n "$RG" -l "$LOCAL" -o none
az acr show -n "$ACR" -o none 2>/dev/null || az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled true -o none

echo ">> montando a imagem na nuvem (3 a 6 minutos)…"
az acr build -r "$ACR" -t "${IMAGEM#*/}" -t "${TAG#*/}" . --no-logs -o none || {
  echo "!! A assinatura não permite 'az acr build' (comum em contas de estudante)."
  echo "   Use o caminho do GitHub Actions descrito no README (seção Deploy) e rode este script de novo com PULAR_BUILD=1."
  [ "${PULAR_BUILD:-0}" = "1" ] || exit 1; }

az appservice plan show -g "$RG" -n "$PLANO" -o none 2>/dev/null || \
  az appservice plan create -g "$RG" -n "$PLANO" --is-linux --sku "$SKU" -o none

USR="$(az acr credential show -n "$ACR" --query username -o tsv)"
PWD_ACR="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"
if ! az webapp show -g "$RG" -n "$NOME" -o none 2>/dev/null; then
  az webapp create -g "$RG" -p "$PLANO" -n "$NOME" --container-image-name "$IMAGEM" \
    --container-registry-url "https://$ACR.azurecr.io" --container-registry-user "$USR" \
    --container-registry-password "$PWD_ACR" -o none
fi
az webapp config container set -g "$RG" -n "$NOME" --container-image-name "$TAG" \
  --container-registry-url "https://$ACR.azurecr.io" --container-registry-user "$USR" \
  --container-registry-password "$PWD_ACR" -o none 2>/dev/null || true

TOKEN="$(az webapp config appsettings list -g "$RG" -n "$NOME" --query "[?name=='ADMIN_TOKEN'].value" -o tsv)"
TOKEN="${TOKEN:-$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-24)}"
az webapp config appsettings set -g "$RG" -n "$NOME" -o none --settings \
  WEBSITES_PORT=8000 WEBSITES_ENABLE_APP_SERVICE_STORAGE=true WEBSITES_CONTAINER_START_TIME_LIMIT=600 \
  PIPELINE_MODO="${PIPELINE_MODO:-offline}" PIPELINE_INTERVALO_MIN=60 DEMO_SEED=1 ADMIN_TOKEN="$TOKEN"
az webapp config set -g "$RG" -n "$NOME" --always-on true --http20-enabled true -o none 2>/dev/null || true
az webapp update -g "$RG" -n "$NOME" --https-only true -o none
az webapp restart -g "$RG" -n "$NOME" -o none

URL="https://$NOME.azurewebsites.net"
echo ">> aguardando o app subir (a primeira vez leva uns 2 minutos)…"
for i in $(seq 1 40); do
  if curl -fsS "$URL/api/v1/saude" >/dev/null 2>&1; then break; fi; sleep 10
done
curl -fsS "$URL/api/v1/saude" && echo
echo
echo "================ PRONTO ================"
echo "Cadastro:      $URL/"
echo "Painel:        $URL/painel"
echo "API (Swagger): $URL/docs"
echo "ADMIN_TOKEN (rodar o pipeline na hora): $TOKEN"
echo "Logs:          az webapp log tail -g $RG -n $NOME"
