"""
Wikivoyage wikitext parser — extracts structured fields from one article.

Currently a proof-of-concept focused on Bangkok. Shows what fields we can
extract without LLM assistance:

  - Lead paragraph
  - Section names and content (Understand, Get in, See, Do, Eat, Sleep, etc.)
  - Listing templates (see/do/buy/eat/drink/sleep/go) with structured params
  - Inline infobox data (currency, country, population, etc. via {{Infobox}})

Output: a single JSON file per article, written to /tmp/wikivoyage_parsed/.

This is the foundation for the bulk importer. Bulk version just wraps
this with a SAX-like XML stream over the multistream bz2 dump.
"""
import bz2
import json
import re
import sys
from pathlib import Path

DUMP = Path(__file__).resolve().parent
OUT_DIR = Path('/tmp/wikivoyage_parsed')

# ─────────────────────────────────────────────────────────────
# 1. Section splitter
# ─────────────────────────────────────────────────────────────
SECTION_RE = re.compile(r'^(=+)\s*(.+?)\s*=+\s*$', re.MULTILINE)


def split_sections(text):
    """Returns [(level, name, content), ...] for each heading section."""
    matches = list(SECTION_RE.finditer(text))
    sections = []
    for i, m in enumerate(matches):
        level = len(m.group(1))
        name = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        sections.append((level, name, content))
    return sections


# ─────────────────────────────────────────────────────────────
# 2. Listing template parser
# ─────────────────────────────────────────────────────────────
LISTING_KINDS = {
    'see': 'attraction', 'do': 'activity', 'buy': 'shopping',
    'eat': 'food', 'drink': 'drink', 'sleep': 'accommodation',
    'go': 'transit', 'listing': None,  # listing uses type= subfield
}

def find_listing_end(text: str, start: int) -> int:
    """Given a position right after '{{see|', find the matching '}}' that closes
    the listing template. Handles nested templates like {{IATA|DMK}} correctly."""
    depth = 2  # We're inside the outer {{
    pos = start
    while pos < len(text):
        c = text[pos]
        if c == '{' and pos + 1 < len(text) and text[pos + 1] == '{':
            depth += 1
            pos += 2
        elif c == '}' and pos + 1 < len(text) and text[pos + 1] == '}':
            depth -= 1
            pos += 2
            if depth == 0:
                return pos
        else:
            pos += 1
    return -1


def extract_listings(text: str) -> list:
    """Returns a list of listing dicts with a 'kind' field added."""
    listings = []
    pos = 0
    while pos < len(text):
        idx = text.lower().find('{{', pos)
        if idx == -1:
            break
        # Check if this is a listing template
        for kind in ('see', 'do', 'buy', 'eat', 'drink', 'sleep', 'go', 'listing'):
            tag = '{{' + kind + '|'
            tag_lower = tag.lower()
            chunk_lower = text[idx:idx + len(tag_lower)].lower()
            if chunk_lower == tag_lower:
                # Find body
                body_start = idx + len(tag)
                end = find_listing_end(text, idx)
                if end == -1:
                    pos = idx + 2
                    break
                body = text[body_start:end - 2]
                parsed = parse_listing_template(body)
                if kind == 'listing':
                    resolved_kind = parsed.get('type', 'other').lower()
                else:
                    resolved_kind = LISTING_KINDS.get(kind, kind)
                parsed['kind'] = resolved_kind
                listings.append(parsed)
                pos = end
                break
        else:
            pos = idx + 2
    return listings

PARAM_RE = re.compile(r'\|\s*([\w\s]+?)\s*=\s*([^\n|]+?)(?=\n\s*\||\n\s*$|\n\s*\}\})', re.MULTILINE)


def parse_listing_template(body: str) -> dict:
    """Parse one {{see|...}} block into structured fields."""
    out = {}
    # Split by lines starting with '|'
    for m in re.finditer(r'\|\s*(\w+)\s*=\s*([^\n]+?)(?=\n\s*\||\Z)', body, re.DOTALL):
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if val:
            out[key] = val
    # Cleanup: if 'name' has alt in it, split
    if 'name' in out and '|' in out['name']:
        out['name'] = out['name'].split('|')[0].strip()
    # Parse lat/long to floats
    if 'lat' in out:
        try:
            out['lat'] = float(out['lat'])
        except ValueError:
            pass
    if 'long' in out:
        try:
            out['long'] = float(out['long'])
        except ValueError:
            pass
    # Strip wiki markup from content
    if 'content' in out:
        out['content'] = re.sub(r"\[\[([^|\]]+?\|)?([^\]]+?)\]\]", r"\2", out['content'])
        out['content'] = re.sub(r"'''(.+?)'''", r"\1", out['content'])
        out['content'] = out['content'].strip()
    return out


# ─────────────────────────────────────────────────────────────
# 3. Climate chart extractor (for seasonal_weather table)
# ─────────────────────────────────────────────────────────────
CLIMATE_RE = re.compile(r'\{\{\s*climate chart\s*\|(.+?)\}\}', re.DOTALL | re.IGNORECASE)


def parse_climate_chart(block: str) -> list:
    """Parse {{climate chart}} table → list of 12 dicts with high/low/precip."""
    # Lines: | 1|22|13|... |
    lines = [l.strip().lstrip('|').strip() for l in block.split('\n') if l.strip().startswith('|')]
    months = []
    for line in lines:
        if line.lower().startswith(('float', 'clear')):
            continue
        parts = [p.strip() for p in line.split('|')]
        # First token may be a comment "<!--- name --->" which we skip
        parts = [p for p in parts if p and not p.startswith('<')]
        if len(parts) >= 3:
            try:
                high = float(parts[-3])
                low = float(parts[-2])
                precip = float(parts[-1])
                months.append({'month': len(months) + 1, 'high_temp_c': high, 'low_temp_c': low, 'precipitation_mm': precip})
            except ValueError:
                pass
    return months


# ─────────────────────────────────────────────────────────────
# 4. Run on Bangkok
# ─────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(DUMP / '_sample_bangkok.wikitext') as f:
        text = f.read()

    sections = split_sections(text)
    print(f"=== {len(sections)} top-level sections in Bangkok ===")
    for level, name, content in sections:
        if level == 2:
            word_count = len(content.split())
            print(f"  H2  {name:>22}  ({word_count:>4} words, {len(content):>6} chars)")

    listings = extract_listings(text)
    print(f"\n=== {len(listings)} listings extracted ===")
    from collections import Counter
    by_kind = Counter(l['kind'] for l in listings)
    for k, n in by_kind.most_common():
        print(f"  {k:>15}  {n}")

    # Climate chart
    climate_m = CLIMATE_RE.search(text)
    if climate_m:
        months = parse_climate_chart(climate_m.group(1))
        print(f"\n=== Climate chart: {len(months)} months ===")
        for m in months[:3]:
            print(f"  {m}")
        print('  ...')

    # Save full extraction
    out = {
        'title': 'Bangkok',
        'page_id': 2613,
        'sections': [{'level': l, 'name': n, 'word_count': len(c.split()), 'char_count': len(c)} for l, n, c in sections],
        'listings_by_kind': dict(by_kind),
        'total_listings': len(listings),
        'sample_listings': listings[:5],
        'climate_months': parse_climate_chart(climate_m.group(1)) if climate_m else [],
    }
    out_path = OUT_DIR / 'bangkok.json'
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n=== Wrote {out_path} ===")


if __name__ == '__main__':
    main()
