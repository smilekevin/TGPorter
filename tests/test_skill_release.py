"""Verify the release skill can be read without fetching project documentation."""

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

import yaml

from tools.build_skill import build_skill


ROOT = Path(__file__).resolve().parents[1]
TAG = "v1.2.3-rc.1"
COMMIT = "a" * 40
IMAGE = "ghcr.io/smilekevin/tgporter"
DIGEST = "sha256:" + "b" * 64


class SkillReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.archive = build_skill(ROOT, cls.directory.name, tag=TAG, commit=COMMIT, digest=DIGEST)
        with ZipFile(cls.archive) as package:
            cls.files = {name: package.read(name) for name in package.namelist()}

    def test_archive_contains_only_the_operational_handoff_files(self):
        self.assertEqual(self.archive.name, "TGPorterSkills.zip")
        self.assertEqual(set(self.files), {
            "tgporter/SKILL.md", "tgporter/references/deployment.md",
            "tgporter/references/release.md", "tgporter/release.json",
            "tgporter/references/project/DEPLOY_EN.md", "tgporter/references/project/LICENSE",
            "tgporter/references/project/config.yaml.example",
            "tgporter/references/project/docker-compose.yaml",
        })
        for name in self.files:
            self.assertEqual(PurePosixPath(name).parts[0], "tgporter")
            self.assertNotIn("..", PurePosixPath(name).parts)
            self.assertNotIn(PurePosixPath(name).name, {"config.yaml", "data.json", "openai.yaml"})

    def test_skill_and_general_documents_are_copied_byte_for_byte(self):
        for source, destination in {
            "skills/tgporter/SKILL.md": "tgporter/SKILL.md",
            "skills/tgporter/references/deployment.md": "tgporter/references/deployment.md",
            "DEPLOY_EN.md": "tgporter/references/project/DEPLOY_EN.md",
            "config.yaml.example": "tgporter/references/project/config.yaml.example",
            "LICENSE": "tgporter/references/project/LICENSE",
        }.items():
            self.assertEqual((ROOT / source).read_bytes(), self.files[destination], source)

    def test_release_metadata_records_exact_image_and_file_integrity(self):
        manifest = json.loads(self.files["tgporter/release.json"])
        self.assertEqual(manifest["tag"], TAG)
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual(manifest["image"], f"{IMAGE}:{TAG}")
        self.assertEqual(manifest["image_digest"], DIGEST)
        self.assertEqual(manifest["release_url"], f"https://github.com/smilekevin/TGPorter/releases/tag/{TAG}")
        self.assertEqual(manifest["download_url"],
                         f"https://github.com/smilekevin/TGPorter/releases/download/{TAG}/TGPorterSkills.zip")
        self.assertEqual(manifest["source_url"], f"https://github.com/smilekevin/TGPorter/archive/refs/tags/{TAG}.zip")
        self.assertEqual(manifest["config_url"], f"https://raw.githubusercontent.com/smilekevin/TGPorter/{TAG}/config.yaml.example")
        self.assertEqual(manifest["compose_url"], f"https://raw.githubusercontent.com/smilekevin/TGPorter/{TAG}/docker-compose.yaml")
        self.assertEqual(set(manifest["files"]), set(self.files) - {"tgporter/release.json"})
        for name, checksum in manifest["files"].items():
            self.assertEqual(hashlib.sha256(self.files[name]).hexdigest(), checksum, name)

    def test_all_relative_document_links_and_anchors_resolve_inside_zip(self):
        for name, data in self.files.items():
            if not name.endswith(".md"):
                continue
            text = re.sub(r"```.*?```", "", data.decode(), flags=re.S)
            targets = re.findall(r"\]\(([^\s)]+)", text)
            targets += re.findall(r'''\b(?:href|src)=["']([^"']+)''', text)
            for target in targets:
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
                    continue
                path = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path))) if parsed.path else name
                with self.subTest(source=name, target=target):
                    self.assertTrue(path in self.files or any(p.startswith(path + "/") for p in self.files))
                    if parsed.fragment and path.endswith(".md"):
                        headings = re.findall(r"^#{1,6}\s+(.+)$", self.files[path].decode(), re.M)
                        anchors, counts = set(), {}
                        for heading in headings:
                            slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                            count = counts.get(slug, 0)
                            counts[slug] = count + 1
                            anchors.add(slug if not count else f"{slug}-{count}")
                        self.assertIn(unquote(parsed.fragment), anchors)

    def test_generated_release_guide_uses_the_manifest_values(self):
        release = self.files["tgporter/references/release.md"].decode()
        manifest = json.loads(self.files["tgporter/release.json"])
        self.assertIn(f"docker pull {manifest['image']}", release)
        self.assertIn(f"docker pull {IMAGE}@{manifest['image_digest']}", release)
        self.assertIn(f"git clone --branch {manifest['tag']} --single-branch {manifest['repository_url']}", release)
        for key in ("release_url", "download_url", "source_url", "config_url", "compose_url"):
            self.assertIn(manifest[key], release, key)

    def test_compose_changes_only_the_service_image(self):
        expected = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())
        expected["services"]["tgporter"]["image"] = f"{IMAGE}:{TAG}"
        actual = yaml.safe_load(self.files["tgporter/references/project/docker-compose.yaml"])
        self.assertEqual(actual, expected)

    def test_release_build_does_not_depend_on_guide_headings_or_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in ("DEPLOY_EN.md", "LICENSE", "config.yaml.example", "docker-compose.yaml",
                         "skills/tgporter/SKILL.md", "skills/tgporter/references/deployment.md"):
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            guide = directory / "DEPLOY_EN.md"
            guide.write_text("# Renamed installation guide\n\n```bash\ndocker image pull --quiet example:edge\n```\n")
            setup = directory / "skills/tgporter/references/deployment.md"
            setup.write_text("# Setup notes without predefined section headings\n")
            archive = build_skill(directory, directory / "output", tag=TAG, commit=COMMIT)
            with ZipFile(archive) as package:
                self.assertEqual(package.read("tgporter/references/project/DEPLOY_EN.md"), guide.read_bytes())
                self.assertEqual(package.read("tgporter/references/deployment.md"), setup.read_bytes())
                self.assertIn(f"docker pull {IMAGE}:{TAG}", package.read("tgporter/references/release.md").decode())

    def test_skill_setup_uses_bundled_docs_and_runtime_uses_live_mcp(self):
        skill = self.files["tgporter/SKILL.md"].decode()
        setup = self.files["tgporter/references/deployment.md"].decode()
        self.assertIn("references/release.md", skill)
        self.assertIn("project/DEPLOY_EN.md#parameters", setup)
        self.assertIn("project/config.yaml.example", setup)
        self.assertIn("../release.json", setup)
        self.assertIn("Inspect the connected MCP definitions", skill)
        self.assertNotIn("openai.yaml", "\n".join(self.files))

    def test_both_language_parameter_references_cover_configurable_fields(self):
        keys = set(yaml.safe_load(self.files["tgporter/references/project/config.yaml.example"]))
        for node in ast.walk(ast.parse((ROOT / "module/app.py").read_text())):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == "_config" and isinstance(node.slice, ast.Constant)):
                keys.add(node.slice.value)
            if not isinstance(node, ast.Call):
                continue
            if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "_config" and node.func.attr == "get"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
            if (isinstance(node.func, ast.Name) and node.func.id == "get_config"
                    and len(node.args) > 1 and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "_config" and isinstance(node.args[1], ast.Constant)):
                keys.add(node.args[1].value)
        text = self.files["tgporter/references/project/DEPLOY_EN.md"].decode()
        for language, section in (("Chinese", (ROOT / "DEPLOY.md").read_text().split("### 配置参数说明", 1)[1]),
                                  ("English", text.split("### Parameters", 1)[1])):
            documented = set(re.findall(r"^\| `([^`]+)` \|", section, re.M))
            self.assertFalse(keys - documented, f"{language} undocumented keys: {keys - documented}")
            for field in ("chat[].chat_id", "chat[].download_filter", "chat[].last_read_message_id",
                          "chat[].upload_telegram_chat_id", "upload_drive.enable_upload_file",
                          "upload_drive.rclone_path", "upload_drive.remote_dir", "upload_drive.before_upload_file_zip",
                          "upload_drive.after_upload_file_delete", "upload_drive.upload_adapter"):
                self.assertIn(field, documented, language)

    def test_repeated_build_is_identical_and_does_not_edit_sources(self):
        original_archive = self.archive.read_bytes()
        inputs = [ROOT / "DEPLOY.md", ROOT / "DEPLOY_EN.md", ROOT / "README.md", ROOT / "docker-compose.yaml",
                  ROOT / "skills/tgporter/references/deployment.md"]
        original = {path: path.read_bytes() for path in inputs}
        build_skill(ROOT, self.directory.name, tag=TAG, commit=COMMIT, digest=DIGEST)
        self.assertEqual(self.archive.read_bytes(), original_archive)
        self.assertEqual({path: path.read_bytes() for path in inputs}, original)

    def test_cli_builds_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "tools/build_skill.py"), "--tag", TAG,
                 "--commit", COMMIT, "--output-dir", "output"],
                cwd=directory, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "output/TGPorterSkills.zip").is_file())

    def test_ci_preview_uses_sha_image_commit_urls_and_actions_download(self):
        run_url = "https://github.com/smilekevin/TGPorter/actions/runs/42"
        with tempfile.TemporaryDirectory() as directory:
            archive = build_skill(ROOT, directory, tag=f"sha-{COMMIT}", commit=COMMIT,
                                  digest=DIGEST, ci_run_url=run_url)
            with ZipFile(archive) as package:
                self.assertEqual(set(package.namelist()), set(self.files))
                manifest = json.loads(package.read("tgporter/release.json"))
                guide = package.read("tgporter/references/release.md").decode()
                compose = yaml.safe_load(package.read("tgporter/references/project/docker-compose.yaml"))
                for name, checksum in manifest["files"].items():
                    self.assertEqual(hashlib.sha256(package.read(name)).hexdigest(), checksum)
            self.assertEqual(manifest["kind"], "ci")
            self.assertIsNone(manifest["release_url"])
            self.assertEqual(manifest["ci_run_url"], run_url)
            self.assertEqual(manifest["download_url"], run_url + "#artifacts")
            self.assertEqual(manifest["source_url"], f"https://github.com/smilekevin/TGPorter/archive/{COMMIT}.zip")
            self.assertEqual(manifest["config_url"], f"https://raw.githubusercontent.com/smilekevin/TGPorter/{COMMIT}/config.yaml.example")
            self.assertEqual(manifest["compose_url"], f"https://raw.githubusercontent.com/smilekevin/TGPorter/{COMMIT}/docker-compose.yaml")
            self.assertEqual(manifest["image"], f"{IMAGE}:sha-{COMMIT}")
            self.assertEqual(manifest["image_digest"], DIGEST)
            self.assertEqual(compose["services"]["tgporter"]["image"], manifest["image"])
            self.assertIn("CI preview", guide)
            self.assertIn(f"docker pull {IMAGE}:sha-{COMMIT}", guide)
            self.assertIn(f"git -C TGPorter fetch origin {COMMIT}", guide)
            self.assertIn(f"git -C TGPorter checkout --detach {COMMIT}", guide)
            self.assertIn(run_url, guide)
            self.assertNotIn("/releases/", guide)
            self.assertNotIn("/refs/tags/", guide)
            self.assertNotIn("git clone --branch sha-", guide)

    def test_ci_preview_rejects_unpublished_images_mismatched_tags_and_run_urls(self):
        for overrides in (
            {"digest": None}, {"tag": TAG}, {"tag": "sha-" + "c" * 40},
            {"ci_run_url": "https://github.com/other/repo/actions/runs/42"},
            {"ci_run_url": "https://github.com/smilekevin/TGPorter/actions/runs/42?token=secret"},
        ):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                inputs = {"tag": f"sha-{COMMIT}", "commit": COMMIT, "digest": DIGEST,
                          "ci_run_url": "https://github.com/smilekevin/TGPorter/actions/runs/42", **overrides}
                with self.assertRaises(ValueError):
                    build_skill(ROOT, directory, **inputs)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_release_inputs_fail_before_writing_archive(self):
        for overrides in (
            {"tag": "latest"}, {"tag": "sha-abc"}, {"tag": "../v1"}, {"tag": "v" * 129},
            {"commit": "short"}, {"digest": "sha256:invalid"},
            {"repository": "owner/repo/extra"}, {"image": f"{IMAGE}:latest"},
        ):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                inputs = {"tag": TAG, "commit": COMMIT, "digest": DIGEST, **overrides}
                with self.assertRaises(ValueError):
                    build_skill(ROOT, directory, **inputs)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
