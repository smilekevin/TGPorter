"""Build a portable release or CI skill with version-matched documentation."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "smilekevin/TGPorter"
IMAGE = "ghcr.io/smilekevin/tgporter"
ARCHIVE_NAME = "TGPorterSkills.zip"
SKILL = PurePosixPath("skills/tgporter")
PACKAGE = PurePosixPath("tgporter")
PROJECT = PACKAGE / "references/project"
# Keep the handoff focused on operating the application, independently of future
# developer documents added to the repository.
PROJECT_FILES = (
    "DEPLOY_EN.md", "LICENSE", "config.yaml.example", "docker-compose.yaml",
)


def validate_release(tag, commit, repository, image, digest, ci_run_url=None):
    """Use the same tag constraints as the release image workflow."""
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)
            or tag == "latest" or (tag.startswith("sha-") and ci_run_url is None)):
        raise ValueError("Release tag must be a non-reserved Docker tag")
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit):
        raise ValueError("Source commit must be a full Git SHA")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be an owner/repository name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]+", image):
        raise ValueError("Image must be an untagged container repository")
    if digest is not None and not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Image digest must be a SHA-256 digest")
    if ci_run_url is not None:
        if tag != f"sha-{commit}" or digest is None:
            raise ValueError("CI bundles require the source SHA image tag and a published digest")
        if not re.fullmatch(rf"https://github\.com/{re.escape(repository)}/actions/runs/[0-9]+", ci_run_url):
            raise ValueError("CI run URL must belong to the source repository")


def release_metadata(tag, commit, repository, image, digest, ci_run_url=None):
    """Define release-specific values once for the manifest, guide and Compose."""
    revision = commit if ci_run_url else tag
    return {
        "kind": "ci" if ci_run_url else "release",
        "tag": tag, "commit": commit, "repository": repository,
        "image": f"{image}:{tag}", "image_digest": digest,
        "release_url": None if ci_run_url else f"https://github.com/{repository}/releases/tag/{tag}",
        "ci_run_url": ci_run_url,
        "download_url": (f"{ci_run_url}#artifacts" if ci_run_url else
                         f"https://github.com/{repository}/releases/download/{tag}/{ARCHIVE_NAME}"),
        "source_url": (f"https://github.com/{repository}/archive/{commit}.zip" if ci_run_url else
                       f"https://github.com/{repository}/archive/refs/tags/{tag}.zip"),
        "repository_url": f"https://github.com/{repository}.git",
        "config_url": f"https://raw.githubusercontent.com/{repository}/{revision}/config.yaml.example",
        "compose_url": f"https://raw.githubusercontent.com/{repository}/{revision}/docker-compose.yaml",
    }


def render_release_guide(release):
    """Render exact installation references without rewriting the general guide."""
    ci = release["kind"] == "ci"
    heading = "CI preview" if ci else release["tag"]
    origin = (f"- [CI run]({release['ci_run_url']})\n" if ci else
              f"- [Release]({release['release_url']})\n")
    notice = ("CI preview for verification; this is not a published Release.\n"
              "The image was published by this CI run. Artifact downloads require\n"
              "GitHub login and expire according to the run's retention policy.\n\n" if ci else "")
    checkout = (f"git clone {release['repository_url']} TGPorter\n"
                f"git -C TGPorter fetch origin {release['commit']}\n"
                f"git -C TGPorter checkout --detach {release['commit']}" if ci else
                f"git clone --branch {release['tag']} --single-branch {release['repository_url']}")
    text = (
        f"# TG Porter {heading}\n\n"
        f"{notice}"
        f"- Source commit: `{release['commit']}`\n"
        f"- Docker image: `{release['image']}`\n"
        f"{origin}"
        f"- [Skill ZIP]({release['download_url']})\n"
        f"- [Application source ZIP]({release['source_url']})\n"
        f"- [Versioned configuration download]({release['config_url']})\n"
        f"- [Source Compose template]({release['compose_url']})\n\n"
        "## Use this bundle\n\n"
        "Use the image and source references on this page for this bundle.\n"
        "The general deployment guide is copied unchanged and includes generic\n"
        "`latest` images and `main` URLs; use this page's exact values when\n"
        "following those examples. The bundled Compose file already selects\n"
        "the image above. A downloaded source Compose template still has a\n"
        "generic image; set its `services.tgporter.image` to the image\n"
        "above before using it. Do not upgrade an existing instance unless requested.\n\n"
        f"```bash\ndocker pull {release['image']}\n```\n\n"
    )
    if release["image_digest"]:
        repository = release["image"].rsplit(":", 1)[0]
        text += (
            "Pull the exact published image by digest:\n\n"
            f"```bash\ndocker pull {repository}@{release['image_digest']}\n```\n\n"
        )
    text += (
        "## Prepare deployment\n\n"
        "Read the bundled [deployment guide](project/DEPLOY_EN.md#prerequisites)\n"
        "and [configuration parameter reference](project/DEPLOY_EN.md#parameters).\n"
        "Copy the bundled [configuration example](project/config.yaml.example)\n"
        "and [Compose file](project/docker-compose.yaml) to the user's chosen\n"
        "deployment directory, preserving existing configuration and data.\n"
        "Set credentials, download paths and the actual proxy configuration\n"
        "before starting. Run these commands from that deployment directory:\n\n"
        "```bash\n"
        "docker compose pull tgporter\n"
        "docker compose run --rm tgporter\n"
        "```\n\n"
        "Complete interactive Telegram login, stop the foreground run, then\n"
        "start the service in the background:\n\n"
        "```bash\ndocker compose up -d tgporter\n```\n\n"
        "For a source installation or local image build, use the source ZIP\n"
        "linked above or check out the exact source:\n\n"
        f"```bash\n{checkout}\n```\n\n"
        f"For local image builds, pass `{release['tag']}` as `APP_VERSION`.\n"
        "Reading the bundled setup and parameter documentation needs no GitHub\n"
        "access. Application downloads, dependency installation and Telegram\n"
        "operations still need network access. Configure the user's own MCP\n"
        "instance URL and credentials, then inspect live MCP tool definitions.\n"
    )
    return text


def render_compose(content, image):
    """Update the service image structurally and retain all other settings."""
    try:
        compose = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ValueError("Invalid Compose source") from error
    if (not isinstance(compose, dict) or not isinstance(compose.get("services"), dict)
            or not isinstance(compose["services"].get("tgporter"), dict)):
        raise ValueError("Compose source must contain the tgporter service")
    compose["services"]["tgporter"]["image"] = image
    return yaml.safe_dump(compose, sort_keys=False, allow_unicode=True).encode("utf-8")


def build_skill(source_root, output_dir, *, tag, commit, repository=REPOSITORY,
                image=IMAGE, digest=None, ci_run_url=None):
    """Return a deterministic ZIP containing one importable skill directory."""
    validate_release(tag, commit, repository, image, digest, ci_run_url)
    release = release_metadata(tag, commit, repository, image, digest, ci_run_url)
    source_root = Path(source_root).resolve()
    sources = [source_root / name for name in PROJECT_FILES]
    sources.extend(p for p in (source_root / str(SKILL)).rglob("*") if p.is_file())
    for name in ("SKILL.md", "references/deployment.md"):
        if not (source_root / str(SKILL) / name).is_file():
            raise ValueError(f"Skill source is missing {name}")
    mapping = {}
    for path in sources:
        if path.is_symlink() or not path.resolve().is_relative_to(source_root):
            raise ValueError("Bundle inputs must be regular files inside the source checkout")
        relative = PurePosixPath(path.relative_to(source_root).as_posix())
        mapping[relative] = (PACKAGE / relative.relative_to(SKILL)
                             if relative.is_relative_to(SKILL) else PROJECT / relative)

    files = {}
    for source, destination in mapping.items():
        content = (source_root / str(source)).read_bytes()
        if source == PurePosixPath("docker-compose.yaml"):
            content = render_compose(content, release["image"])
        files[str(destination)] = content

    files[str(PACKAGE / "references/release.md")] = render_release_guide(release).encode("utf-8")
    manifest = {
        **release,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    files[str(PACKAGE / "release.json")] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for name, data in sorted(files.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, data)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--digest")
    parser.add_argument("--ci-run-url", help="Build a CI preview bound to its published SHA image")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        archive = build_skill(ROOT, **vars(args))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(archive)


if __name__ == "__main__":
    main()
