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
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_REGION=$REGION,DATABASE_BACKEND=firestore" \
    --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest" \
    --service-account "nexusmind-sa@$PROJECT_ID.iam.gserviceaccount.com"

echo "Deployed! Getting URL..."
gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format="value(status.url)"

echo ""
echo "Note: If the service account doesn't exist yet, run:"
echo "  gcloud iam service-accounts create nexusmind-sa --display-name='NexusMind AI'"
echo "  gcloud secrets create GEMINI_API_KEY --data-file=-"
