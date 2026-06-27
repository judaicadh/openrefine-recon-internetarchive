"""Offline tests for recon_internetarchive.py (IA API is mocked)."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import recon_internetarchive as ia  # noqa: E402

FAKE_SEARCH = {"response": {"docs": [
    {"identifier": "78_bublitchki_barry-sisters_1940", "title": "Bublitchki",
     "creator": ["Barry Sisters"], "year": 1940, "date": "1940-01-01",
     "collection": ["georgeblood", "78rpm"], "mediatype": "audio"},
    {"identifier": "some-other-thing", "title": "Bublik Songs of Russia",
     "creator": "Various", "year": 1955, "mediatype": "audio"},
]}}
FAKE_META = {
    "metadata": {"title": "Bublitchki", "creator": ["Barry Sisters"],
                 "date": "1940", "year": "1940",
                 "collection": ["georgeblood", "78rpm"],
                 "mediatype": "audio",
                 "licenseurl": "http://creativecommons.org/publicdomain/zero/1.0/"},
    "files": [
        {"name": "side-a.flac", "format": "Flac", "length": "165.2",
         "title": "Bublitchki"},
        {"name": "side-a.mp3", "format": "VBR MP3", "length": "165.2",
         "title": "Bublitchki", "track": "1"},
        {"name": "side-b.mp3", "format": "VBR MP3", "length": "172.0",
         "title": "Yidl Mitn Fidl", "track": "2"},
        {"name": "cover.jpg", "format": "JPEG"},
    ],
}


def fake_get(url, params=None):
    if "advancedsearch" in url:
        fake_get.last_q = params["q"]
        return FAKE_SEARCH
    return FAKE_META


@pytest.fixture()
def client():
    with patch.object(ia, "ia_get", side_effect=fake_get):
        ia._meta_cache.clear()
        yield ia.app.test_client()


def test_manifest_types_and_cors(client):
    r = client.get("/reconcile")
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    m = r.get_json()
    assert [t["id"] for t in m["defaultTypes"]][0] == "audio"
    assert m["suggest"]["property"]


def test_bound_properties_become_query_filters(client):
    queries = {"q0": {"query": "Bublitchki", "type": "audio",
               "properties": [{"pid": "creator", "v": "Barry Sisters"},
                              {"pid": "collection", "v": "georgeblood"}]}}
    r = client.post("/reconcile", data={"queries": json.dumps(queries)})
    assert "creator:" in fake_get.last_q and "Barry Sisters" in fake_get.last_q
    assert "collection:(georgeblood)" in fake_get.last_q
    assert "mediatype:(audio)" in fake_get.last_q
    results = r.get_json()["q0"]["result"]
    assert results[0]["id"] == "78_bublitchki_barry-sisters_1940"
    assert results[0]["match"] is True
    assert results[1]["match"] is False


def test_data_extension_prefers_mp3_files(client):
    extend = {"ids": ["78_bublitchki_barry-sisters_1940"],
              "properties": [{"id": "stream_url"}, {"id": "files_json"},
                             {"id": "license_url"}]}
    r = client.post("/reconcile", data={"extend": json.dumps(extend)})
    rows = r.get_json()["rows"]["78_bublitchki_barry-sisters_1940"]
    assert rows["stream_url"][0]["str"].endswith("/embed/78_bublitchki_barry-sisters_1940")
    files = json.loads(rows["files_json"][0]["str"])
    assert all("MP3" in f["format"] for f in files)
    assert [f["title"] for f in files] == ["Bublitchki", "Yidl Mitn Fidl"]
    assert "creativecommons" in rows["license_url"][0]["str"]


def test_lucene_sanitize_strips_operators():
    out = ia.lucene_sanitize('Songs (Vol. 2): "best" of A/B')
    for ch in '():"/':
        assert ch not in out


def test_property_suggest(client):
    r = client.get("/reconcile/suggest/property?prefix=coll").get_json()
    assert [p["id"] for p in r["result"]] == ["collection"]
