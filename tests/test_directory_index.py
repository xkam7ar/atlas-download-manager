from __future__ import annotations

from atlas.directory_explorer import DirectoryExplorerAction, directory_explorer_actions
from atlas.directory_parser import parse_directory_index
from atlas.directory_scanner import directory_scan_result_from_work_item
from atlas.directory_tree import DirectoryTree, selected_directory_roots
from atlas.models import ScanStatus, WorkItem


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
