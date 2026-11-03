"""The web UI's static files: what the Content-Security-Policy allows, files
that exist, and (with node installed) the JavaScript's syntax and pure
functions."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "hackrf_mcp" / "web" / "static"
PAGES = sorted(STATIC.glob("*.html"))
SCRIPTS = sorted((STATIC / "js").glob("*.js"))


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_pages_need_nothing_the_csp_forbids(page):
    html = page.read_text()
    # No inline script, inline style, style attribute or event handler.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert "<style" not in html
    assert not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+=", html)
    # Nothing from another origin (no CDN, no web fonts).
    assert not re.search(r"(src|href)=\"(https?:)?//", html)
    for ref in re.findall(r"(?:src|href)=\"(/static/[^\"]+)\"", html):
        assert (STATIC / ref[len("/static/"):]).is_file(), f"{page.name} refers to missing {ref}"


def test_stylesheets_and_scripts_stay_on_this_origin():
    for f in [*STATIC.glob("*.css"), *SCRIPTS]:
        text = f.read_text()
        assert not re.search(r"url\(\s*['\"]?(https?:)?//", text), f.name
        assert "@import" not in text, f.name
        assert "fonts.googleapis" not in text, f.name


@pytest.mark.parametrize("name", ["leaflet", "protomaps-leaflet"])
def test_vendored_libraries_are_the_published_releases(name):
    vendor = STATIC / "vendor" / name
    assert (vendor / "LICENSE").is_file()
    readme = (vendor / "README").read_text()
    files = [f for f in sorted(vendor.rglob("*")) if f.is_file() and f.name not in ("LICENSE", "README")]
    assert files
    for f in files:
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        assert f"{digest}  {f.relative_to(vendor)}" in readme, f.name
    for css in vendor.glob("*.css"):
        assert not re.search(r"url\(\s*['\"]?(https?:)?//", css.read_text())


def test_scripts_import_only_files_that_exist():
    for f in SCRIPTS:
        for ref in re.findall(r"from '(\.[^']+)'", f.read_text()):
            assert (f.parent / ref).resolve().is_file(), f"{f.name} imports missing {ref}"


def test_scripts_never_parse_text_as_html():
    # Subjects, labels and raw lines arrive over the air: they may only ever
    # become text nodes.
    for f in SCRIPTS:
        text = f.read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment"):
            assert sink not in text, f"{f.name} uses {sink}"


def test_scripts_name_no_protocol():
    # Profiles come from /api/registry; the UI must not hard-code them.
    for f in SCRIPTS:
        text = f.read_text().lower()
        for name in ("'adsb'", "'ais'", "'flex'", "'pocsag'", "'acars'", "'vdl2'", "'aprs'", "'eas'"):
            assert name not in text, f"{f.name} names {name}"


node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax(script):
    subprocess.run([node, "--check", str(script)], check=True)


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_pure_functions_with_node():
    test = Path(__file__).parent / "js" / "webui.test.mjs"
    r = subprocess.run([node, "--test", str(test)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
