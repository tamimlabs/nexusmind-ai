#!/bin/bash
# Cloud Run deployment — one command deploy
# Usage: bash scripts/deploy.sh [PROJECT_ID] [REGION]

set -e

PROJECT_ID="${1:-$(gcloud config get-value project)}"
REGION="${2:-us-central1}"
SERVICE_NAME="nexusmind-ai"

echo "Deploying $SERVICE_NAME to Cloud Run..."
echo "  Project: $PROJECT_ID"
echo "  Region:  $REGION"

echo "Ensuring Pub/Sub topics exist (idempotent)..."
gcloud pubsub topics describe nexusmind-tasks --project "$PROJECT_ID" >/dev/null 2>&1 || gcloud pubsub topics create nexusmind-tasks --project "$PROJECT_ID" || true
gcloud pubsub topics describe nexusmind-events --project "$PROJECT_ID" >/dev/null 2>&1 || gcloud pubsub topics create nexusmind-events --project "$PROJECT_ID" || true

echo "Ensuring Firestore database exists (idempotent)..."
gcloud firestore databases describe --project "$PROJECT_ID" --region "$REGION" >/dev/null 2>&1 || gcloud firestore databases create --project "$PROJECT_ID" --location "$REGION" --type=firestore --quiet || true

gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --platform managed \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --allow-unauthenticated \
    --memory 512Mi \
    --cpu 1 \
    --min-instances 0 \
    --max-instances 5 \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_REGION=$REGION,DATABASE_BACKEND=firestore,GOOGLE_CLOUD_REGION=$REGION" \
    --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,GITHUB_TOKEN=GITHUB_TOKEN:latest" \
    --service-account "nexusmind-sa@$PROJECT_ID.iam.gserviceaccount.com"

echo "Deployed! Getting URL..."
gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format="value(status.url)"

echo ""
echo "Note: If the service account doesn't exist yet, run:"
echo "  gcloud iam service-accounts create nexusmind-sa --display-name='NexusMind AI'"
echo "  gcloud secrets create GEMINI_API_KEY --data-file=-"
