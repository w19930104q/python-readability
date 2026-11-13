"""Optional source mapping for extracted summaries.

When :class:`~readability.readability.Document` is constructed with
``source_mapping=True`` (or when ``summary_with_sources()`` is called), every
piece of text kept in the summary can be traced back to the *baseline* parse
tree: the tree produced from the input right after HTML parsing and the
initial lxml cleaning, before any readability transformation runs.

A character's origin is described by:

* ``xpath`` -- XPath of the baseline element owning the text slot;
* ``slot``  -- ``"text"`` for the element's own text or ``"tail"`` for the
  text following the element;
* ``offset`` -- Unicode code point offset of the first character inside that
  slot.

Text synthesized by readability itself (currently the ``"VIDEO"`` placeholder
written into video iframes) has no source and is reported with ``source`` set
to ``None``.

The tracker is fully passive: when no tracker is attached every hook is a
no-op and the extraction pipeline behaves exactly as before.
"""

TEXT = "text"
TAIL = "tail"

# A piece is a 3-tuple (element, slot, length):
#   element: baseline element the characters came from, or None for synthetic
#   slot:    TEXT / TAIL, or None for synthetic pieces
#   length:  number of Unicode code points covered by the piece


class SourceTracker:
    """Records the provenance of text/tail slots through tree mutations."""

    def __init__(self, baseline):
        """Build origin information for every element in the baseline tree.

        XPath strings are computed eagerly because lxml paths refer to the
        tree an element currently lives in, and elements get moved between
        trees while the summary is assembled.
        """
        tree = baseline.getroottree()
        self._baseline = set()
        self._xpath = {}
        self._slots = {}  # type: dict[tuple, list]
        for elem in baseline.iter():
            self._baseline.add(elem)
            self._xpath[elem] = tree.getpath(elem)
            # Remember the decoded slot strings implicitly via their
            # elements: entities have already been decoded by the parser.
            if elem.text:
                self._slots.setdefault((elem, TEXT), None)
            if elem.tail:
                self._slots.setdefault((elem, TAIL), None)

    # -- slot provenance -------------------------------------------------

    def pieces(self, elem, slot):
        """Return the ordered origin pieces of an element's text/tail slot."""
        key = (elem, slot)
        if key in self._slots:
            recorded = self._slots[key]
            if recorded is not None:
                return recorded
            # Baseline slot never rewritten: it is its own origin.
            value = getattr(elem, slot)
            if value:
                return [(elem, slot, len(value))]
            return []
        value = getattr(elem, slot)
        if not value:
            return []
        if elem in self._baseline:
            return [(elem, slot, len(value))]
        # An element introduced by the pipeline and never assigned tracked
        # content can only hold synthetic text.
        return [(None, None, len(value))]

    def assign(self, elem, slot, pieces):
        """Attach provenance pieces to a slot that is about to be written."""
        if pieces:
            self._slots[(elem, slot)] = pieces
        else:
            self._slots.pop((elem, slot), None)

    def copy_slot(self, destination, dest_slot, source, source_slot):
        """Move provenance from one slot to another (whole-slot copy)."""
        self.assign(
            destination, dest_slot, self.pieces(source, source_slot)
        )

    def mark_synthetic(self, elem, slot):
        """Mark a slot as text invented by the pipeline (no source)."""
        value = getattr(elem, slot)
        if value:
            self._slots[(elem, slot)] = [(None, None, len(value))]
        else:
            self._slots.pop((elem, slot), None)

    def clear_slot(self, elem, slot):
        """Forget provenance for a slot that is being emptied."""
        self._slots.pop((elem, slot), None)

    # -- tree mutations --------------------------------------------------

    def drop(self, elem):
        """Record an element removal *before* ``elem.drop_tree()`` runs.

        Mirrors lxml's own tail migration: the dropped element's tail is
        concatenated onto the previous sibling's tail (or the parent's text
        when there is no previous sibling).
        """
        tail = elem.tail
        if not tail:
            return
        moved = self.pieces(elem, TAIL)
        parent = elem.getparent()
        previous = elem.getprevious()
        target = previous if previous is not None else parent
        if target is None:
            return
        target_slot = TAIL if previous is not None else TEXT
        existing = target.text if target_slot == TEXT else target.tail
        combined = []
        if existing:
            combined.extend(self.pieces(target, target_slot))
        combined.extend(moved)
        self.assign(target, target_slot, combined)

    # -- output helpers --------------------------------------------------

    def describe(self, elem, slot, offset):
        """Resolve a piece-relative offset into a baseline location."""
        if elem is None:
            return None
        return {
            "xpath": self._xpath[elem],
            "slot": slot,
            "offset": offset,
        }


class NullTracker:
    """Tracker used when source mapping is disabled; every hook is a no-op."""

    def pieces(self, elem, slot):
        return []

    def assign(self, elem, slot, pieces):
        pass

    def copy_slot(self, destination, dest_slot, source, source_slot):
        pass

    def mark_synthetic(self, elem, slot):
        pass

    def clear_slot(self, elem, slot):
        pass

    def drop(self, elem):
        pass

    def describe(self, elem, slot, offset):
        return None


NULL_TRACKER = NullTracker()


def iter_slots(root):
    """Yield (element, slot, value) in true document order.

    The order for each element is: its own ``text``, then every child
    recursively (a child's ``tail`` belongs right after that child), then
    the element's own ``tail``. ``Element.iter()`` cannot be used directly
    because it visits an element's tail together with the element itself,
    before its descendants -- which is not document order for mixed content
    like ``<p>text<em>em</em>tail</p>``.
    """
    def walk(elem):
        if elem.text:
            yield elem, TEXT, elem.text
        for child in elem:
            yield from walk(child)
            if child.tail:
                yield child, TAIL, child.tail

    yield from walk(root)
    if root.tail:
        yield root, TAIL, root.tail


def build_source_map(root, tracker):
    """Build plain text and character-level source ranges for a summary tree.

    Plain text is the concatenation of every text/tail slot in document
    order; each run of consecutive whitespace characters (including line
    breaks and tabs) is collapsed into a single space. Only non-whitespace
    characters originating from the baseline tree get source ranges;
    whitespace and synthetic text are left unmapped.

    Returns ``(plain_text, ranges)`` where each range is a dict with
    half-open ``[start, end)`` offsets into ``plain_text``. Adjacent
    characters sharing the same baseline slot are coalesced into one range.
    """
    output = []
    ranges = []
    position = 0
    pending_space = False
    # The open range tracks origin identity, not the per-character offset,
    # so adjacent characters of one baseline slot coalesce correctly.
    open_range = None

    def flush_pending_space():
        nonlocal position, pending_space, open_range
        if pending_space:
            output.append(" ")
            position += 1
            pending_space = False
            # The inserted separator breaks adjacency of source ranges.
            open_range = None

    def emit_char(char, origin_elem, origin_slot, offset):
        nonlocal position, pending_space, open_range
        if char.isspace():
            # Whitespace is global state: a newline in one slot still
            # separates the words in the next slot.
            pending_space = True
            return
        flush_pending_space()
        output.append(char)
        start = position
        position += 1
        identity = (origin_elem, origin_slot)
        if open_range is not None and open_range["identity"] == identity:
            # Coalesce adjacent characters sharing the same origin slot,
            # including adjacent synthetic characters (identity is
            # (None, None)).
            open_range["end"] = position
        else:
            open_range = {
                "start": start,
                "end": position,
                "identity": identity,
                "first_offset": offset,
            }
            ranges.append(open_range)

    for elem, slot, value in iter_slots(root):
        pieces = tracker.pieces(elem, slot)
        cursor = 0
        for origin_elem, origin_slot, length in pieces:
            fragment = value[cursor:cursor + length]
            for index, char in enumerate(fragment):
                # Offset inside the baseline slot equals the piece-relative
                # index; pieces always cover whole source slots.
                emit_char(char, origin_elem, origin_slot, index)
            cursor += length
        # Defensive: characters not covered by provenance pieces are treated
        # as synthetic rather than silently misattributed.
        for char in value[cursor:]:
            emit_char(char, None, None, 0)

    # Drop a trailing separator.
    if output and output[-1] == " ":
        output.pop()
        position -= 1
    text = "".join(output)

    # Materialize internal ranges into the public descriptor shape, dropping
    # a leading separator and shifting the remaining offsets by one.
    leading = 1 if text.startswith(" ") else 0
    if leading:
        text = text[1:]
    result = []
    for item in ranges:
        start = item["start"] - leading
        end = item["end"] - leading
        if end <= 0:
            continue
        origin_elem, origin_slot = item["identity"]
        source = (
            None
            if origin_elem is None
            else tracker.describe(
                origin_elem, origin_slot, item["first_offset"]
            )
        )
        result.append({"start": max(start, 0), "end": end, "source": source})
    return text, result


def summary_payload(document):
    """Assemble the full JSON-serializable payload for a summary tree.

    ``html`` is taken from the document's own ``get_clean_html()`` so the
    serialization is byte-identical to :meth:`Document.summary`; the source
    ranges are computed from the same underlying tree.
    """
    html = document.get_clean_html()
    text, ranges = build_source_map(document.html, document.tracker)
    return {
        "html": html,
        "text": text,
        "sources": ranges,
    }
