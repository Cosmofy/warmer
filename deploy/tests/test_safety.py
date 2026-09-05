"""Local-only deployment guard checks; never run SSH or application operations."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


class DeploymentSafetyTests(unittest.TestCase):
    def run_script(self, name, *args, env=None):
        # No inherited secrets, deployment flags, host or credentials.
        return subprocess.run(
            ["/bin/bash", str(DEPLOY / name), *args],
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", **(env or {})},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_shell_syntax(self):
        for name in ("release.sh", "remote-release.sh"):
            with self.subTest(script=name):
                result = subprocess.run(
                    ["/bin/bash", "-n", str(DEPLOY / name)],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_without_authority_fails_before_external_commands(self):
        result = self.run_script("release.sh", env={"PATH": "/nonexistent"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_push_event_cannot_deploy(self):
        result = self.run_script("release.sh", env={
            "PATH": "/nonexistent", "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "WARMER_DEPLOY_ENABLED": "true", "WARMER_BOOTSTRAPPED": "true",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_missing_bootstrap_cannot_deploy(self):
        result = self.run_script("release.sh", env={
            "PATH": "/nonexistent", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main", "WARMER_DEPLOY_ENABLED": "true",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_invalid_host_and_port_fail_before_external_commands(self):
        approved = {
            "PATH": "/nonexistent", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main", "WARMER_DEPLOY_ENABLED": "true",
            "WARMER_BOOTSTRAPPED": "true",
        }
        for host, port in (("", "29404"), ("-oProxyCommand=bad", "29404"),
                           ("a.example b.example", "29404"),
                           ("host.example", ""), ("host.example", "65536"),
                           ("host.example", "$(false)")):
            with self.subTest(host=host, port=port):
                result = self.run_script("release.sh", env={
                    **approved, "DEPLOY_HOST": host, "APP_PORT": port,
                })
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")

    def test_remote_invalid_inputs_fail_before_host_commands(self):
        for args in ((), ("bootstrap", "29404", "x"),
                     ("promote", "65536", "x"),
                     ("promote", "29404", "../../state.db")):
            with self.subTest(args=args):
                result = self.run_script("remote-release.sh", *args,
                                         env={"PATH": "/nonexistent"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")

    def test_unit_has_one_worker_external_state_and_optional_telemetry(self):
        text = (DEPLOY / "cosmofy-warmer.service").read_text()
        self.assertRegex(text, r"--host 127\.0\.0\.1 --port [0-9]+ --workers 1")
        self.assertIn("StateDirectory=cosmofy-warmer", text)
        self.assertIn("ReadWritePaths=/var/lib/cosmofy-warmer", text)
        self.assertIn("ConditionPathExists=/etc/cosmofy/warmer-bootstrapped", text)
        self.assertNotIn("cosmofy-warmer-otel.service", text)

    def test_app_env_has_external_state_and_no_provisioned_credentials(self):
        text = (DEPLOY / "warmer.env.example").read_text()
        self.assertIn("\nWARMER_API_TOKEN=\n", text)
        self.assertIn("\nSTELLATE_URL=\n", text)
        self.assertIn("WARMER_STATE_DB=/var/lib/cosmofy-warmer/state.db", text)
        self.assertIn("OTEL_SDK_DISABLED=true", text)
        self.assertNotIn("AWS_ACCESS_KEY_ID=", text)

    def test_ci_env_and_junit_paths_match_the_core_contract(self):
        text = (ROOT / ".github/workflows/tests.yml").read_text()
        token_line = next(line for line in text.splitlines()
                          if line.strip().startswith("WARMER_API_TOKEN:"))
        token = token_line.split(":", 1)[1].strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(token.isascii())
        self.assertFalse(any(character.isspace() for character in token))
        self.assertIn("STELLATE_URL: https://warmer-ci.stellate.sh", text)
        self.assertIn("WARMER_STATE_DB: ${{ runner.temp }}/warmer-ci-state.db", text)
        self.assertIn("mkdir -p test-results", text)
        self.assertIn("pytest -v --junitxml=test-results/pytest.xml", text)
        self.assertIn("path: test-results/pytest.xml", text)


if __name__ == "__main__":
    unittest.main()
