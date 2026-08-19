# Cloud Run deployment (Windows PowerShell)
# Usage: .\scripts\deploy.ps1 [PROJECT_ID] [REGION]

param(
    [string]$ProjectId = "",
    [string]$Region = "us-central1"
)

if (-not $ProjectId) {
    $ProjectId = (gcloud config get-value project 2>$null)
}

$ServiceName = "nexusmind-ai"

Write-Host "Deploying $ServiceName to Cloud Run..." -ForegroundColor Green
Write-Host "  Project: $ProjectId"
Write-Host "  Region:  $Region"

gcloud run deploy $ServiceName `
    --source . `
    --platform managed `
    --region $Region `
    --project $ProjectId `
    --allow-unauthenticated `
    --memory 512Mi `
    --cpu 1 `
    --min-instances 0 `
    --max-instances 5 `
    --set-env-vars "GOOGLE_CLOUD_PROJECT=$ProjectId,GOOGLE_CLOUD_REGION=$Region"

Write-Host "`nDeployed! Getting URL..." -ForegroundColor Green
$Url = gcloud run services describe $ServiceName --region $Region --format="value(status.url)"
Write-Host "URL: $Url" -ForegroundColor Cyan
