"""Source-level guardrails that stop broken deployments from shipping.

These read files rather than importing the app, so they catch problems that
only appear once the code is packaged: a setting that is documented but never
reaches the container, a module missing from an image, or images published
without the tests having passed.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _documented_env_vars() -> set[str]:
    """Names of every setting .env.example documents (active or commented out)."""
    names: set[str] = set()
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"#?\s*([A-Z][A-Z0-9_]*)=", line)
        if match:
            names.add(match.group(1))
    return names


def test_every_documented_setting_is_forwarded_by_compose():
    # docker compose does NOT hand the whole .env to a container - only the
    # variables listed under `environment:`. A setting documented in
    # .env.example but missing there silently does nothing.
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    documented = _documented_env_vars()
    assert len(documented) > 20  # the parser found the file's contents
    missing = sorted(name for name in documented if not re.search(rf"\b{name}\b", compose))
    assert not missing, f"documented in .env.example but never passed to a container: {missing}"


def test_webui_image_contains_every_core_module_it_imports():
    # webui/Dockerfile copies core/ files one by one (not the whole package),
    # so a new `from core.x import ...` passes every test run from the repo
    # checkout yet crash-loops the published image with ModuleNotFoundError.
    dockerfile = (ROOT / "webui" / "Dockerfile").read_text(encoding="utf-8")
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        if line.startswith("COPY core/"):
            copied.update(line.split()[1:-1])  # drop "COPY" and the destination
    assert copied

    imported: set[str] = set()
    for path in (ROOT / "webui").rglob("*.py"):
        if "tests" in path.parts:
            continue
        imported.update(re.findall(r"(?m)^\s*(?:from|import)\s+core\.([A-Za-z_]+)", path.read_text(encoding="utf-8")))
    assert imported
    missing = sorted(m for m in imported if f"core/{m}.py" not in copied and f"core/{m}/" not in copied)
    assert not missing, f"imported by webui/ but not copied into its image: core.{missing}"


def test_images_are_only_published_after_the_tests_pass():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^  test:\s*$", workflow), "docker-publish.yml has no test job"
    assert "pytest" in workflow
    publish_job = workflow[workflow.index("  build-and-push:"):]
    assert re.search(r"(?m)^\s+needs:\s*test\s*$", publish_job), "the publish job does not wait for the tests"
