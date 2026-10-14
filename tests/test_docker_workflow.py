"""Exercise the publishing workflow's shell steps without Docker or GitHub."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/docker-build.yml"
IMAGE = "ghcr.io/smilekevin/hermes-telegram-downloader"
SHA = "a" * 40


class DockerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
        cls.steps = {
            step["id"]: step
            for step in cls.workflow["jobs"]["build"]["steps"]
            if "id" in step
        }

    def run_step(self, step_id, ref, force="false", image_exists=True,
                 release_published="false"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            docker_calls = root / "docker-calls"
            docker = root / "docker"
            docker.write_text(
                '#!/bin/bash\n'
                'printf "%s\\n" "$*" >> "$DOCKER_CALLS"\n'
                'exit "$DOCKER_EXIT"\n'
            )
            docker.chmod(0o755)
            env = {
                **os.environ,
                "IMAGE": IMAGE,
                "SHA": SHA,
                "GITHUB_REF": ref,
                "GITHUB_OUTPUT": str(output),
                "FORCE": force,
                "RELEASE_PUBLISHED": release_published,
                "DOCKER_CALLS": str(docker_calls),
                "DOCKER_EXIT": "0" if image_exists else "1",
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
            }
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                 self.steps[step_id]["run"]],
                env=env, text=True, capture_output=True, timeout=10,
            )
            return (
                result,
                output.read_text() if output.exists() else "",
                docker_calls.read_text() if docker_calls.exists() else "",
            )

    def image_tags(self, ref, release_published="false"):
        result, output, _ = self.run_step("tags", ref, release_published=release_published)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.split("tags<<EOF\n", 1)[1].split("\nEOF", 1)[0].splitlines()

    def test_trigger_has_release_published_without_schedule_or_tag_push(self):
        events = self.workflow["on"]
        self.assertNotIn("schedule", events)
        self.assertNotIn("tags", events["push"])
        self.assertEqual(events["push"]["branches"], ["master"])
        self.assertEqual(events["release"]["types"], ["published"])
        self.assertIn("workflow_dispatch", events)

    def test_builds_queue_without_replacing_pending_releases(self):
        self.assertEqual(self.workflow["concurrency"]["queue"], "max")
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")

    def test_master_publishes_only_sha(self):
        self.assertEqual(self.image_tags("refs/heads/master"),
                         [f"{IMAGE}:sha-{SHA}"])

    def test_branch_publishes_only_sha(self):
        self.assertEqual(self.image_tags("refs/heads/fix/example"), [f"{IMAGE}:sha-{SHA}"])

    def test_published_release_publishes_exact_version_sha_and_latest(self):
        for tag in ("v1.0.1", "v1.0.2-rc.1", "release_2026.10"):
            with self.subTest(tag=tag):
                self.assertEqual(self.image_tags(f"refs/tags/{tag}", release_published="true"),
                                 [f"{IMAGE}:sha-{SHA}", f"{IMAGE}:{tag}", f"{IMAGE}:latest"])

    def test_manual_tag_build_does_not_update_latest(self):
        self.assertEqual(self.image_tags("refs/tags/v1.0.1"),
                         [f"{IMAGE}:sha-{SHA}", f"{IMAGE}:v1.0.1"])

    def test_release_builds_even_when_same_commit_image_exists(self):
        for tag in ("v1.0.1", "v1.0.2"):
            with self.subTest(tag=tag):
                result, output, calls = self.run_step("check", f"refs/tags/{tag}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, "build=true\n")
                self.assertEqual(calls, "")

    def test_master_builds_even_when_same_commit_image_exists(self):
        result, output, calls = self.run_step("check", "refs/heads/master")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "build=true\n")
        self.assertEqual(calls, "")

    def test_reserved_and_invalid_tag_names_fail_before_publishing(self):
        for tag in ("latest", f"sha-{SHA}", "releases/v1.0.1", "-v1.0.1", "v" * 129):
            with self.subTest(tag=tag):
                result, _, _ = self.run_step("tags", f"refs/tags/{tag}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout + result.stderr)

    def test_existing_branch_image_can_skip_build(self):
        result, output, calls = self.run_step("check", "refs/heads/fix/example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "build=false\n")
        self.assertIn(f"imagetools inspect {IMAGE}:sha-{SHA}", calls)

    def test_manual_force_rebuild_does_not_inspect_existing_image(self):
        result, output, calls = self.run_step("check", "refs/heads/master", force="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "build=true\n")
        self.assertEqual(calls, "")

    def test_missing_image_builds(self):
        result, output, _ = self.run_step("check", "refs/heads/master", image_exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "build=true\n")


if __name__ == "__main__":
    unittest.main()
