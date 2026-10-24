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


def test_an_unclosed_head_does_not_hide_the_body():
    p = parse_html("<html><head><title>T</title><body><p>Visible text</p></body></html>", BASE)
    assert "Visible text" in p.text


def test_broken_markup_still_yields_text():
    p = parse_html("<div><p>Open <b>tags <i>everywhere", BASE)
    assert "Open tags everywhere" in p.text
