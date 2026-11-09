# Static site runbook

This is the shared procedure for humans, Codex, and Claude. User instructions in the current session take precedence. Use the same configuration and commands regardless of agent.

## Architecture and ownership

- One shared GitHub OIDC provider per AWS account. Setup reuses an existing provider, including one created by another application.
- Per-site CloudFormation stacks: `static-site-NAME-certificate` and `static-site-NAME`.
- The bootstrap stack, if needed, is `static-sites-account`.
- All stacks use `us-east-1` so the CloudFront ACM certificate and S3 bucket work in one region.
- Resource names, account, repository, domain, and build settings come from `site.json`.
- AWS stack outputs are the source of truth for the bucket and distribution IDs; do not copy them into GitHub secrets or configuration.
- DNS stays with the existing provider. The setup command prints the required records.
- The site bucket and shared OIDC provider are retained if their stacks are deleted. There is no automated destroy command.
- This publishes multi-page static sites. Extensionless routes are directories with `index.html`; this is not an SPA fallback.

## One-time shared repository setup

1. Keep this repository private in the same GitHub organization as the private website repositories.
2. In repository Settings → Actions → General → Access, allow other repositories in the organization to use its workflows.
3. Publish a tested `v1` tag. The caller workflow refers to that tag.

The GitHub CLI equivalent of the access setting is:

```sh
gh api --method PUT repos/monotropiclabs/static-site-deploy/actions/permissions/access -f access_level=organization
```

## Add a website

1. Copy `site.example.json` to `site.json` at the root of the website repository. Set a unique name, canonical domain, redirect domains, exact GitHub repository and production branch, AWS account, build command, and build output directory. Names must be 1–24 lowercase letters, numbers, or hyphens, starting with a letter. Domain names must be lowercase and cannot contain wildcards.
2. The output directory must contain only the generated public site and an `index.html`. It must not be the repository root. Exclude `site.json`, agent instructions, dependency files, and other source/configuration files from the generator's output.
3. Add `.github/workflows/deploy.yml`:

```yaml
name: Deploy website
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
jobs:
  deploy:
    uses: monotropiclabs/static-site-deploy/.github/workflows/deploy.yml@v1
```

If production uses another branch, change both the trigger and `site.json`. An optional `with: {config: path/to/site.json}` selects a different configuration path. Commands and output paths remain relative to the website repository root in GitHub Actions; keep the configuration at the root when using local deploy.

4. Add an `AGENTS.md` link to this runbook. Have `CLAUDE.md` point to `AGENTS.md`. Preserve any instructions already present in those files.
5. Use a local AWS profile with provisioning rights in the intended account. Confirm the account before making changes. Do not copy AWS credentials into GitHub. The daily deployment role is created automatically and trusts only the configured repository and branch, with the STS audience. It accepts both GitHub's legacy subject and the immutable owner/repository ID subject format.
6. Run:

```sh
/path/to/static-site-deploy/site check --config /path/to/website/site.json
/path/to/static-site-deploy/site setup --config /path/to/website/site.json --profile PROFILE
```

7. On the first run, add the printed ACM validation CNAMEs at your DNS provider. These validate domain ownership without moving traffic. At Porkbun, remove the zone suffix from the record name when entering the Host field. For `_abc.example.com.`, use `_abc`; for `_abc.www.example.com.`, use `_abc.www`. Keep these CNAMEs for certificate renewal.
8. Run the same setup command again after validation. It applies the site stack and prints the CloudFront URL and website DNS targets.
9. Deploy once and inspect the CloudFront URL, homepage, nested pages, images, and metadata:

```sh
/path/to/static-site-deploy/site deploy --config /path/to/website/site.json --profile PROFILE
```

10. Switch website DNS after the preview works. At Porkbun, replace the apex's old A/ALIAS records with one ALIAS, Host blank, pointing to the CloudFront distribution hostname. Set each redirect subdomain's CNAME to the same hostname. Preserve unrelated MX/TXT and certificate-validation CNAMEs. Keep the old host available while DNS propagates.
11. Commit and push the website source and workflow. Observe the first GitHub Actions deployment. Local build output is not committed.

## Provisioning permissions

The local provisioning identity needs CloudFormation, the S3/CloudFront resources, ACM certificate access, and permission to manage the named deployment roles and GitHub OIDC provider. This is broader than the daily deployment role.

Generate a reviewable IAM policy for the configured AWS account:

```sh
./site policy --config /path/to/website/site.json > /tmp/static-sites-provisioning-policy.json
```

An account administrator can create a customer-managed policy from that JSON and attach it to the setup identity. The generated policy exceeds the IAM user inline-policy size limit, so use a managed policy for IAM users. IAM roles, buckets and stacks are restricted to the static-site naming prefixes. CloudFront distribution operations cover the account's distributions because IDs are assigned at creation; distribution/function creation and origin access control operations require wildcard scope. Certificate requests require the `ManagedBy=static-site-deploy` tag. Treat this as a trusted provisioning policy, not a runtime deployment policy. Existing permission boundaries, SCPs, or explicit denies may require administrator changes.

## Ordinary changes and deployments

Edit the website and push to the production branch. The shared workflow builds using `build_command`, verifies the output, assumes the per-site role, synchronizes the directory with `--delete`, invalidates `/*`, and waits for invalidation to finish. Concurrent deployments to the same site are serialized.

For a manual local deployment, use `./site deploy --config ... --profile ...`. Add `--skip-build` only when deploying an existing verified output directory. The command checks the AWS account before uploading.

Browser caching is 60 seconds; the CDN may cache for a day between deployments. Successful deployments invalidate the CDN. Changing the cache header in the deployment code requires re-uploading existing unchanged objects, because `s3 sync` does not update metadata for skipped files.

File sync is not an atomic release switch. For these small static sites, a brief mix of old and new files during upload is accepted. To roll back, revert the source commit and deploy again.

## Infrastructure changes and shared updates

Rerun `setup` after changing domains, the trusted repository/branch, or infrastructure templates. Certificate changes may require new DNS validation. Existing certificates are retained during replacement so an in-use certificate does not block the stack update.

For a shared update: run the checks, review the rendered CloudFormation template and IAM changes, commit, then publish the `v1` tag on a compatible revision. Changing the workflow tag does not apply CloudFormation changes to existing sites; rerun setup for each affected site. New configuration keys or incompatible behavior belong on `v2`.

Use `./site render --config ...` to inspect the exact site template, `./site outputs --config ... --profile ...` for deployed resource IDs, and `./site dns --config ... --profile ...` to print website routing records again.

## Troubleshooting

- **Wrong account:** choose the correct local profile or fix the account ID. The script refuses to deploy across accounts.
- **AccessDenied:** identify the exact missing permission in AWS's error. Fix the setup identity for infrastructure operations; fix the per-site role for upload/invalidation errors.
- **Setup returns validation records:** add the CNAMEs, keep existing traffic routing, and rerun setup after ACM issues the certificate.
- **Stack fails:** inspect `aws cloudformation describe-stack-events --stack-name NAME --region us-east-1 --profile PROFILE`. Do not delete unrelated stacks or recreate a retained bucket automatically.
- **Shared workflow cannot be found:** verify the `v1` tag and the shared repository's Actions access setting.
- **OIDC fails:** verify repository/branch, the STS audience, and whether the repository has a customized subject claim. A custom subject needs a matching trust policy; do not broaden it to all repositories. Do not add a GitHub environment to the deployment job without updating its subject restriction.
- **Nested page fails:** verify its built `directory/index.html` exists and the distribution uses the routing function. Do not enable an SPA fallback for this multi-page site.
- **New deployment looks old:** wait for the workflow's invalidation; a browser can retain an old file for up to 60 seconds.
- **Missing file returns 403:** the S3 origin is private. CloudFront may return S3's 403 for a nonexistent key; it does not return the homepage for missing pages.

## References

- [CloudFront OAC](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html)
- [CloudFront certificate region](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html)
- [GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
- [Shared private workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/share-with-your-organization)
