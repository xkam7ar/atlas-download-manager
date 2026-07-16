from __future__ import annotations

import pytest

from atlas.directory_explorer import DirectoryExplorerAction, directory_explorer_actions
from atlas.directory_index import (
    UnsupportedDirectoryIndexError,
    http_url_origin,
    same_http_origin,
    url_within_directory_scope,
)
from atlas.directory_parser import parse_directory_index
from atlas.directory_scanner import directory_scan_result_from_work_item
from atlas.directory_tree import DirectoryTree, selected_directory_roots
from atlas.models import HubKind, ScanStatus, WorkItem


def test_parse_apache_autoindex_rows_with_dates_sizes_and_parent() -> None:
    html = """
    <html><body><pre>
    <a href="../">Parent Directory</a>                             -
    <a href="cours/">cours/</a>             2023-12-23 06:50    -
    <a href="notes.txt">notes.txt</a>       2024-01-02 03:04   1.5K
    <a href="index.html">index.html</a>     2024-01-03 03:04   812
    </pre></body></html>
    """

    index = parse_directory_index("https://example.com/serveur/", html)

    assert [entry.name for entry in index.entries] == [
        "Parent Directory",
        "cours/",
        "notes.txt",
        "index.html",
    ]
    assert index.entries[0].parent is True
    assert index.folders[0].url == "https://example.com/serveur/cours/"
    assert index.folders[0].last_modified is not None
    assert index.files[0].visible_size == 1536
    assert index.files[0].kind == "file"
    assert index.files[1].kind == "html"


def test_parse_simple_href_list_as_directory_map() -> None:
    html = """
    <a href="_CODE/">_CODE/</a>
    <a href="_MACOS/">_MACOS/</a>
    <a href="LOCALSEND/LocalSend-1.15.apk">LocalSend apk</a>
    """

    index = parse_directory_index("https://downloads.example/root/", html)

    assert [entry.name for entry in index.folders] == ["_CODE/", "_MACOS/"]
    assert index.files[0].url == "https://downloads.example/root/LOCALSEND/LocalSend-1.15.apk"


def test_parse_copyparty_plain_text_index_strips_ansi_and_encodes_paths() -> None:
    listing = (
        "# acct: *\n"
        "# perms: ['read', 'get']\n"
        "# srvinf: public archive\n"
        "\x1b[0;7;36m20260626161231\x1b[0;33m 95.7G\x1b[0m "
        "\x1b[94m## Driver & recovery CDs/\n"
        "\x1b[0;7;36m20260626155850\x1b[0;36m 638B\x1b[0m README.md\n"
        "20260626155850 10B ../../escape.txt\n"
        "20260626155850 10B https://evil.example/file.txt\n"
    )

    index = parse_directory_index(
        "https://downloads.example/root",
        listing,
        content_type="text/plain; charset=utf-8",
    )

    assert index.parser_name == "copyparty-text"
    assert index.complete is True
    assert [entry.name for entry in index.folders] == ["Driver & recovery CDs/"]
    assert index.folders[0].url == ("https://downloads.example/root/Driver%20&%20recovery%20CDs/")
    assert index.files[0].url == "https://downloads.example/root/README.md"
    assert index.files[0].visible_size == 638
    assert index.files[0].last_modified is not None
    assert len(index.entries) == 2


def test_parse_current_copyparty_text_timestamp_and_grouped_byte_count() -> None:
    listing = (
        "# acct: *\n"
        "# perms: ['read', 'get']\n"
        "# srvinf: public archive\n"
        "2026-06-25 22:46:52      286,176,619  ## PICS/\n"
        "2026-06-26 16:12:31  102,853,117,698  Driver & recovery CDs/\n"
        "2026-06-26 15:58:50              638  README.md\n"
    )

    index = parse_directory_index(
        "https://downloads.example/root/",
        listing,
        content_type="text/plain; charset=utf-8",
    )

    assert [entry.name for entry in index.folders] == ["## PICS/", "Driver & recovery CDs/"]
    assert index.folders[0].url == "https://downloads.example/root/%23%23%20PICS/"
    assert index.folders[0].visible_size == 286_176_619
    assert index.folders[1].visible_size == 102_853_117_698
    assert index.files[0].visible_size == 638
    assert index.files[0].last_modified is not None


def test_parse_copyparty_html_omits_ui_and_zip_control_links() -> None:
    html = """
    <!doctype html><html id="ht_brw"><head>
      <link rel="stylesheet" href="/.cpr/w/browser.css">
    </head><body>
      <a href="?b=u">switch to basic browser</a>
      <a href="/">/</a>
      <a href="AV/?zip=crc">zip</a>
      <a href="AV/">AV/</a> 2026-06-26 16:18 288
      <a href="README.md">README.md</a> 2026-06-26 15:58 638
      <a href="?h">control-panel</a>
    </body></html>
    """

    index = parse_directory_index(
        "https://downloads.example/",
        html,
        content_type="text/html; charset=utf-8",
    )

    assert index.parser_name == "copyparty-html"
    assert [entry.name for entry in index.entries] == ["AV/", "README.md"]


def test_parse_plain_text_rejects_ambiguous_documents_explicitly() -> None:
    with pytest.raises(UnsupportedDirectoryIndexError, match="not a recognized"):
        parse_directory_index(
            "https://example.com/readme.txt",
            "This is an ordinary text document with a URL https://example.org/file.zip\n",
            content_type="text/plain",
        )


def test_parse_directory_index_marks_entry_limit_as_incomplete() -> None:
    html = "\n".join(f'<a href="file-{index}.txt">file {index}</a>' for index in range(2_001))

    index = parse_directory_index("https://example.com/files/", html)

    assert len(index.entries) == 2_000
    assert index.complete is False
    assert index.truncated_reason == "entry-limit"


def test_parse_html_ignores_inert_markup_and_rejects_unsafe_urls() -> None:
    html = """
    <!-- <a href="comment.zip">comment</a> -->
    <script>const link = '<a href="script.zip">script</a>';</script>
    <style>.x { content: '<a href="style.zip">style</a>'; }</style>
    <a href=" Java&#x53;cript:alert(1)">script scheme</a>
    <a href="data:text/plain,hello">data scheme</a>
    <a href="file:///tmp/secret">file scheme</a>
    <a href="https://user@example.com/root/private.zip">userinfo</a>
    <a href="./">self</a>
    <a href="report.zip?download=1#first">report</a>
    <a href="./report.zip?download=1#second">duplicate report</a>
    <a href="café.txt">café</a>
    """

    index = parse_directory_index("https://example.com/root/?view=details", html)

    assert [entry.name for entry in index.entries] == ["report", "café"]
    assert [entry.url for entry in index.entries] == [
        "https://example.com/root/report.zip?download=1",
        "https://example.com/root/caf%C3%A9.txt",
    ]


def test_directory_names_strip_terminal_controls_but_keep_literal_markup() -> None:
    html = """
    <a href="safe/">[link=https://evil.example]trusted[/link]</a>
    <a href="%1B%5D8%3B%3Bhttps%3A%2F%2Fevil.example%07name%1B%5D8%3B%3B%07/"></a>
    """

    index = parse_directory_index("https://example.com/root/", html)

    assert index.entries[0].name == "[link=https://evil.example]trusted[/link]/"
    assert index.entries[1].name == "name/"
    assert all("\x1b" not in entry.name and "\x07" not in entry.name for entry in index.entries)

    bidi = parse_directory_index(
        "https://example.com/root/",
        '<a href="safe.txt">safe\u202egnp.exe\u2069</a>',
    )
    assert bidi.entries[0].name == "safegnp.exe"


def test_parse_declared_charset_preserves_names_and_exact_large_sizes() -> None:
    html = '<a href="café">café</a> 9007199254740993B'.encode("iso-8859-1")

    index = parse_directory_index(
        "https://example.com/root/",
        html,
        content_type='text/html; charset="iso-8859-1"',
    )

    assert index.files[0].name == "café"
    assert index.files[0].url == "https://example.com/root/caf%C3%A9"
    assert index.files[0].kind == "file"
    assert index.files[0].visible_size == 9_007_199_254_740_993


def test_parse_scope_keeps_skipped_entries_but_only_exposes_safe_children() -> None:
    html = """
    <a href="inside.bin">inside</a>
    <a href="sub/../../sibling.bin">literal escape</a>
    <a href="%2e%2e/encoded.bin">encoded escape</a>
    <a href="/a/c/side.bin">sideways</a>
    <a href="//example.com/a/b/default.bin">default port</a>
    <a href="//example.com:444/a/b/other-port.bin">other port</a>
    <a href="//other.example/a/b/external.bin">external</a>
    """

    index = parse_directory_index("https://example.com:443/a/b/", html)
    entries = {entry.name: entry for entry in index.entries}

    assert [entry.name for entry in index.files] == ["inside", "default port"]
    assert entries["default port"].url == "https://example.com/a/b/default.bin"
    assert entries["literal escape"].skipped_reason == (
        "parent directory link skipped by no-parent policy"
    )
    assert entries["encoded escape"].skipped_reason == (
        "parent directory link skipped by no-parent policy"
    )
    assert entries["sideways"].skipped_reason == (
        "parent directory link skipped by no-parent policy"
    )
    assert entries["other port"].skipped_reason == "external link skipped by default"
    assert entries["external"].skipped_reason == "external link skipped by default"
    assert set(index.skipped) == {
        entries["literal escape"],
        entries["encoded escape"],
        entries["sideways"],
        entries["other port"],
        entries["external"],
    }


@pytest.mark.parametrize(
    "candidate",
    [
        "https://user@example.com/a/b/file.bin",
        "https://example.com/a/b/sub/%2Fetc",
        "https://example.com/a/b/sub/%255cetc",
        "https://example.com/a/b/x%00y",
        "https://example.com/a/b/../escape.bin",
    ],
)
def test_directory_scope_rejects_origins_and_paths_execution_would_refuse(
    candidate: str,
) -> None:
    assert not url_within_directory_scope("https://example.com/a/b/", candidate)


def test_http_origin_rejects_userinfo_and_normalizes_default_ports() -> None:
    assert http_url_origin("https://user@example.com/") is None
    assert not same_http_origin("https://example.com/", "https://user@example.com/")
    assert same_http_origin("https://example.com:443/a/", "https://example.com/a/b")


def test_parse_apache_table_skips_sort_headers_and_keeps_row_metadata() -> None:
    html = """
    <table>
      <tr><th><a href="?C=N;O=D">Name</a></th>
      <th><a href="?C=M;O=A">Last modified</a></th>
      <th><a href="?C=S;O=A">Size</a></th></tr>
      <tr><td><a href="/">Parent Directory</a></td><td>&nbsp;</td><td>-</td></tr>
      <tr><td><a href="cours/">cours/</a></td>
      <td align="right">2023-12-23 06:50</td><td align="right">-</td></tr>
      <tr><td><a href="readme.txt">readme.txt</a></td>
      <td align="right">2024-01-02 03:04</td><td align="right">1K</td></tr>
    </table>
    """

    index = parse_directory_index("https://perso.example/serveur/", html)

    assert [entry.name for entry in index.folders] == ["cours/"]
    assert [entry.name for entry in index.files] == ["readme.txt"]
    assert index.folders[0].last_modified is not None
    assert index.files[0].last_modified is not None
    assert index.files[0].visible_size == 1024


def test_parse_autoindex_filters_ampersand_sort_controls_and_split_iec_sizes() -> None:
    html = """
    <html><head><title>Index of /archive/</title></head><body>
    <table id="list"><thead><tr>
      <th><a href="?C=N&amp;O=A">File Name</a>
          <a href="?C=N&amp;O=D">&darr;</a></th>
      <th><a href="?C=S&amp;O=A">File Size</a>
          <a href="?C=S&amp;O=D">&darr;</a></th>
      <th><a href="?C=M&amp;O=A">Date</a>
          <a href="?C=M&amp;O=D">&darr;</a></th>
    </tr></thead><tbody>
      <tr><td><a href="README.txt">README.txt</a></td>
          <td>2.8 KiB</td><td>2022-Jan-20 08:52</td></tr>
      <tr><td><a href="archive.torrent">archive.torrent</a></td>
          <td>4.7 MiB</td><td>2022-Jan-22 03:47</td></tr>
      <tr><td><a href="bundle.zip">bundle.zip</a></td>
          <td>3.4 GiB</td><td>2022-Jan-22 03:47</td></tr>
    </tbody></table></body></html>
    """

    index = parse_directory_index("https://example.com/archive/", html)

    assert index.parser_name == "autoindex-html"
    assert [entry.name for entry in index.entries] == [
        "README.txt",
        "archive.torrent",
        "bundle.zip",
    ]
    assert [entry.visible_size for entry in index.files] == [
        2_867,
        4_928_307,
        3_650_722_201,
    ]
    assert index.files[0].last_modified is not None
    assert index.files[0].last_modified.isoformat() == "2022-01-20T08:52:00"


def test_parse_query_driven_folder_uses_query_value_as_name() -> None:
    html = """
    <html><head><title>AffWeb Files</title></head><body>
      <a href="index.php?dir=audio">
        <i class="material-icons-round folder-icon">folder</i>
        <span class="file-name">audio</span>
      </a>
    </body></html>
    """

    index = parse_directory_index("https://files.example/", html)

    assert [entry.name for entry in index.folders] == ["audio/"]
    assert index.folders[0].url == "https://files.example/index.php?dir=audio"


def test_out_of_scope_file_is_skipped_without_becoming_parent_directory() -> None:
    html = '<a href="../captions/episode.vtt">episode captions</a>'

    index = parse_directory_index("https://example.com/download/", html)

    assert index.entries[0].kind == "file"
    assert index.entries[0].parent is False
    assert index.entries[0].skipped_reason == ("parent directory link skipped by no-parent policy")


def test_directory_scan_result_keeps_failed_status_separate_from_empty_entries() -> None:
    scan = WorkItem(
        url="https://example.com/files/",
        host="example.com",
        scan_status=ScanStatus.failed,
        scan_type="failed scan",
        scan_errors=[
            {
                "code": "tls_cert_verify_failed",
                "message": "TLS certificate verification failed",
                "url": "https://example.com/files/",
                "recoverable": True,
            }
        ],
    )

    result = directory_scan_result_from_work_item(scan)

    assert result.status == ScanStatus.failed
    assert result.ok is False
    assert result.entries == ()
    assert result.errors[0].code == "tls_cert_verify_failed"


def test_directory_scan_result_keeps_external_and_out_of_scope_rows_skipped() -> None:
    scan = WorkItem(
        url="https://example.com/root/",
        final_url="https://example.com/root/",
        host="example.com",
        final_host="example.com",
        scan_status=ScanStatus.success,
        discovered_work_items=[
            WorkItem(
                url="https://example.com/root/file.bin",
                kind=HubKind.file,
                same_host=True,
            ),
            WorkItem(
                url="https://other.example/root/external.bin",
                kind=HubKind.file,
                same_host=False,
                external_host=True,
                error="external link skipped by default",
            ),
            WorkItem(
                url="https://example.com/sibling.bin",
                kind=HubKind.file,
                same_host=True,
                error="parent directory link skipped by no-parent policy",
            ),
        ],
    )

    result = directory_scan_result_from_work_item(scan)

    assert [entry.url for entry in result.files] == ["https://example.com/root/file.bin"]
    assert {entry.url for entry in result.skipped} == {
        "https://other.example/root/external.bin",
        "https://example.com/sibling.bin",
    }
    assert (
        next(entry.name for entry in result.skipped if entry.url.endswith("/sibling.bin"))
        == "sibling.bin"
    )


def test_directory_tree_selected_roots_normalizes_folder_names() -> None:
    html = """
    <a href="cours/">cours/</a>
    <a href="images/">images/</a>
    """
    index = parse_directory_index("https://example.com/serveur/", html)

    roots = selected_directory_roots(
        index.source_url,
        index.folders,
        ["cours", "missing/"],
    )

    assert roots == (
        "https://example.com/serveur/cours/",
        "https://example.com/serveur/missing/",
    )
    assert DirectoryTree.from_index(index).render_lines()[:2] == [
        "https://example.com/serveur/",
        "|-- cours/",
    ]


def test_directory_explorer_actions_are_state_driven() -> None:
    html = """
    <a href="cours/">cours/</a>
    <a href="notes.txt">notes.txt</a>
    """
    index = parse_directory_index("https://example.com/serveur/", html)

    assert directory_explorer_actions(index, status=ScanStatus.failed) == (
        DirectoryExplorerAction.back,
        DirectoryExplorerAction.quit,
    )
    assert DirectoryExplorerAction.visible_files in directory_explorer_actions(
        index,
        status=ScanStatus.success,
    )
