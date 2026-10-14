"""Exercise CI and Release publishing decisions and summaries without Docker."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/smilekevin/telegram-media-downloader"
REPOSITORY = "smilekevin/telegram-media-downloader"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


class DockerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {}
        cls.steps = {}
        for name, filename, job in (
            ("ci", "tests.yml", "docker"),
            ("release", "docker-build.yml", "build"),
        ):
            workflow = yaml.load(
                (ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader
            )
            cls.workflows[name] = workflow
            cls.steps[name] = {
                step["id"]: step for step in workflow["jobs"][job]["steps"] if "id" in step
            }

    def run_step(self, workflow, step_id, ref="refs/heads/master", event="push",
                 actor="smilekevin", head_repository=REPOSITORY, published="true",
                 digest=DIGEST, tags=None):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            summary = Path(directory) / "summary"
            env = {
                **os.environ,
                "IMAGE": self.workflows[workflow]["env"]["IMAGE"],
                "SHA": SHA,
                "GITHUB_REF": ref,
                "GITHUB_EVENT_NAME": event,
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_ACTOR": actor,
                "HEAD_REPOSITORY": head_repository,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "PUBLISHED": published,
                "DIGEST": digest,
                "TAGS": tags or f"{IMAGE}:sha-{SHA}",
            }
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                 self.steps[workflow][step_id]["run"]],
                env=env, text=True, capture_output=True, timeout=10,
            )
            return (result, output.read_text() if output.exists() else "",
                    summary.read_text() if summary.exists() else "")

    def image_tags(self, workflow, ref):
        result, output, _ = self.run_step(workflow, "tags", ref=ref)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output.split("tags<<EOF\n", 1)[1].split("\nEOF", 1)[0].splitlines()

    def test_release_workflow_runs_only_when_release_is_published(self):
        release = self.workflows["release"]
        self.assertEqual(release["name"], "Build Release Docker Image")
        self.assertEqual(release["on"], {"release": {"types": ["published"]}})

    def test_ci_handles_pr_master_push_and_manual_runs(self):
        events = self.workflows["ci"]["on"]
        self.assertEqual(set(events), {"pull_request", "push", "workflow_dispatch"})
        self.assertEqual(events["push"]["branches"], ["master"])

    def test_ci_publishing_waits_for_tests_and_uses_one_permission_decision(self):
        ci = self.workflows["ci"]
        docker = ci["jobs"]["docker"]
        self.assertEqual(ci["permissions"], {"contents": "read"})
        self.assertEqual(docker["needs"], "tests")
        self.assertEqual(docker["permissions"]["packages"], "write")
        publish_condition = "steps.publication.outputs.publish == 'true'"
        login = next(step for step in docker["steps"] if step.get("uses", "").startswith("docker/login-action"))
        self.assertEqual(login["if"], publish_condition)
        self.assertEqual(self.steps["ci"]["build"]["with"]["push"], "${{ " + publish_condition + " }}")

    def test_release_builds_queue_without_replacing_pending_releases(self):
        concurrency = self.workflows["release"]["concurrency"]
        self.assertEqual(concurrency["queue"], "max")
        self.assertEqual(concurrency["cancel-in-progress"], "false")

    def test_ci_publishes_only_sha_for_every_ref(self):
        for ref in ("refs/heads/master", "refs/pull/5/merge", "refs/heads/fix/example", "refs/tags/v1.0.1"):
            with self.subTest(ref=ref):
                self.assertEqual(self.image_tags("ci", ref), [f"{IMAGE}:sha-{SHA}"])

    def test_release_publishes_version_sha_and_latest(self):
        for tag in ("v1.0.1", "v1.0.2-rc.1", "release_2026.10"):
            with self.subTest(tag=tag):
                self.assertEqual(self.image_tags("release", f"refs/tags/{tag}"),
                                 [f"{IMAGE}:sha-{SHA}", f"{IMAGE}:{tag}", f"{IMAGE}:latest"])

    def test_release_rejects_invalid_reserved_and_non_tag_refs(self):
        refs = ["refs/heads/master"] + [f"refs/tags/{tag}" for tag in
                ("latest", f"sha-{SHA}", "releases/v1.0.1", "-v1.0.1", "v" * 129)]
        for ref in refs:
            with self.subTest(ref=ref):
                result, output, _ = self.run_step("release", "tags", ref=ref)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout + result.stderr)
                self.assertEqual(output, "")

    def test_different_releases_on_same_commit_still_publish_their_version(self):
        self.assertIn(f"{IMAGE}:v1.0.1", self.image_tags("release", "refs/tags/v1.0.1"))
        self.assertIn(f"{IMAGE}:v1.0.2", self.image_tags("release", "refs/tags/v1.0.2"))
        self.assertEqual(self.steps["release"]["build"]["with"]["push"], "true")
        self.assertNotIn("if", self.steps["release"]["build"])

    def test_ci_allows_own_pr_push_and_manual_publication(self):
        for event in ("pull_request", "push", "workflow_dispatch"):
            with self.subTest(event=event):
                result, output, _ = self.run_step("ci", "publication", event=event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, "publish=true\n")

    def test_ci_does_not_publish_fork_or_dependabot_prs(self):
        for actor, head in (("contributor", "contributor/fork"),
                            ("dependabot[bot]", REPOSITORY), ("contributor", "")):
            with self.subTest(actor=actor, head=head):
                result, output, _ = self.run_step("ci", "publication", event="pull_request",
                                                   actor=actor, head_repository=head)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, "publish=false\n")

    def test_ci_summary_contains_pull_command_compose_digest_and_sha_tag(self):
        result, _, summary = self.run_step("ci", "summary")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## CI image for verification", summary)
        self.assertIn(f"docker pull {IMAGE}@{DIGEST}", summary)
        self.assertIn(f"image: {IMAGE}@{DIGEST}", summary)
        self.assertIn(f"{IMAGE}:sha-{SHA}", summary)
        self.assertNotIn(f"{IMAGE}:latest", summary)

    def test_unpublished_ci_summary_does_not_claim_a_pullable_image(self):
        result, _, summary = self.run_step("ci", "summary", published="false", digest="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Image not published", summary)
        self.assertNotIn("docker pull", summary)
        self.assertNotIn("image:", summary)

    def test_release_summary_contains_version_latest_and_digest(self):
        tags = "\n".join(self.image_tags("release", "refs/tags/v1.0.1"))
        result, _, summary = self.run_step("release", "summary", tags=tags)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{IMAGE}:v1.0.1", summary)
        self.assertIn(f"{IMAGE}:latest", summary)
        self.assertIn(f"docker pull {IMAGE}@{DIGEST}", summary)
        self.assertIn(f"image: {IMAGE}@{DIGEST}", summary)

    def test_published_summary_fails_if_digest_is_missing(self):
        for workflow in ("ci", "release"):
            with self.subTest(workflow=workflow):
                result, _, summary = self.run_step(workflow, "summary", digest="")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("::error::", result.stdout + result.stderr)
                self.assertEqual(summary, "")


if __name__ == "__main__":
    unittest.main()
