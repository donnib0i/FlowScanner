from fastapi.testclient import TestClient

import web.app as webapp

client = TestClient(webapp.app)


def test_favicon_is_a_real_icon():
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"


def test_touch_icon_is_a_real_png_under_both_names():
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon-32.png"):
        r = client.get(path)
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", path


def test_robots_keeps_crawlers_out_of_the_api():
    body = client.get("/robots.txt").text
    assert "Disallow: /api/" in body
