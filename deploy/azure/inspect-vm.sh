#!/usr/bin/env bash
# Read-only inventory. Run in Azure Cloud Shell (Bash) after signing in.
# Usage: bash inspect-vm.sh SUBSCRIPTION_ID RESOURCE_GROUP VM_NAME
set -euo pipefail
subscription=${1:?Provide the subscription ID}
resource_group=${2:?Provide the VM resource group}
vm_name=${3:?Provide the VM name}

az account show --subscription "$subscription" \
  --query '{subscription:id,tenant:tenantId}' --output json
az vm show --subscription "$subscription" --resource-group "$resource_group" --name "$vm_name" \
  --query '{name:name,group:resourceGroup,location:location,size:hardwareProfile.vmSize,identity:identity.type,principal:identity.principalId}' --output json
az acr list --subscription "$subscription" \
  --query '[].{name:name,group:resourceGroup,location:location,loginServer:loginServer}' --output table

# Azure Run Command executes this read-only shell inspection on the existing VM.
# Do not dump container environments, inspect entire containers, or read .env files.
az vm run-command invoke --subscription "$subscription" \
  --resource-group "$resource_group" --name "$vm_name" --command-id RunShellScript \
  --scripts 'set -eu
uname -m
df -h /
free -m
docker --version
docker compose version
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
docker volume ls --format "table {{.Name}}\t{{.Driver}}"
if command -v az >/dev/null 2>&1; then az version --query "\"azure-cli\"" --output tsv; else echo "Azure CLI: not installed on VM"; fi' \
  --query 'value[].message' --output tsv
