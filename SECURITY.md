# Security Policy

## Reporting a vulnerability
Please **don't open a public issue** for security problems. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. Include steps to reproduce and the impact you expect. I'll acknowledge reports within 7 days.

Please don't test against other people's Slack workspaces or API keys.

## Security design at a glance
- **Model output is untrusted input.** Every tool argument that reaches a file path, URL or paid API is validated in code: path sanitizing, URL host allow-list, IATA and date checks.
- **Secrets never reach the model.** API keys are masked in errors at the source and redacted again before anything goes to the LLM, logs, database or UI.
- **Low agency.** All tools are read-only lookups; the agent can't send, delete, pay or change anything.
- **Abuse limits.** Per-user rate limit, input size cap, event dedupe, request timeouts, bounded agent loop.
- **Honest data.** Tool failures and simulated data are flagged to both the model and the user.
- **Least privilege in the cloud.** Non-root container, no inbound ports (Slack Socket Mode), HTTPS-only egress, IAM scoped to specific resources, KMS encryption.
- **CI security gates.** Bandit, pip-audit, Gitleaks and cfn-lint run on every push.

Full threat model, OWASP LLM Top 10 mapping, findings and remaining risks: **[docs/SECURITY_ASSESSMENT.md](docs/SECURITY_ASSESSMENT.md)**.
