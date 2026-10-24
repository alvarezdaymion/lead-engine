import pytest

from lead_engine.html import decode_cfemail, parse_html

BASE = "https://shop.example.com/about"


def test_visible_text_links_and_title():
    p = parse_html("""<html><head><title>About | Shop</title><style>p{color:red}</style></head>
    <body><nav><a href="/team">Our team</a> <a href="https://elsewhere.example.org/x#top">Out</a></nav>
    <h1>About</h1><p>We fix   bikes.</p><script>var hidden = 1;</script><p>We fix bikes.</p></body></html>""", BASE)
    assert p.title == "About | Shop"
    assert p.lines == ["Our team Out", "About", "We fix bikes."]
    assert ("https://shop.example.com/team", "Our team") in p.links
    assert ("https://elsewhere.example.org/x", "Out") in p.links
    assert "hidden" not in p.text


def test_addresses_from_mailto_text_and_cloudflare_obfuscation():
    encoded = "42" + "".join("%02x" % (ord(c) ^ 0x42) for c in "pat@example.com")
    assert decode_cfemail(encoded) == "pat@example.com"
    p = parse_html('<p><a href="mailto:Dana.Whitlock@Example.com?subject=hi">Email Dana</a></p>'
                   "<p>Billing: billing@example.com.</p>"
                   '<p><span class="__cf_email__" data-cfemail="%s">[protected]</span></p>' % encoded, BASE)
    assert ("dana.whitlock@example.com", "Email Dana", "mailto") in p.emails
    assert ("billing@example.com", "Billing: billing@example.com.", "text") in p.emails
    assert any(e[0] == "pat@example.com" for e in p.emails)


def test_json_ld_is_parsed_and_graphs_are_flattened():
    p = parse_html("""<head><script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [{"@type": "LocalBusiness", "name": "Shop",
      "founder": {"@type": "Person", "name": "Pat Example"}}, {"@type": "WebSite", "name": "Shop"}]}
    </script><script type="application/ld+json">{not json}</script></head><body><p>Hi</p></body>""", BASE)
    types = [n.get("@type") for n in p.jsonld]
    assert "LocalBusiness" in types and "WebSite" in types


def test_escaped_json_ld_and_image_names_that_look_like_addresses():
    p = parse_html('<script type="application/ld+json">{&quot;@type&quot;: &quot;Person&quot;, '
                   '&quot;name&quot;: &quot;Pat Example&quot;}</script>'
                   '<script type="application/ld+json">{"@type": "Person", "name": "Say &quot;hi&quot;"}</script>'
                   "<p>Files: %s and pat@example.com</p>" % ("logo" + "@" + "2x.png"), BASE)
    assert [n["name"] for n in p.jsonld] == ["Pat Example", 'Say &quot;hi&quot;']
    assert [e[0] for e in p.emails] == ["pat@example.com"]


def test_an_unclosed_head_does_not_hide_the_body():
    p = parse_html("<html><head><title>T</title><body><p>Visible text</p></body></html>", BASE)
    assert "Visible text" in p.text


def test_broken_markup_still_yields_text():
    p = parse_html("<div><p>Open <b>tags <i>everywhere", BASE)
    assert "Open tags everywhere" in p.text


@pytest.mark.parametrize("address", [
    "pat@<span>example.com</span>",
    "<strong>pat</strong>&#64;example.com",
    "pa<span>t@ex</span>ample.com",
    "pat@ex<!-- formatting -->ample.com",
])
def test_inline_address_fragments_keep_every_occurrence_and_full_context(address):
    parsed = parse_html("<p>Email Pat Example: pat@example.com</p>"
                        "<p>Email Lee Sample: %s</p>" % address, BASE)
    assert parsed.emails == [("pat@example.com", "Email Pat Example: pat@example.com", "text"),
                             ("pat@example.com", "Email Lee Sample: pat@example.com", "text")]


def test_direct_parent_text_keeps_boundaries_headings_and_quotations():
    parsed = parse_html("<body>Intro.<section><h2>Our team</h2>Pat <b>Example</b>, Owner."
                        "<p>Lee Sample, CEO.</p>Pat Example retired.</section>"
                        "<blockquote>Pat Example, Owner.<p>Quoted biography.</p></blockquote></body>", BASE)
    assert parsed.elements == ["Intro.", "Pat Example, Owner.", "Lee Sample, CEO.",
                               "Pat Example retired.", "Quoted biography."]
    assert parsed.headings == ["Our team"]
    assert ("Pat Example, Owner.", ["Our team"]) in parsed.sections
    assert ("Pat Example retired.", ["Our team"]) in parsed.sections
    assert parsed.quotations == ["Pat Example, Owner. Quoted biography."]


def test_direct_text_never_joins_across_child_blocks():
    parsed = parse_html("<div>Pat Example<p>Lee Sample</p>, Owner.</div>"
                        "<div>Email Pat Example: pat@<p>example.com</p></div>", BASE)
    assert parsed.elements == ["Pat Example", "Lee Sample", ", Owner.", "Email Pat Example: pat@", "example.com"]
    assert parsed.emails == []


def test_repeated_addresses_in_one_element_keep_the_complete_text():
    parsed = parse_html("<p>Email Pat Example: pat@example.com<br>"
                        "Email Pat Example: pat@<b>example.com</b></p>", BASE)
    context = "Email Pat Example: pat@example.com Email Pat Example: pat@example.com"
    assert parsed.emails == [("pat@example.com", context, "text")] * 2


@pytest.mark.parametrize("tag", ["q", "cite"])
def test_inline_citations_preserve_the_complete_enclosing_claim(tag):
    parsed = parse_html("<p>Pat Example, Owner <%s>until 2020</%s>.</p>" % (tag, tag), BASE)
    assert parsed.elements == ["Pat Example, Owner until 2020."]
    assert parsed.quotations == (["until 2020"] if tag == "q" else [])
