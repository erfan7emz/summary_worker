# Build and push the new worker image to ACR
az acr build --registry summaryworkeracr --image summary-workers:latest .

az acr login --name summaryworkeracr

docker buildx build --platform linux/amd64 \
  -t summaryworkeracr.azurecr.io/summary-workers:latest \
  --push .

# The Container App will automatically pull and deploy the new image
# However, you can force a new deployment with:
az containerapp update \
    --name summary-workers \
    --resource-group summary-rg \
    --image summaryworkeracr.azurecr.io/summary-workers:latest


# Test worker locally
celery -A worker worker --loglevel=info