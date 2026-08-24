# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | Yes                |

## Reporting a Vulnerability

If you discover a security vulnerability within NexusMind AI, please send an email to [contact.tamimlabs@gmail.com](mailto:contact.tamimlabs@gmail.com). All security vulnerabilities will be promptly addressed.

**Please do NOT report security vulnerabilities through public GitHub issues.**

## Security Best Practices

When using NexusMind AI:

1. **API Keys**: Never commit API keys to version control. Use `.env` files and ensure they're in `.gitignore`.

2. **High-Risk Tools**: The `execute_code` and `run_command` tools require human approval by default. Do not disable this without understanding the risks.

3. **Network Access**: The agent can make outbound HTTP requests. Be aware of what data it can access.

4. **Cloud Deployment**: When deploying to Cloud Run, ensure proper IAM roles and minimal permissions.

5. **Environment Variables**: Use separate API keys for development and production.

6. **Watcher Autonomy Is Memory-Gated**: Watcher-generated goals require a matching standing instruction in memory. Anyone who can add memory instructions effectively grants automation powers, so protect dashboard and API access accordingly.

7. **Local Token Storage**: GitHub and other watcher tokens stored in `data/watcher_state.json` and `.env` are stored as plaintext locally. Ensure appropriate host access control, and rotate any token that may have been leaked.

## Authentication

- API endpoints do not require authentication by default (demo mode)
- For production use, implement proper authentication middleware
- Never expose the dashboard without authentication in public networks
