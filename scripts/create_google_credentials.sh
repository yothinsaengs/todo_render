#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${1:-${PROJECT_ID:-}}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-todo-render}"
KEY_FILE="${GOOGLE_KEY_FILE:-secrets/google-service-account.json}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Usage: bash scripts/create_google_credentials.sh GOOGLE_CLOUD_PROJECT_ID" >&2
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud is required. Run this script in Google Cloud Shell or install the Google Cloud CLI." >&2
  exit 1
fi

SERVICE_ACCOUNT_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}"
gcloud services enable \
  iam.googleapis.com \
  sheets.googleapis.com

if gcloud iam service-accounts describe "${SERVICE_ACCOUNT_EMAIL}" >/dev/null 2>&1; then
  echo "Service account already exists: ${SERVICE_ACCOUNT_EMAIL}"
else
  gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
    --display-name="Todo Render Focus Board" \
    --description="Reads and writes Focus Board data in Google Sheets"
fi

if [[ -e "${KEY_FILE}" ]]; then
  echo "Refusing to overwrite existing key: ${KEY_FILE}" >&2
  exit 1
fi

mkdir -p "$(dirname "${KEY_FILE}")"
gcloud iam service-accounts keys create "${KEY_FILE}" \
  --iam-account="${SERVICE_ACCOUNT_EMAIL}"
chmod 600 "${KEY_FILE}"

echo
echo "Credential created: ${KEY_FILE}"
echo "Share the spreadsheet with:"
echo "${SERVICE_ACCOUNT_EMAIL}"
echo
echo "Local .env value:"
echo "GOOGLE_SERVICE_ACCOUNT_FILE=./${KEY_FILE}"
