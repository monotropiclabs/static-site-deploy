# Static site deployments

Plain S3 and CloudFront hosting for Monotropic's static sites. CloudFormation owns the infrastructure; GitHub Actions runs each site's build, uploads its output, and invalidates the CDN.

The website repositories remain private. The S3 buckets block public access and CloudFront authenticates through Origin Access Control. Each website has its own bucket, distribution, and deployment role. Deployments can update that site's files and cache, but cannot modify infrastructure.

## Use it

See [RUNBOOK.md](RUNBOOK.md) for the complete setup, deployment, upgrade, and troubleshooting instructions. Codex and Claude use that same document through [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).

Each website contains:

- A `site.json` like [site.example.json](site.example.json).
- A short `.github/workflows/deploy.yml` calling the workflow in this repository at `@v1`.
- Its existing build tool; Jekyll, Astro, plain HTML, and other static generators can all publish a directory.

Local commands require Python 3, AWS CLI v2, and GitHub CLI. The website controls any build dependencies, such as Docker. Python uses only its standard library.

```sh
./site check --config /path/to/website/site.json
./site setup --config /path/to/website/site.json --profile tastebud
./site deploy --config /path/to/website/site.json --profile tastebud
```

Setup prints certificate-validation CNAMEs on its first run. Add those at the existing DNS provider and run setup again to create the site. Check the CloudFront preview after the first deployment, then update website DNS. Ordinary pushes to the configured branch deploy automatically.

## Layout

| File | Purpose |
| --- | --- |
| `cloudformation/account.json` | Shared GitHub OIDC provider, created only if none exists |
| `cloudformation/certificate.json` | Per-site ACM certificate in `us-east-1` |
| `cloudformation/site.json` | Bucket, CloudFront, routing function, and deployment role |
| `functions/router.js` | Directory indexes and canonical-domain redirects |
| `scripts/site.py` | Setup, configuration checks, rendering, DNS output, and manual deployment |
| `.github/workflows/deploy.yml` | Shared push-to-deploy workflow |

Compatible fixes are released on the `v1` tag. Breaking configuration or infrastructure changes require a new major tag; site repositories choose when to adopt it.

## Checks

```sh
python3 -m unittest discover -s tests -v
./site render --config site.example.json > /tmp/static-site-template.json
```

Routing tests also use Node.js. CI additionally validates the CloudFormation templates with `cfn-lint`.
