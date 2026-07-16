from __future__ import annotations

import contextlib
import functools
import shutil
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from atlas.backends import SiteMirrorEngine
from atlas.models import DownloadAttrMode, SiteBackendChoice, SiteDownloadOptions


@pytest.mark.integration
def test_wget2_mirror_local_page_with_page_requisites_and_stats(tmp_path: Path) -> None:
    if not shutil.which("wget2"):
        pytest.skip("wget2 is not installed")

    site_root = tmp_path / "site"
    site_root.mkdir()
    (site_root / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="/style.css"></head>'
        '<body><img src="/logo.txt"></body></html>',
        encoding="utf-8",
    )
    (site_root / "style.css").write_text("body { background: url('/bg.txt'); }\n", encoding="utf-8")
    (site_root / "logo.txt").write_text("logo\n", encoding="utf-8")
    (site_root / "bg.txt").write_text("bg\n", encoding="utf-8")

    with _local_http_server(site_root) as base_url:
        output_dir = tmp_path / "mirror"
        result = SiteMirrorEngine().mirror(
            SiteDownloadOptions(
                url=f"{base_url}/index.html",
                output_dir=output_dir,
                backend=SiteBackendChoice.wget2,
                depth=1,
                page_requisites=True,
                convert_links=False,
                wait=0,
                stats=True,
            )
        )

    assert result.status == "success"
    assert any(path.name == "index.html" for path in output_dir.rglob("*"))
    assert any(path.name == "style.css" for path in output_dir.rglob("*"))
    assert result.ydl_opts is not None
    stats = result.ydl_opts["stats"]
    assert stats["summary"]["site"]["urls"] >= 1


@pytest.mark.integration
def test_wget2_mirror_parser_fidelity_for_html_and_css_requisites(tmp_path: Path) -> None:
    if not shutil.which("wget2"):
        pytest.skip("wget2 is not installed")

    site_root = tmp_path / "site"
    assets = site_root / "assets"
    css_dir = assets / "css"
    image_dir = assets / "images"
    css_dir.mkdir(parents=True)
    image_dir.mkdir()
    (site_root / "index.html").write_text(
        """
        <html>
          <head>
            <base href="/assets/">
            <link rel="stylesheet" href="css/main.css">
            <link rel="icon" href="favicon.txt">
            <link rel="preload" as="style" href="css/preload.css">
            <meta name="robots" content="index,follow">
          </head>
          <body>
            <picture>
              <source srcset="source-1x.txt 1x, source-2x.txt 2x">
              <img src="fallback.txt" alt="fallback">
            </picture>
            <img src="small.txt" srcset="small.txt 1x, large.txt 2x" alt="srcset">
            <a download href="download.txt">download</a>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    (css_dir / "main.css").write_text(
        '@import url("imported.css");\nbody { background: url("../images/bg.txt"); }\n',
        encoding="utf-8",
    )
    (css_dir / "preload.css").write_text("body { color: black; }\n", encoding="utf-8")
    (css_dir / "imported.css").write_text(".imported { display: block; }\n", encoding="utf-8")
    for name in (
        "favicon.txt",
        "fallback.txt",
        "source-1x.txt",
        "source-2x.txt",
        "small.txt",
        "large.txt",
        "download.txt",
    ):
        (assets / name).write_text(f"{name}\n", encoding="utf-8")
    (image_dir / "bg.txt").write_text("bg\n", encoding="utf-8")

    with _local_http_server(site_root) as base_url:
        output_dir = tmp_path / "mirror"
        result = SiteMirrorEngine().mirror(
            SiteDownloadOptions(
                url=f"{base_url}/index.html",
                output_dir=output_dir,
                backend=SiteBackendChoice.wget2,
                depth=2,
                page_requisites=True,
                convert_links=False,
                download_attr=DownloadAttrMode.strip_path,
                wait=0,
                stats=True,
            )
        )

    downloaded_names = {path.name for path in output_dir.rglob("*") if path.is_file()}
    assert result.status == "success"
    assert {
        "index.html",
        "main.css",
        "imported.css",
        "bg.txt",
        "favicon.txt",
        "preload.css",
        "small.txt",
        "large.txt",
        "source-1x.txt",
        "source-2x.txt",
        "fallback.txt",
        "download.txt",
    } <= downloaded_names


@contextlib.contextmanager
def _local_http_server(root: Path) -> Iterator[str]:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
