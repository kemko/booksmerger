from __future__ import annotations

from bookmerger.references import ResourceMap, resolve_uri, rewrite_css, rewrite_xml


def test_resolves_percent_encoded_paths_fragments_and_base_uri() -> None:
    assert resolve_uri("OPS/text/a.xhtml", "../img/%D0%BA.png?x=1#part") == (
        "OPS/img/к.png",
        "x=1",
        "part",
    )
    assert resolve_uri("OPS/text/a.xhtml", "picture.png#x", "../images/") == (
        "OPS/images/picture.png",
        "",
        "x",
    )


def test_rewrites_xml_css_svg_and_keeps_external_and_data_uris() -> None:
    mapping = ResourceMap.under(
        ["OPS/text/chapter.xhtml", "OPS/images/к.png", "OPS/styles/a.css"], "books/0001"
    )
    xml = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><base href="../"/>'
        b'<link href="styles/a.css"/><style>p{background:url(images/%D0%BA.png)}'
        b'</style></head><body><img src="images/%D0%BA.png" '
        b'srcset="images/%D0%BA.png 1x, data:image/png;base64,a 2x"/>'
        b'<a href="https://example.test/x">x</a></body></html>'
    )
    result = rewrite_xml(xml, mapping, "OPS/text/chapter.xhtml")
    assert b"../images/%D0%BA.png" in result
    assert b"https://example.test/x" in result
    assert b"data:image/png;base64,a" in result
    css = rewrite_css(
        b'@import url("a.css"); p{background:url(../images/%D0%BA.png)}',
        mapping,
        "OPS/styles/a.css",
    )
    assert b"../images/%D0%BA.png" in css
