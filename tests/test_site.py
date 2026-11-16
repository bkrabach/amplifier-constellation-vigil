"""The product page builds, and the page, the README and the map agree with one record."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD = json.loads((ROOT / "docs/ecosystem-map.json").read_text())


def load_build():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "site/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_builds_with_every_local_reference_present(tmp_path: Path):
    site = load_build()
    site.build(tmp_path / "out")
    page = (tmp_path / "out/index.html").read_text()
    assert "@@" not in page and RECORD["summary"] in page
    nodes = {node["id"]: node["label"] for node in RECORD["nodes"]}
    for edge in RECORD["edges"]:
        assert f"{nodes[edge['from']]} <strong>{edge['relation']}</strong> {nodes[edge['to']]}" in page
    assert (tmp_path / "out/ecosystem/index.html").exists() and (tmp_path / "out/ecosystem/map.svg").exists()
    assert (tmp_path / "out/assets/social-card.png").exists() and (tmp_path / "out/.nojekyll").exists()


def test_a_missing_local_reference_fails_the_build(tmp_path: Path):
    site = load_build()
    try:
        site.validate(tmp_path, '<a href="nowhere.html">x</a>', tmp_path)
    except ValueError as error:
        assert "nowhere.html" in str(error)
    else:
        raise AssertionError("a broken link passed")
    try:
        site.validate(tmp_path, '<a href="#gone">x</a>', tmp_path)
    except ValueError as error:
        assert "#gone" in str(error)
    else:
        raise AssertionError("a missing anchor passed")


def test_readme_block_and_generated_views_match_the_record():
    readme = (ROOT / "README.md").read_text()
    block = readme[readme.index("<!-- ecosystem-map:start -->"):readme.index("<!-- ecosystem-map:end -->")]
    assert RECORD["summary"] in block and "Supports: " + ", ".join(RECORD["supports_roles"]) + "." in block
    site_url = RECORD["product_site"]["url"]
    for link in (f"[Product website]({site_url})", f"[Ecosystem map]({site_url}ecosystem/)", "[Open the map](docs/ecosystem-map.svg)", "[Map guide]("):
        assert link in block
    assert block.index("Product website") < block.index("Ecosystem map") < block.index("Open the map") < block.index("Map guide")
    assert RECORD["visibility"] == "public" and RECORD["repository"] == "bkrabach/amplifier-constellation-vigil"
    assert (ROOT / "docs/product/map.svg").read_bytes() == (ROOT / "docs/ecosystem-map.svg").read_bytes()
    assert RECORD["display_name"] in (ROOT / "docs/product/index.html").read_text()
