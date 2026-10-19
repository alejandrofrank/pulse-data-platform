"""Check the HTML's actual asset URLs against a deployed application."""

import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if path and path.startswith("/"):
            self.paths.add(path)


def check_assets(base):
    with urllib.request.urlopen(base + "/", timeout=15) as response:
        assert response.status == 200
        parser = AssetLinks()
        parser.feed(response.read().decode("utf-8"))
    assert parser.paths, "HTML did not reference any frontend assets"
    for path in sorted(parser.paths):
        with urllib.request.urlopen(urljoin(base, path), timeout=15) as response:
            assert response.status == 200 and response.read(), f"Unavailable asset: {path}"
