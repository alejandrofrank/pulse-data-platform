"""The chat must not combine new markup with stale browser-cached assets."""

import re

from fastapi.testclient import TestClient

from pulse_api import app as api


def asset_urls(response):
    return re.findall(r'(?:href|src)="(/agent-static/[^\"]+)"', response.text)


def test_chat_assets_are_fetchable_and_page_requires_revalidation():
    with TestClient(api.app) as client:
        response = client.get("/agents")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        urls = asset_urls(response)
        assert len(urls) == 2
        for url in urls:
            assert re.search(r"\?v=[0-9a-f]{16}$", url)
            assert client.get(url).status_code == 200


def test_changed_stylesheet_gets_a_new_url_but_unchanged_script_does_not(tmp_path, monkeypatch):
    for name in ("index.html", "style.css", "app.js"):
        (tmp_path / name).write_bytes((api.AGENT_STATIC / name).read_bytes())
    monkeypatch.setattr(api, "AGENT_STATIC", tmp_path)
    with TestClient(api.app) as client:
        before = asset_urls(client.get("/agents"))
        assert asset_urls(client.get("/agents")) == before
        with (tmp_path / "style.css").open("a", encoding="utf-8") as stylesheet:
            stylesheet.write("\n.history-panel { background: #161616; }\n")
        after = asset_urls(client.get("/agents"))
    assert after[0] != before[0]
    assert after[1] == before[1]
