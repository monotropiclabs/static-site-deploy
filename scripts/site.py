#!/usr/bin/env python3
"""Manage static sites with the AWS CLI and Python's standard library."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CACHE_CONTROL = "public,max-age=60,s-maxage=86400"


class CommandError(RuntimeError):
    pass


def run(command, capture=True, cwd=None):
    result = subprocess.run(command, text=True, capture_output=capture, cwd=cwd)
    if result.returncode:
        raise CommandError((result.stderr or result.stdout or "Command failed").strip())
    return result.stdout.strip() if capture else ""


def read_config(path):
    config = json.loads(Path(path).read_text())
    required = {"name", "domain", "redirect_domains", "github_repository", "branch",
                "aws_account_id", "aws_region", "build_command", "output_directory"}
    if set(config) != required:
        raise ValueError(f"Configuration keys must be exactly: {', '.join(sorted(required))}")
    for key in required - {"redirect_domains"}:
        if not isinstance(config[key], str) or not config[key] or "\n" in config[key] or "\r" in config[key]:
            raise ValueError(f"{key} must be a nonempty single-line string")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", config["name"]):
        raise ValueError("name must be 1–24 lowercase letters, numbers or hyphens, starting with a letter")
    if not re.fullmatch(r"\d{12}", config["aws_account_id"]):
        raise ValueError("aws_account_id must have 12 digits")
    if config["aws_region"] != "us-east-1":
        raise ValueError("This single-region template uses us-east-1 for both S3 and the CloudFront certificate")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["github_repository"]):
        raise ValueError("github_repository must be owner/repository")
    if not isinstance(config["redirect_domains"], list):
        raise ValueError("redirect_domains must be an array")
    domains = [config["domain"], *config["redirect_domains"]]
    domain_pattern = r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if any(not isinstance(domain, str) or not re.fullmatch(domain_pattern, domain) for domain in domains):
        raise ValueError("Domains must be lowercase DNS hostnames without wildcards or trailing dots")
    if len(set(domains)) != len(domains):
        raise ValueError("Canonical and redirect domains must be distinct")
    output = Path(config["output_directory"])
    if output.is_absolute() or not output.parts or any(part in ("..", ".git", ".github") for part in output.parts):
        raise ValueError("output_directory must be a child directory, never the repository root")
    return config


def rendered_template(config):
    template = json.loads((ROOT / "cloudformation/site.json").read_text())
    code = (ROOT / "functions/router.js").read_text()
    code = code.replace("__CANONICAL_DOMAIN__", json.dumps(config["domain"]))
    code = code.replace("__REDIRECT_DOMAINS__", json.dumps(config["redirect_domains"]))
    template["Resources"]["Router"]["Properties"]["FunctionCode"] = code
    return template


def checked_output(config, site_root):
    output = site_root / config["output_directory"]
    resolved = output.resolve()
    if resolved == site_root.resolve() or site_root.resolve() not in resolved.parents:
        raise ValueError("Build output must stay inside the site repository")
    if not (output / "index.html").is_file():
        raise ValueError(f"Missing {output / 'index.html'}; build before deploying")
    if any(path.is_symlink() for path in output.rglob("*")) or output.is_symlink():
        raise ValueError("Build output must not contain symbolic links")
    if any((output / name).exists() for name in (".git", ".github", "site.json", "Gemfile", "Dockerfile")):
        raise ValueError("Build output contains source/configuration files")
    return output


class Site:
    def __init__(self, config, profile=None):
        self.config = config
        self.profile = profile
        self.stack = f"static-site-{config['name']}"

    def aws(self, *args):
        command = ["aws", "--region", self.config["aws_region"], "--no-cli-pager"]
        if self.profile:
            command += ["--profile", self.profile]
        return run([*command, *args])

    def aws_json(self, *args):
        return json.loads(self.aws(*args, "--output", "json"))

    def identity(self):
        account = self.aws_json("sts", "get-caller-identity")["Account"]
        if account != self.config["aws_account_id"]:
            raise ValueError(f"Wrong AWS account: expected {self.config['aws_account_id']}, got {account}")
        print(f"AWS account: {account}; region: {self.config['aws_region']}", flush=True)

    def stack_info(self, name):
        try:
            return self.aws_json("cloudformation", "describe-stacks", "--stack-name", name)["Stacks"][0]
        except CommandError as error:
            if "does not exist" in str(error):
                return None
            raise

    def deploy_stack(self, name, template, parameters=None):
        with tempfile.TemporaryDirectory(prefix="static-site-") as directory:
            path = Path(directory) / "template.json"
            path.write_text(json.dumps(template))
            args = ["cloudformation", "deploy", "--stack-name", name, "--template-file", str(path),
                    "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
                    "--tags", "ManagedBy=static-site-deploy"]
            if parameters:
                args += ["--parameter-overrides", *[f"{key}={value}" for key, value in parameters.items()]]
            print(f"Applying {name}…", flush=True)
            print(self.aws(*args), flush=True)

    def bootstrap(self):
        arn = f"arn:aws:iam::{self.config['aws_account_id']}:oidc-provider/token.actions.githubusercontent.com"
        providers = self.aws_json("iam", "list-open-id-connect-providers")["OpenIDConnectProviderList"]
        if any(item["Arn"] == arn for item in providers):
            provider = self.aws_json("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", arn)
            if "sts.amazonaws.com" not in provider["ClientIDList"]:
                raise ValueError("Existing GitHub OIDC provider lacks the sts.amazonaws.com audience; have its owner add it")
            print("Reusing the account's GitHub OIDC provider.", flush=True)
        else:
            self.deploy_stack("static-sites-account", json.loads((ROOT / "cloudformation/account.json").read_text()))

    def certificate(self):
        name = f"{self.stack}-certificate"
        parameters = {"DomainName": self.config["domain"], "RedirectDomains": ",".join(self.config["redirect_domains"])}
        values = [{"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()]
        existing = self.stack_info(name)
        template = str(ROOT / "cloudformation/certificate.json")
        if existing is None:
            self.aws("cloudformation", "create-stack", "--stack-name", name, "--template-body", f"file://{template}",
                     "--parameters", json.dumps(values), "--tags", "Key=ManagedBy,Value=static-site-deploy")
        elif existing["StackStatus"] not in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"):
            if existing["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
                raise ValueError(f"Certificate stack is {existing['StackStatus']}; inspect its events before retrying")
            try:
                self.aws("cloudformation", "update-stack", "--stack-name", name,
                         "--template-body", f"file://{template}", "--parameters", json.dumps(values))
            except CommandError as error:
                if "No updates are to be performed" not in str(error):
                    raise
        for attempt in range(15):
            try:
                resource = self.aws_json("cloudformation", "describe-stack-resource", "--stack-name", name,
                                         "--logical-resource-id", "Certificate")["StackResourceDetail"]
                if resource.get("PhysicalResourceId"):
                    certificate = self.aws_json("acm", "describe-certificate", "--certificate-arn", resource["PhysicalResourceId"])["Certificate"]
                    expected_domains = {self.config["domain"], *self.config["redirect_domains"]}
                    if set(certificate.get("SubjectAlternativeNames", [])) != expected_domains:
                        time.sleep(2)
                        continue
                    records = {item["ResourceRecord"]["Name"]: item["ResourceRecord"] for item in certificate.get("DomainValidationOptions", []) if "ResourceRecord" in item}
                    if certificate["Status"] == "ISSUED" or records:
                        return certificate, list(records.values())
            except CommandError as error:
                if not any(value in str(error) for value in ("does not exist", "ResourceNotFoundException")):
                    raise
            time.sleep(2)
        raise ValueError(f"AWS is still preparing {name}; rerun setup in a minute")

    def setup(self):
        self.identity()
        self.bootstrap()
        certificate, records = self.certificate()
        if certificate["Status"] != "ISSUED":
            print("\nAdd these certificate-validation records at your DNS provider:")
            for record in records:
                print(f"{record['Type']}  {record['Name']}  →  {record['Value']}")
            print("\nKeep the current website routing records. Rerun this setup command after DNS validation.")
            return
        name = f"{self.stack}-certificate"
        status = self.stack_info(name)["StackStatus"]
        if status.endswith("IN_PROGRESS"):
            waiter = "stack-create-complete" if status == "CREATE_IN_PROGRESS" else "stack-update-complete"
            self.aws("cloudformation", "wait", waiter, "--stack-name", name)
        repo = json.loads(run(["gh", "api", f"repos/{self.config['github_repository']}"]))
        if repo["full_name"].lower() != self.config["github_repository"].lower():
            raise ValueError("GitHub repository was renamed; update site.json before setup")
        owner, repo_name = self.config["github_repository"].split("/")
        parameters = {"SiteName": self.config["name"], "DomainName": self.config["domain"],
                      "RedirectDomains": ",".join(self.config["redirect_domains"]), "CertificateArn": certificate["CertificateArn"],
                      "GitHubRepository": self.config["github_repository"], "GitHubOwnerId": str(repo["owner"]["id"]),
                      "GitHubRepositoryId": str(repo["id"]), "GitHubBranch": self.config["branch"],
                      "GitHubOwner": owner, "GitHubRepoName": repo_name}
        self.deploy_stack(self.stack, rendered_template(self.config), parameters)
        self.dns()

    def outputs(self):
        info = self.stack_info(self.stack)
        if info is None or info["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"):
            raise ValueError("The site stack is not ready; run setup first")
        return {item["OutputKey"]: item["OutputValue"] for item in info.get("Outputs", [])}

    def dns(self):
        outputs = self.outputs()
        print(f"\nCloudFront preview: https://{outputs['DistributionDomain']}")
        print("After deploying and checking the preview, change website DNS:")
        print(f"ALIAS/ANAME  {self.config['domain']}  →  {outputs['DistributionDomain']}")
        for domain in self.config["redirect_domains"]:
            print(f"CNAME       {domain}  →  {outputs['DistributionDomain']}")
        print("At Porkbun, leave Host blank for the apex ALIAS. Keep the certificate-validation CNAMEs and MX/TXT records.")

    def deploy(self, site_root, skip_build=False):
        self.identity()
        outputs = self.outputs()
        if not skip_build:
            run(["bash", "-eo", "pipefail", "-c", self.config["build_command"]], capture=False, cwd=site_root)
        output = checked_output(self.config, site_root)
        print(f"Syncing {output} to s3://{outputs['BucketName']}/", flush=True)
        self.aws("s3", "sync", str(output) + "/", f"s3://{outputs['BucketName']}/", "--delete",
                 "--no-follow-symlinks", "--cache-control", CACHE_CONTROL, "--only-show-errors")
        invalidation = self.aws_json("cloudfront", "create-invalidation", "--distribution-id", outputs["DistributionId"], "--paths", "/*")
        self.aws("cloudfront", "wait", "invalidation-completed", "--distribution-id", outputs["DistributionId"], "--id", invalidation["Invalidation"]["Id"])
        print(f"Deployed {outputs['SiteUrl']}", flush=True)


def provisioning_policy(config):
    account = config["aws_account_id"]
    region = config["aws_region"]
    stack_arns = [f"arn:aws:cloudformation:{region}:{account}:stack/static-site-*/*", f"arn:aws:cloudformation:{region}:{account}:stack/static-sites-account/*"]
    def allow(actions, resources, **extra):
        return {"Effect": "Allow", "Action": actions, "Resource": resources, **extra}
    return {"Version": "2012-10-17", "Statement": [
        allow(["cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents", "cloudformation:DescribeStackResource", "cloudformation:DescribeStackResources", "cloudformation:ListStackResources", "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet", "cloudformation:GetTemplate"], stack_arns),
        allow(["cloudformation:ValidateTemplate", "cloudformation:GetTemplateSummary"], "*"),
        allow("iam:ListOpenIDConnectProviders", "*"),
        allow(["iam:CreateOpenIDConnectProvider", "iam:GetOpenIDConnectProvider", "iam:TagOpenIDConnectProvider"], f"arn:aws:iam::{account}:oidc-provider/token.actions.githubusercontent.com"),
        allow(["iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole", "iam:UpdateRoleDescription", "iam:UpdateAssumeRolePolicy", "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:TagRole", "iam:UntagRole"], f"arn:aws:iam::{account}:role/static-site-*-deploy"),
        allow("acm:RequestCertificate", "*", Condition={"StringEquals": {"aws:RequestTag/ManagedBy": "static-site-deploy"}}),
        allow("acm:AddTagsToCertificate", f"arn:aws:acm:{region}:{account}:certificate/*", Condition={"StringEquals": {"aws:RequestTag/ManagedBy": "static-site-deploy"}}),
        allow(["acm:DescribeCertificate", "acm:GetCertificate", "acm:ListTagsForCertificate", "acm:RemoveTagsFromCertificate", "acm:DeleteCertificate"], f"arn:aws:acm:{region}:{account}:certificate/*", Condition={"StringEquals": {"aws:ResourceTag/ManagedBy": "static-site-deploy"}}),
        allow(["s3:CreateBucket", "s3:DeleteBucket", "s3:GetBucket*", "s3:PutBucket*", "s3:DeleteBucketPolicy", "s3:GetEncryptionConfiguration", "s3:PutEncryptionConfiguration", "s3:ListBucket"], f"arn:aws:s3:::static-site-*-{account}-{region}"),
        allow(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], f"arn:aws:s3:::static-site-*-{account}-{region}/*"),
        allow(["cloudfront:CreateDistribution", "cloudfront:CreateFunction"], "*"),
        allow(["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution", "cloudfront:CreateInvalidation", "cloudfront:GetInvalidation", "cloudfront:TagResource", "cloudfront:UntagResource", "cloudfront:ListTagsForResource"], f"arn:aws:cloudfront::{account}:distribution/*"),
        allow(["cloudfront:DescribeFunction", "cloudfront:GetFunction", "cloudfront:UpdateFunction", "cloudfront:PublishFunction", "cloudfront:DeleteFunction", "cloudfront:ListTagsForResource", "cloudfront:TagResource", "cloudfront:UntagResource"], f"arn:aws:cloudfront::{account}:function/static-site-*"),
        allow(["cloudfront:CreateOriginAccessControl", "cloudfront:GetOriginAccessControl", "cloudfront:UpdateOriginAccessControl", "cloudfront:DeleteOriginAccessControl"], "*")
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "render", "policy", "setup", "outputs", "dns", "deploy"])
    parser.add_argument("--config", default="site.json", help="site.json in the website repository")
    parser.add_argument("--profile", help="local AWS CLI profile; omitted in GitHub Actions")
    parser.add_argument("--skip-build", action="store_true", help="deploy the existing build output")
    args = parser.parse_args()
    path = Path(args.config).resolve()
    try:
        config = read_config(path)
        site = Site(config, args.profile)
        if args.command == "check":
            print(f"Configuration valid: {config['name']} → {config['domain']}")
        elif args.command == "render":
            print(json.dumps(rendered_template(config), indent=2))
        elif args.command == "policy":
            print(json.dumps(provisioning_policy(config), indent=2))
        elif args.command == "outputs":
            site.identity()
            print(json.dumps(site.outputs(), indent=2))
        elif args.command == "dns":
            site.identity()
            site.dns()
        elif args.command == "setup":
            site.setup()
        elif args.command == "deploy":
            site.deploy(path.parent, args.skip_build)
    except (ValueError, OSError, CommandError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
