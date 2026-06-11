"""
Wikivoyage dump parser for Backpacker Index.

Reads the official English Wikivoyage dump (already downloaded to wikivoyage_dump/)
and produces:
  - candidate_destinations.jsonl: {page_id, title, slug, ns, is_redirect, status, type, parent_page_id, lat, lon, wikidata_qid, page_image_free, page_len}
  - article_categories.json: {page_id: [cat_name_lower, ...]}

Designed for streaming — no full dump load into RAM. Uses only standard library.

Run from project root:
    python3 wikivoyage_dump/parse_wikivoyage.py
"""
import bz2
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DUMP_DIR = Path(__file__).parent
OUT_DIR = DUMP_DIR

# ─────────────────────────────────────────────────────────────
# 1. Load page table (id, namespace, title, is_redirect, page_len)
# ─────────────────────────────────────────────────────────────
def load_pages():
    """Returns: dict[page_id] -> {title, ns, is_redirect, page_len}"""
    pages = {}
    # Schema: (page_id, page_namespace, page_title, page_is_redirect, page_is_new,
    #          page_random, page_touched, page_links_updated, page_latest, page_len, ...)
    # 7 groups: id, ns, title, is_redirect, is_new, page_latest, page_len
    pat = re.compile(
        r"\((\d+),(\d+),'((?:[^'\\]|\\.)*)',(\d),(\d),[^,]+,'[^']*','[^']*',(\d+),(\d+),"
    )
    with gzip.open(DUMP_DIR / 'enwikivoyage-latest-page.sql.gz', 'rt', encoding='utf-8', errors='ignore') as f:
        for line in f:
            for m in pat.finditer(line):
                pid = int(m.group(1))
                ns = int(m.group(2))
                if ns != 0:
                    continue
                title = m.group(3).replace('\\', '').replace('_', ' ')
                is_redirect = bool(int(m.group(4)))
                page_len = int(m.group(7))
                pages[pid] = {
                    'title': title,
                    'ns': ns,
                    'is_redirect': is_redirect,
                    'page_len': page_len,
                }
    return pages


# ─────────────────────────────────────────────────────────────
# 2. Load redirects (page_id -> target_title)
# ─────────────────────────────────────────────────────────────
def load_redirects():
    """Returns: dict[page_id] -> target_title (raw, with underscores)"""
    redirects = {}
    pat = re.compile(r"\((\d+),'[^']*','([^']+)',")
    with gzip.open(DUMP_DIR / 'enwikivoyage-latest-redirect.sql.gz', 'rt', encoding='utf-8', errors='ignore') as f:
        for line in f:
            for m in pat.finditer(line):
                redirects[int(m.group(1))] = m.group(2)
    return redirects


# ─────────────────────────────────────────────────────────────
# 3. Load categories (page_id -> [cat_name_lower])
# ─────────────────────────────────────────────────────────────
def load_categories():
    cats = defaultdict(set)
    pat = re.compile(r"\((\d+),'([^']+)',")
    with gzip.open(DUMP_DIR / 'enwikivoyage-latest-categorylinks.sql.gz', 'rt', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    for m in pat.finditer(text):
        cats[int(m.group(1))].add(m.group(2).lower())
    return dict(cats)


# ─────────────────────────────────────────────────────────────
# 4. Load geo_tags (page_id -> (lat, lon))
# ─────────────────────────────────────────────────────────────
def load_geo_tags():
    geo = {}
    # Schema: (gt_id, gt_page_id, gt_globe, gt_primary, lat, lon, ...)
    pat = re.compile(r"\(\d+,(\d+),'earth',\d+,([\-\d\.]+),([\-\d\.]+),")
    with gzip.open(DUMP_DIR / 'enwikivoyage-latest-geo_tags.sql.gz', 'rt', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    for m in pat.finditer(text):
        try:
            geo[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        except ValueError:
            pass
    return geo


# ─────────────────────────────────────────────────────────────
# 5. Load page_props (page_id -> {prop: value})
# ─────────────────────────────────────────────────────────────
def load_page_props():
    props = defaultdict(dict)
    pat = re.compile(r"\((\d+),'([^']+)','([^']*)',")
    with gzip.open(DUMP_DIR / 'enwikivoyage-latest-page_props.sql.gz', 'rt', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    for m in pat.finditer(text):
        page_id, k, v = int(m.group(1)), m.group(2), m.group(3)
        if k in ('wikibase_item', 'page_image_free', 'geocrumb-is-in', 'kartographer_links'):
            props[page_id][k] = v
    return dict(props)


# ─────────────────────────────────────────────────────────────
# 6. Stream articles XML, extract status template + plain intro
# ─────────────────────────────────────────────────────────────
# {{pagebanner|...}}, {{usable|...}}, {{star|...}}, {{outline|...}}, {{guide|...}}
STATUS_TPL = re.compile(
    r"\{\{\s*(?P<status>star|usable|outline|guide|usablecity|outlinestar|starcity|guidecity)"
    r"(?:\||\}\})",
    re.IGNORECASE,
)

def stream_article_status():
    """Yields (page_id, status_str) for every page that has a status template."""
    with bz2.open(DUMP_DIR / 'enwikivoyage-latest-pages-articles.xml.bz2', 'rb') as f:
        buf = b''
        for chunk in f:
            buf += chunk
            while b'</page>' in buf:
                page_xml, buf = buf.split(b'</page>', 1)
                pid_m = re.search(rb'<id>(\d+)</id>', page_xml)
                if not pid_m:
                    continue
                page_id = int(pid_m.group(1))
                # Find <text ...>...</text> (first 8KB is enough for the lead templates)
                text_m = re.search(rb'<text[^>]*>(.{0,8000})', page_xml, re.DOTALL)
                if not text_m:
                    continue
                head = text_m.group(1)
                # Find status template
                tpl_m = re.search(rb'\{\{\s*(star|usable|outline|guide)', head, re.IGNORECASE)
                if tpl_m:
                    yield (page_id, tpl_m.group(1).decode('utf-8', errors='ignore').lower())


# ─────────────────────────────────────────────────────────────
# 7. Main pipeline
# ─────────────────────────────────────────────────────────────
def slugify(title: str) -> str:
    """Convert Wikivoyage title to URL slug, like 'Ho Chi Minh City' -> 'ho-chi-minh-city'."""
    s = title.strip()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s.lower())
    return s


def main():
    print("=== Loading page table ===", file=sys.stderr)
    pages = load_pages()
    print(f"  {len(pages)} main-namespace pages", file=sys.stderr)

    print("=== Loading redirects ===", file=sys.stderr)
    redirects = load_redirects()
    print(f"  {len(redirects)} redirects", file=sys.stderr)

    print("=== Loading categories ===", file=sys.stderr)
    cats = load_categories()

    print("=== Loading geo_tags ===", file=sys.stderr)
    geo = load_geo_tags()
    print(f"  {len(geo)} pages with geo coords", file=sys.stderr)

    print("=== Loading page_props ===", file=sys.stderr)
    props = load_page_props()

    print("=== Streaming article status templates ===", file=sys.stderr)
    status_map = {}
    for page_id, status in stream_article_status():
        status_map[page_id] = status
    print(f"  {len(status_map)} articles with status templates", file=sys.stderr)

    # Build candidate destinations:
    # Wikivoyage article = main-namespace page that is NOT a redirect and
    # has a `geocrumb-is-in` parent (i.e. belongs to a hierarchy).
    # Optional filter: status template (star/usable/outline/guide) OR significant
    # page length (≥ 3KB) — that picks up high-quality city articles like
    # Bangkok that have no status banner.
    print("=== Building candidate destination set ===", file=sys.stderr)
    candidates = []
    for page_id, page in pages.items():
        if page['is_redirect']:
            continue
        title = page['title']
        # Skip Wikivoyage namespace, topic articles, etc. (already filtered by ns==0)
        # Skip if no parent crumb — those are top-level or orphan articles
        if 'geocrumb-is-in' not in props.get(page_id, {}):
            continue
        # Heuristic status: use template if present, else infer from page_len
        status = status_map.get(page_id)
        if status is None and page['page_len'] < 3000:
            continue  # Skip short articles without status template (likely stubs)
        if status is None:
            status = 'no-banner'  # High-quality article without status template
        slug = slugify(title)
        parent_id = int(props[page_id]['geocrumb-is-in'])
        wikidata_qid = props.get(page_id, {}).get('wikibase_item')
        image_filename = props.get(page_id, {}).get('page_image_free')
        lat, lon = geo.get(page_id, (None, None))
        candidates.append({
            'page_id': page_id,
            'title': title,
            'slug': slug,
            'page_len': page['page_len'],
            'status': status,
            'parent_page_id': parent_id,
            'wikidata_qid': wikidata_qid,
            'page_image_filename': image_filename,
            'latitude': lat,
            'longitude': lon,
            'categories': sorted(cats.get(page_id, [])),
        })

    # Sort by page_id for stable output
    candidates.sort(key=lambda c: c['page_id'])

    out = OUT_DIR / 'candidate_destinations.jsonl'
    with open(out, 'w', encoding='utf-8') as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + '\n')
    print(f"=== Wrote {len(candidates)} candidates to {out} ===", file=sys.stderr)

    # Status breakdown
    from collections import Counter
    by_status = Counter(c['status'] for c in candidates)
    by_parent = sum(1 for c in candidates if c['parent_page_id'] is not None)
    with_geo = sum(1 for c in candidates if c['latitude'] is not None)
    with_qid = sum(1 for c in candidates if c['wikidata_qid'])
    with_image = sum(1 for c in candidates if c['page_image_filename'])
    print(f"  By status: {dict(by_status)}", file=sys.stderr)
    print(f"  With parent crumb: {by_parent}", file=sys.stderr)
    print(f"  With geo coords: {with_geo}", file=sys.stderr)
    print(f"  With Wikidata QID: {with_qid}", file=sys.stderr)
    print(f"  With page image: {with_image}", file=sys.stderr)


if __name__ == '__main__':
    main()
