"""Setup script — validates environment and initializes services."""

import os
import sys


def check_env():
    """Check required environment variables."""
    required = ["GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT"]
    missing = []
    for var in required:
        if not os.environ.get(var):
            missing.append(var)

    if missing:
        print(f"Missing required env vars: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your values.")
        return False

    print("Environment variables: OK")
    return True


def check_gcloud():
    """Check if gcloud CLI is available and authenticated."""
    import subprocess
    try:
        result = subprocess.run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            capture_output=True, text=True, timeout=10
        )
        account = result.stdout.strip()
        if account:
            print(f"GCloud authenticated as: {account}")
            return True
        print("GCloud not authenticated. Run: gcloud auth login")
        return False
    except FileNotFoundError:
        print("gcloud CLI not found. Install from: https://cloud.google.com/sdk/docs/install")
        return False


def check_firestore():
    """Check Firestore connectivity."""
    try:
        from google.cloud import firestore

        from agent.config import settings
        firestore.Client(project=settings.google_cloud_project)
        print(f"Firestore connected: {settings.google_cloud_project}")
        return True
    except Exception as e:
        print(f"Firestore connection failed: {e}")
        return False


def main():
    print("=== NexusMind AI — Environment Check ===\n")
    checks = [
        ("Environment Variables", check_env),
        ("Google Cloud CLI", check_gcloud),
        ("Firestore", check_firestore),
    ]

    results = []
    for name, check in checks:
        print(f"Checking {name}...", end=" ")
        ok = check()
        results.append((name, ok))
        print()

    print("\n=== Results ===")
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")

    all_ok = all(ok for _, ok in results)
    if all_ok:
        print("\nAll checks passed! Ready to run.")
    else:
        print("\nSome checks failed. Fix the issues above before running.")

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
