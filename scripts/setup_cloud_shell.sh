#!/bin/bash
# setup_cloud_shell.sh — Run this in Google Cloud Shell to set up NexusMind AI

set -e

echo "=== NexusMind AI — Cloud Shell Setup ==="

# Clone repo
if [ ! -d "nexusmind-ai" ]; then
    echo "Cloning repository..."
    git clone https://github.com/tamimlabs/nexusmind-ai.git
fi

cd nexusmind-ai

# Install dependencies
echo "Installing dependencies..."
pip install -e . --quiet

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo ""
    echo ">>> IMPORTANT: Edit .env and add your Gemini API keys <<<"
    echo ">>> GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX are optional <<<"
    echo ""
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:  nano .env"
echo "  2. Add your Gemini API keys (comma-separated)"
echo "  3. Run:  python -m api.main"
echo "  4. Click Web Preview button → Preview on port 8080"
echo "  5. Deploy to Cloud Run (always-awake):  DEPLOY=1 bash scripts/setup_cloud_shell.sh"
echo ""
echo "Dashboard will be available at: http://localhost:8080"

# --- Cloud Run Deployment (always-awake event-driven agent) ---
# min-instances=1 keeps one warm instance running at all times so Pub/Sub
# events are handled instantly (no cold starts). max-instances=5 caps cost
# while still allowing burst traffic to scale out.
if [ "${DEPLOY:-0}" = "1" ]; then
    echo ""
    echo "=== Deploying to Cloud Run (min-instances=1) ==="

    PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
    REGION="${REGION:-us-central1}"

    # Cloud Run source deploys require a Dockerfile at the project root
    if [ ! -f "Dockerfile" ] && [ -f "cloud/cloud_run/Dockerfile" ]; then
        cp cloud/cloud_run/Dockerfile Dockerfile
        echo "Copied cloud/cloud_run/Dockerfile -> ./Dockerfile for source deploy"
    fi

    gcloud run deploy nexusmind-ai \
        --source . \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --platform managed \
        --allow-unauthenticated \
        --cpu 1 \
        --memory 512Mi \
        --timeout 3600 \
        --min-instances 1 \
        --max-instances 5 \
        --set-env-vars "GOOGLE_CLOUD_PROJECT=$PROJECT_ID"

    SERVICE_URL=$(gcloud run services describe nexusmind-ai \
        --region "$REGION" --format 'value(status.url)')
    echo ""
    echo "Always-awake agent deployed at: $SERVICE_URL"
else
    echo ""
    echo "=== Cloud Run Deployment (optional, always-awake) ==="
    echo "Re-run with DEPLOY=1 to deploy, or run manually:"
    echo ""
    echo "  cp cloud/cloud_run/Dockerfile Dockerfile   # source deploy needs root Dockerfile"
    echo "  gcloud run deploy nexusmind-ai \\"
    echo "      --source . \\"
    echo "      --region us-central1 \\"
    echo "      --platform managed \\"
    echo "      --allow-unauthenticated \\"
    echo "      --cpu 1 \\"
    echo "      --memory 512Mi \\"
    echo "      --min-instances 1 \\"
    echo "      --max-instances 5 \\"
    echo "      --set-env-vars GOOGLE_CLOUD_PROJECT=\$(gcloud config get-value project)"
fi
