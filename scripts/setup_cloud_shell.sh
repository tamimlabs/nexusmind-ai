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
echo ""
echo "Dashboard will be available at: http://localhost:8080"
