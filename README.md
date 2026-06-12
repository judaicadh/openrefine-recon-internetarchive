# openrefine-recon-internetarchive

An [OpenRefine reconciliation service](https://reconciliation-api.github.io/specs/latest/)
for the [Internet Archive](https://archive.org). No credentials required.

Built for [Shira](https://shira.wikibase.cloud) (Jewish & Yiddish LP recordings,
Penn Libraries Judaica DH) — IA's digitized 78s and LPs (e.g. the George Blood
collection) make it a rich source of *playable* matches — but generic for any
mediatype.

## Quick start

```bash
pip install -r requirements.txt
python recon_internetarchive.py
```

OpenRefine → Reconcile → Add standard service →
`http://localhost:8767/reconcile`

## Usage notes

- Types map to IA mediatypes: **audio** (default), texts, movies, image.
- Bind columns via **As property** (autocompletes): `creator`, `year`,
  `collection` (e.g. `georgeblood`), `language`, `mediatype`. These become
  real filters in the Lucene query sent to IA's advanced search.
- If strict filters return nothing, a loose free-text retry runs
  automatically.
- Data extension: `identifier`, `ia_url`, `creator`, `year`, `date`,
  `collections`, `mediatype`, `license_url`, `stream_url`
  (`https://archive.org/embed/<id>`), and `files_json` — per-track audio
  files with name/title/length/format (MP3 representations preferred),
  the raw material for track-level linking.
- `license_url` is surfaced deliberately: check it before embedding rather
  than merely linking.

## Tests

`pip install pytest && pytest` — fully offline, the IA API is mocked.

## License

MIT
