import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('site_deploy', ROOT / 'scripts/site.py')
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
CONFIG = {
    'name': 'example', 'domain': 'example.com', 'redirect_domains': ['www.example.com'],
    'github_repository': 'example/website', 'branch': 'main', 'aws_account_id': '123456789012',
    'aws_region': 'us-east-1', 'build_command': 'make build', 'output_directory': 'dist'
}


class ConfigTests(unittest.TestCase):
    def config(self, **updates):
        data = copy.deepcopy(CONFIG)
        data.update(updates)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'site.json'
            path.write_text(json.dumps(data))
            return site.read_config(path)

    def test_valid_config(self):
        self.assertEqual(self.config(), CONFIG)

    def test_dangerous_output_directories_rejected(self):
        for path in ['.', '', '../dist', '/tmp/dist', 'dist/../../', '.git', '.github/output']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.config(output_directory=path)

    def test_invalid_identities_and_domains_rejected(self):
        for update in [dict(name='../../other'), dict(aws_account_id='1234'), dict(domain='example.com;echo secret'), dict(domain='Example.com'), dict(redirect_domains=['example.com']), dict(github_repository='other'), dict(build_command='echo hi\naws s3 rm'), dict(aws_region='us-west-2')]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.config(**update)

    def test_source_directory_cannot_be_deployed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dist').mkdir()
            (root / 'dist/index.html').write_text('website')
            self.assertEqual(site.checked_output(CONFIG, root), root / 'dist')
            (root / 'dist/site.json').write_text('{}')
            with self.assertRaises(ValueError):
                site.checked_output(CONFIG, root)

    def test_symlinks_cannot_exfiltrate_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dist').mkdir()
            (root / 'dist/index.html').write_text('website')
            (root / 'dist/secret').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                site.checked_output(CONFIG, root)

    def test_infrastructure_is_private_and_deploy_role_is_scoped(self):
        template = site.rendered_template(CONFIG)
        resources = template['Resources']
        self.assertTrue(all(resources['Bucket']['Properties']['PublicAccessBlockConfiguration'].values()))
        role = resources['DeploymentRole']['Properties']
        trust = role['AssumeRolePolicyDocument']['Statement'][0]['Condition']['StringEquals']
        self.assertEqual(trust['token.actions.githubusercontent.com:aud'], 'sts.amazonaws.com')
        self.assertEqual(len(trust['token.actions.githubusercontent.com:sub']), 2)
        actions = [action for statement in role['Policies'][0]['PolicyDocument']['Statement'] for action in ([statement['Action']] if isinstance(statement['Action'], str) else statement['Action'])]
        self.assertNotIn('iam:*', actions)
        self.assertNotIn('cloudformation:UpdateStack', actions)
        self.assertIn('cloudfront:CreateInvalidation', actions)


class RouterTests(unittest.TestCase):
    def route(self, uri, host='example.com', query=None):
        code = site.rendered_template(CONFIG)['Resources']['Router']['Properties']['FunctionCode']
        event = {'request': {'uri': uri, 'headers': {'host': {'value': host}}, 'querystring': query or {}}}
        script = code + '\nprocess.stdout.write(JSON.stringify(handler(' + json.dumps(event) + ')));'
        return json.loads(subprocess.check_output(['node', '-e', script], text=True))

    def test_directory_indexes(self):
        self.assertEqual(self.route('/')['uri'], '/index.html')
        self.assertEqual(self.route('/philosophy/')['uri'], '/philosophy/index.html')

    def test_directory_redirect_and_assets(self):
        self.assertEqual(self.route('/philosophy')['headers']['location']['value'], '/philosophy/')
        self.assertEqual(self.route('/styles.css')['uri'], '/styles.css')
        self.assertEqual(self.route('/apps/logo.svg')['uri'], '/apps/logo.svg')

    def test_www_redirect_preserves_encoded_and_repeated_query(self):
        result = self.route('/philosophy', 'www.example.com', {'q': {'value': 'a%20b'}, 'tag': {'multiValue': [{'value': 'one'}, {'value': 'two'}]}})
        self.assertEqual(result['headers']['location']['value'], 'https://example.com/philosophy/?q=a%20b&tag=one&tag=two')
        self.assertEqual(result['statusCode'], 301)

    def test_cloudfront_preview_is_accessible(self):
        result = self.route('/philosophy/', 'd123.cloudfront.net')
        self.assertEqual(result['uri'], '/philosophy/index.html')
        self.assertNotIn('statusCode', result)


class CertificateTests(unittest.TestCase):
    def test_aws_creation_failure_is_reported_immediately(self):
        instance = site.Site(CONFIG)
        responses = [
            {'StackResourceDetail': {'ResourceStatus': 'DELETE_SKIPPED'}},
            {'StackEvents': [{'LogicalResourceId': 'Certificate', 'ResourceStatus': 'CREATE_FAILED', 'ResourceStatusReason': 'AccessDenied: acm:RequestCertificate'}]},
        ]
        with patch.object(instance, 'stack_info', return_value={'StackStatus': 'CREATE_IN_PROGRESS'}), \
             patch.object(instance, 'aws_json', side_effect=responses), \
             patch('time.sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'AccessDenied: acm:RequestCertificate'):
                instance.certificate()
            sleep.assert_not_called()

    def test_failed_stack_with_certificate_is_preserved(self):
        instance = site.Site(CONFIG)
        with patch.object(instance, 'stack_info', return_value={'StackStatus': 'ROLLBACK_COMPLETE'}), \
             patch.object(instance, 'aws_json', return_value={'StackResources': [{'PhysicalResourceId': 'arn:aws:acm:us-east-1:123456789012:certificate/existing'}]}), \
             patch.object(instance, 'aws') as mutate:
            with self.assertRaisesRegex(ValueError, 'still contains resources'):
                instance.certificate()
            mutate.assert_not_called()

    def test_empty_failed_stack_can_retry_and_print_validation(self):
        instance = site.Site(CONFIG)
        record = {'Name': '_abc.example.com.', 'Type': 'CNAME', 'Value': '_abc.acm-validations.aws.'}
        certificate = {'Status': 'PENDING_VALIDATION', 'SubjectAlternativeNames': ['example.com', 'www.example.com'], 'DomainValidationOptions': [{'ResourceRecord': record}]}
        responses = [
            {'StackResources': [{'ResourceStatus': 'DELETE_SKIPPED'}]},
            {'StackResourceDetail': {'ResourceStatus': 'CREATE_IN_PROGRESS', 'PhysicalResourceId': 'arn:aws:acm:us-east-1:123456789012:certificate/new'}},
            {'Certificate': certificate},
        ]
        with patch.object(instance, 'stack_info', return_value={'StackStatus': 'ROLLBACK_COMPLETE'}), \
             patch.object(instance, 'aws_json', side_effect=responses), \
             patch.object(instance, 'aws') as mutate:
            result, records = instance.certificate()
            self.assertEqual(result['Status'], 'PENDING_VALIDATION')
            self.assertEqual(records, [record])
            calls = [call.args[:2] for call in mutate.call_args_list]
            self.assertEqual(calls, [('cloudformation', 'delete-stack'), ('cloudformation', 'wait'), ('cloudformation', 'create-stack')])


if __name__ == '__main__':
    unittest.main()
