"""
recon_internetarchive.py — An OpenRefine reconciliation service for the
Internet Archive.

Reconcile album/recording titles against archive.org items, with supporting
columns bound as structured search filters:

    creator, year, collection, language, mediatype

No credentials required — IA's advanced search API is open.

Setup:
    pip install flask rapidfuzz requests
    python recon_internetarchive.py
    # OpenRefine -> Reconcile -> Add standard service ->
    #   http://localhost:8767/reconcile

Types:
    audio (default) — sound recordings (LPs, 78s, tapes)
    texts, movies, image — other IA mediatypes

Data extension (Add columns from reconciled values) offers:
    identifier, ia_url, creator, year, date, collections, mediatype,
    license_url, stream_url, files_json (per-track audio files with
    name/title/length/format — the track-level link source)

Items are identified by their IA identifier; clicking a match opens
https://archive.org/details/<identifier>.
"""

import json
import logging
import re
import threading
import time
import unicodedata

import requests as rq
from flask import Flask, request, jsonify
from rapidfuzz import fuzz

HOST = "0.0.0.0"
PORT = 8767
SERVICE_NAME = "Internet Archive"
USER_AGENT = "ShiraOpenRefineRecon/1.0 (Penn Libraries Judaica DH)"

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"

AUDIO_FORMATS = ("VBR MP3", "MP3", "Flac", "FLAC", "Ogg Vorbis", "WAVE",
                 "AIFF", "Apple Lossless Audio", "24bit Flac")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("recon")

app = Flask(__name__)
_meta_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# --- polite pacing ----------------------------------------------------------
_rate_lock = threading.Lock()
_last_request = [0.0]
MIN_INTERVAL = 0.6


def ia_get(url: str, params: dict | None = None) -> dict | None:
    with _rate_lock:
        wait = MIN_INTERVAL - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
    try:
        r = rq.get(url, params=params, headers={"User-Agent": USER_AGENT},
                   timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        log.exception("IA request FAILED: %s %s", url, params)
        return None


# --- normalization & scoring -------------------------------------------------

def normalize(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    for pat, rep in [
        (r"tsch|tsh|tch", "ch"),
        (r"sch", "sh"),
        (r"kh|ch(?=[aou])", "h"),
        (r"oi", "oy"),
        (r"ie", "i"),
        (r"w", "v"),
    ]:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def lucene_sanitize(s: str) -> str:
    """Strip characters with meaning in IA's Lucene-ish query syntax."""
    return re.sub(r'[+\-!(){}\[\]^"~*?:\\/]', " ", s).strip()


FILTER_PROPS = ["creator", "year", "collection", "language", "mediatype"]
FILTER_PROP_META = [
    {"id": "creator", "name": "Creator / artist"},
    {"id": "year", "name": "Year"},
    {"id": "collection", "name": "Collection (e.g. georgeblood)"},
    {"id": "language", "name": "Language (e.g. yiddish, yid)"},
    {"id": "mediatype", "name": "Mediatype (audio, texts, ...)"},
]


def prop_value(props: list, pid: str) -> str:
    for p in props or []:
        if p.get("pid") == pid:
            v = p.get("v")
            if isinstance(v, list):
                v = " ".join(str(x) for x in v)
            return str(v or "").strip()
    return ""


def as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def score_candidate(query_title: str, query_creator: str, doc: dict) -> float:
    title_score = fuzz.token_set_ratio(
        normalize(query_title), normalize(str(doc.get("title", ""))))
    if not query_creator:
        return title_score
    creators = " ".join(as_list(doc.get("creator")))
    creator_score = fuzz.token_set_ratio(
        normalize(query_creator), normalize(creators))
    return 0.65 * title_score + 0.35 * creator_score


# --- reconciliation -----------------------------------------------------------

def reconcile_one(q: dict) -> dict:
    query_title = q.get("query", "")
    limit = q.get("limit") or 5
    qtype = q.get("type") or "audio"
    if isinstance(qtype, list):
        qtype = qtype[0] if qtype else "audio"
    if isinstance(qtype, dict):
        qtype = qtype.get("id", "audio")

    bound = {pid: prop_value(q.get("properties"), pid) for pid in FILTER_PROPS}
    mediatype = bound["mediatype"] or qtype

    parts = [f'title:({lucene_sanitize(query_title)})']
    if mediatype:
        parts.append(f"mediatype:({lucene_sanitize(mediatype)})")
    if bound["creator"]:
        parts.append(f'creator:({lucene_sanitize(bound["creator"])})')
    if bound["year"]:
        parts.append(f'year:({lucene_sanitize(bound["year"])})')
    if bound["collection"]:
        parts.append(f'collection:({lucene_sanitize(bound["collection"])})')
    if bound["language"]:
        parts.append(f'language:({lucene_sanitize(bound["language"])})')

    params = {
        "q": " AND ".join(parts),
        "fl[]": ["identifier", "title", "creator", "year", "date",
                 "collection", "mediatype"],
        "rows": 10,
        "page": 1,
        "output": "json",
    }
    data = ia_get(SEARCH_URL, params)
    docs = ((data or {}).get("response") or {}).get("docs", [])
    log.info("search %r (filters %s) -> %d hits",
             query_title,
             {k: v for k, v in bound.items() if v} or "none",
             len(docs))

    # loose retry: title words + creator as free text, keep mediatype only
    if not docs and any(bound.values()):
        loose_q = lucene_sanitize(f"{bound['creator']} {query_title}")
        params["q"] = f"({loose_q})" + (
            f" AND mediatype:({lucene_sanitize(mediatype)})" if mediatype else "")
        data = ia_get(SEARCH_URL, params)
        docs = ((data or {}).get("response") or {}).get("docs", [])
        log.info("  loose retry -> %d hits", len(docs))

    results = []
    for doc in docs:
        ident = doc.get("identifier")
        if not ident:
            continue
        score = score_candidate(query_title, bound["creator"], doc)
        creators = ", ".join(as_list(doc.get("creator")))[:60]
        bits = [creators, str(doc.get("year") or doc.get("date") or "")[:10]]
        extra = " · ".join(b for b in bits if b)
        name = str(doc.get("title", ""))
        if extra:
            name = f"{name} ({extra})"
        results.append({
            "id": ident,
            "name": name,
            "type": [{"id": str(doc.get("mediatype", mediatype)),
                      "name": str(doc.get("mediatype", mediatype)).capitalize()}],
            "score": round(score, 1),
            "match": score >= 95,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    if results:
        log.info("  top: %.1f  %s", results[0]["score"], results[0]["name"])
    return {"result": results[:limit]}


# --- data extension -----------------------------------------------------------

EXTEND_PROPS = [
    {"id": "identifier", "name": "IA identifier"},
    {"id": "ia_url", "name": "Item URL"},
    {"id": "creator", "name": "Creator(s)"},
    {"id": "year", "name": "Year"},
    {"id": "date", "name": "Date"},
    {"id": "collections", "name": "Collection(s)"},
    {"id": "mediatype", "name": "Mediatype"},
    {"id": "license_url", "name": "License URL"},
    {"id": "stream_url", "name": "Stream/embed URL"},
    {"id": "files_json", "name": "Audio files (JSON: name/title/length/format)"},
]


def get_metadata_cached(identifier: str) -> dict | None:
    with _cache_lock:
        if identifier in _meta_cache:
            return _meta_cache[identifier]
    data = ia_get(METADATA_URL.format(identifier=identifier))
    if data:
        with _cache_lock:
            _meta_cache[identifier] = data
    return data


def audio_files(meta: dict) -> list[dict]:
    out = []
    for f in meta.get("files") or []:
        if f.get("format") in AUDIO_FORMATS:
            out.append({
                "name": f.get("name"),
                "title": f.get("title") or f.get("name"),
                "length": f.get("length"),
                "format": f.get("format"),
                "track": f.get("track"),
            })
    # prefer one playable representation per track: keep MP3s if present
    mp3s = [f for f in out if "MP3" in (f["format"] or "")]
    return mp3s or out


def extend_item(identifier: str, prop_ids: list[str]) -> dict:
    data = get_metadata_cached(identifier)
    if data is None:
        return {pid: [] for pid in prop_ids}
    md = data.get("metadata") or {}

    def s(v):
        return [{"str": str(v)}] if v not in (None, "", []) else []

    values: dict[str, list] = {}
    for pid in prop_ids:
        if pid == "identifier":
            values[pid] = s(identifier)
        elif pid == "ia_url":
            values[pid] = s(f"https://archive.org/details/{identifier}")
        elif pid == "creator":
            values[pid] = s(", ".join(as_list(md.get("creator"))))
        elif pid == "year":
            values[pid] = s(md.get("year"))
        elif pid == "date":
            values[pid] = s(md.get("date"))
        elif pid == "collections":
            values[pid] = s(", ".join(as_list(md.get("collection"))))
        elif pid == "mediatype":
            values[pid] = s(md.get("mediatype"))
        elif pid == "license_url":
            values[pid] = s(md.get("licenseurl"))
        elif pid == "stream_url":
            values[pid] = s(f"https://archive.org/embed/{identifier}")
        elif pid == "files_json":
            files = audio_files(data)
            values[pid] = s(json.dumps(files, ensure_ascii=False)) if files else []
        else:
            values[pid] = []
    return values


# --- flask routes --------------------------------------------------------------

MANIFEST = {
    "versions": ["0.2"],
    "name": SERVICE_NAME,
    "identifierSpace": "https://archive.org/details/",
    "schemaSpace": "https://archive.org/",
    "defaultTypes": [
        {"id": "audio", "name": "Audio"},
        {"id": "texts", "name": "Texts"},
        {"id": "movies", "name": "Movies"},
        {"id": "image", "name": "Image"},
    ],
    "view": {"url": "https://archive.org/details/{{id}}"},
    "preview": {
        "url": f"http://localhost:{PORT}/reconcile/preview?id={{{{id}}}}",
        "width": 430,
        "height": 130,
    },
    "suggest": {
        "property": {
            "service_url": f"http://localhost:{PORT}",
            "service_path": "/reconcile/suggest/property",
        },
    },
    "extend": {
        "propose_properties": {
            "service_url": f"http://localhost:{PORT}",
            "service_path": "/reconcile/propose_properties",
        },
        "property_settings": [],
    },
}


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def jsonp_or_json(payload):
    callback = request.values.get("callback")
    if callback:
        body = f"{callback}({json.dumps(payload)});"
        return app.response_class(body, mimetype="application/javascript")
    return jsonify(payload)


@app.route("/reconcile", methods=["GET", "POST", "OPTIONS"])
def reconcile():
    if request.method == "OPTIONS":
        return ("", 204)
    queries = request.values.get("queries")
    extend = request.values.get("extend")

    if queries:
        qs = json.loads(queries)
        return jsonp_or_json({key: reconcile_one(q) for key, q in qs.items()})

    if extend:
        payload = json.loads(extend)
        prop_ids = [p["id"] for p in payload.get("properties", [])]
        rows = {ident: extend_item(ident, prop_ids)
                for ident in payload.get("ids", [])}
        return jsonp_or_json({
            "meta": [p for p in EXTEND_PROPS if p["id"] in prop_ids],
            "rows": rows,
        })

    return jsonp_or_json(MANIFEST)


@app.route("/reconcile/suggest/property")
def suggest_property():
    prefix = (request.args.get("prefix") or "").lower()
    matches = [p for p in FILTER_PROP_META
               if prefix in p["id"] or prefix in p["name"].lower()]
    return jsonp_or_json({
        "code": "/api/status/ok", "status": "200 OK",
        "prefix": prefix,
        "result": matches or FILTER_PROP_META,
    })


@app.route("/reconcile/propose_properties")
def propose_properties():
    qtype = request.args.get("type", "audio")
    return jsonp_or_json({"type": qtype, "properties": EXTEND_PROPS})


@app.route("/reconcile/preview")
def preview():
    identifier = request.args.get("id", "")
    data = get_metadata_cached(identifier)
    if data is None:
        return "<html><body>Not found</body></html>"
    md = data.get("metadata") or {}
    creators = ", ".join(as_list(md.get("creator")))
    colls = ", ".join(as_list(md.get("collection"))[:3])
    n = len(audio_files(data))
    thumb = f"https://archive.org/services/img/{identifier}"
    return (
        "<html><body style='font-family:sans-serif;font-size:13px;display:flex;gap:10px'>"
        f"<img src='{thumb}' width='100' height='100' style='object-fit:cover'/>"
        f"<div><b>{md.get('title','')}</b><br/>{creators}<br/>"
        f"{md.get('date') or md.get('year') or ''} · {n} audio file(s)<br/>"
        f"<i>{colls}</i></div>"
        "</body></html>"
    )


if __name__ == "__main__":
    print(f"OpenRefine reconciliation endpoint: http://localhost:{PORT}/reconcile")
    app.run(host=HOST, port=PORT, threaded=True)
