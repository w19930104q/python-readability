# Source mapping format

The optional source mapping feature lets every non-whitespace character of an
extracted summary be traced back, character by character, to the original
parsed document. It is opt-in and does not change the summary that is selected.

## Enabling it

* **Python API** — construct the document with `source_mapping=True` and call
  `summary_with_sources()`, or call `summary_with_sources()` on any document
  (it transparently re-parses to establish a clean baseline):

  ```python
  from readability import Document

  doc = Document(html, source_mapping=True)
  payload = doc.summary_with_sources()
  ```

* **CLI** — pass `--json` (or `-j`):

  ```sh
  python -m readability --json page.html
  ```

  Without `--json` the CLI keeps its historical plain-text output.

## Baseline tree

All offsets and XPaths refer to the **baseline parse tree**: the tree obtained
from the input after HTML parsing, entity decoding, removal of hidden elements
and the initial lxml cleaning (`html_cleaner`, link absolutisation, …) but
**before** any readability transformation (unlikely-candidate removal,
`div` → `p` conversion, sibling merging, sanitizing, lenient retry, …).

When an `lxml` element/element-tree is passed as input, source mapping works on
a deep copy so the caller's DOM is never mutated. The historical shared-DOM
behavior is unchanged when mapping is disabled.

## Payload

`summary_with_sources()` returns a dict; `--json` prints the same structure
plus a `title` field:

```json
{
  "html": "<serialized summary HTML, identical to Document.summary()>",
  "text": "plain text concatenation with whitespace normalized",
  "sources": [
    {
      "start": 0,
      "end": 12,
      "source": {
        "xpath": "/html/body/div/p[1]",
        "slot": "text",
        "offset": 0
      }
    },
    { "start": 13, "end": 18, "source": null }
  ]
}
```

### `html`

The serialized summary. It is byte-identical to what `Document.summary()`
(or `summary(html_partial=...)`) returns with mapping disabled, including the
post-serialization attribute cleaning performed by `get_clean_html()`.

### `text`

Plain text built by walking the summary tree in document order and
concatenating every `text`/`tail` slot:

* for an element, the order is its own `text`, then each child recursively
  (a child's `tail` belongs immediately after that child), then the
  element's own `tail`;
* each maximal run of whitespace characters (spaces, tabs, newlines,
  `&nbsp;`, …) is collapsed to a single ASCII space;
* leading/trailing separators are trimmed.

### `sources`

A list of disjoint, ordered, half-open intervals `[start, end)` over Unicode
code point offsets in `text`. Every non-whitespace character of `text` is
covered by exactly one interval; the inserted space separators are not
covered.

Each interval either carries a source object or is explicitly source-less:

| Field    | Meaning |
|----------|---------|
| `start`  | first character offset in `text` (inclusive) |
| `end`    | one past the last character offset in `text` (exclusive) |
| `source` | `null` for synthetic text, otherwise the origin descriptor below |

The origin descriptor:

| Field    | Meaning |
|----------|---------|
| `xpath` | XPath of the baseline element owning the source slot |
| `slot`  | `"text"` for the element's own text, `"tail"` for the text following it |
| `offset` | Unicode code point offset of the interval's first character inside the baseline slot |

Adjacent characters that share the same origin descriptor are coalesced into
one interval. Because offsets are Unicode code point offsets, CJK characters
and emoji (including multi-code-point sequences such as family ZWJ sequences)
are indexed per code point, never per UTF-8/UTF-16 byte or code unit.

HTML entities are counted **after decoding**: for the source slot
`a&amp;b&#65;c`, the decoded text is `a&bAc`, so `&` sits at offset 1 and
`A` at offset 2.

## How provenance is tracked through transformations

The tracker records the origin of each text/tail slot as an ordered list of
"pieces" and updates it at every tree mutation:

* **dropping an element** — lxml migrates the dropped element's `tail` onto
  the previous sibling's `tail` (or the parent's `text`); the tracker moves
  the matching pieces to the destination slot;
* **`div` → `p` conversion** — text migrated into a synthesized `<p>` and
  tails wrapped into new paragraphs inherit the origin of the slot they came
  from;
* **sibling/segment merging and element moves** — elements keep their
  identity when re-parented, so their slots keep their recorded origin;
* **lenient retry** — the document is re-parsed and a fresh tracker is built
  from the new baseline, so stale state never leaks across attempts;
* **synthetic text** — the only text invented by the extractor is the
  `VIDEO` placeholder written into retained video iframes; it is marked with
  `"source": null`;
* **removed content** — deleted ads/comments and their descendants never
  appear in the summary and therefore never receive a source interval.

Identical text appearing in different elements is never conflated: each
occurrence keeps the XPath and offset of its own baseline slot.

## Round-trip example

```python
from lxml.html import document_fromstring
from readability import Document

payload = Document(html, source_mapping=True).summary_with_sources()
baseline = document_fromstring(html)

for item in payload["sources"]:
    if item["source"] is None:
        continue  # synthetic text, nothing to look up
    node = baseline.xpath(item["source"]["xpath"])[0]
    slot_value = getattr(node, item["source"]["slot"])
    length = item["end"] - item["start"]
    origin_fragment = slot_value[
        item["source"]["offset"]:item["source"]["offset"] + length
    ]
    assert origin_fragment == payload["text"][item["start"]:item["end"]]
```

Note that `document_fromstring(html)` here is a shorthand for illustration;
the precise baseline also applies hidden-element removal and the lxml
cleaner. The test suite builds the exact baseline in `tests/test_source_mapping.py`.
