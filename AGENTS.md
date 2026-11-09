# Agent instructions

Read [RUNBOOK.md](RUNBOOK.md) before changing infrastructure or onboarding a site. It is the canonical procedure for both Codex and Claude.

Keep the implementation to CloudFormation, AWS/GitHub CLIs, Python's standard library, and the website's existing build command. Use each website's `site.json` as its configuration. Inspect the current AWS identity and account before applying changes.

Preserve the private S3 origin, per-site deployment permissions, exact production branch/repository trust, directory routing, and canonical redirects. Run the routing/configuration tests and CloudFormation checks after changing shared code. Keep updates to `v1` backward compatible; document and version breaking changes separately.
