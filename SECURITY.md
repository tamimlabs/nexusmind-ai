# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability within NexusMind AI, please send an email to Tamim Hasan. All security vulnerabilities will be promptly addressed.

**Please do NOT report security vulnerabilities through public GitHub issues.**

## Security Best Practices

When using NexusMind AI:

1. **API Keys**: Never commit API keys to version control. Use `.env` files and ensure they're in `.gitignore`.

2. **High-Risk Tools**: The `execute_code` and `run_command` tools require human approval by default. Do not disable this without understanding the risks.

3. **Network Access**: The agent can make outbound HTTP requests. Be aware of what data it can access.

4. **Cloud Deployment**: When deploying to Cloud Run, ensure proper IAM roles and minimal permissions.

5. **Environment Variables**: Use separate API keys for development and production.

## Authentication

- API endpoints do not require authentication by default (demo mode)
- For production use, implement proper authentication middleware
- Never expose the dashboard without authentication in public networks
