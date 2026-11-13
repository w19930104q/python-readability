"""Offline regression tests for the optional source mapping interface.

The mapping contract enforced here:

* ``summary_with_sources()`` returns ``html`` / ``text`` / ``sources``;
* enabling mapping never changes the extracted summary HTML;
* the caller's DOM is never mutated when an element is passed as input;
* every non-whitespace plain-text character is covered by exactly one
  half-open source range;
* each range round-trips to the same decoded characters at the recorded
  XPath + slot + Unicode offset in the pre-cleaning parse tree;
* identical text in different locations keeps distinct sources;
* synthetic text (the VIDEO placeholder) is explicitly marked source-less;
* results are stable across repeated calls and across the lenient retry.
"""

import json
import subprocess
import sys
import unittest

from lxml.html import document_fromstring
from lxml.html import fragment_fromstring

from readability import Document
from readability.sourcemap import build_source_map
from readability.sourcemap import SourceTracker


LONG = (
    "This is ordinary article text with enough punctuation and detail "
    "to be selected by the extractor without any doubt today. "
)


def baseline_tree(html):
    """Parse tree equivalent to the Document baseline (entities decoded)."""
    return document_fromstring(html)


class MappingAssertsMixin:
    def assert_valid_mapping(self, html, payload):
        tree = baseline_tree(html).getroottree()
        text = payload["text"]
        ranges = payload["sources"]

        # Half-open, in-order, mutually disjoint intervals.
        previous_end = 0
        covered = [False] * len(text)
        for item in ranges:
            start, end = item["start"], item["end"]
            self.assertGreaterEqual(start, previous_end)
            self.assertLess(start, end)
            self.assertLessEqual(end, len(text))
            previous_end = end
            for index in range(start, end):
                self.assertFalse(covered[index])
                covered[index] = True

        # Every non-whitespace character is covered exactly once.
        for index, char in enumerate(text):
            if not char.isspace():
                self.assertTrue(covered[index], f"unmapped char at {index}")

        # Every mapped range round-trips to equal baseline characters.
        for item in ranges:
            source = item["source"]
            if source is None:
                continue
            nodes = tree.xpath(source["xpath"])
            self.assertTrue(nodes, source["xpath"])
            slot_value = getattr(nodes[0], source["slot"])
            self.assertIsNotNone(slot_value)
            length = item["end"] - item["start"]
            fragment = slot_value[
                source["offset"]:source["offset"] + length
            ]
            self.assertEqual(
                fragment,
                text[item["start"]:item["end"]],
                f"round-trip mismatch at {source}",
            )

    def source_at(self, payload, position):
        for item in payload["sources"]:
            if item["start"] <= position < item["end"]:
                return item["source"]
        self.fail(f"no source at position {position}")


class TestSourceMapping(MappingAssertsMixin, unittest.TestCase):
    def test_payload_shape_and_default_cli_unchanged(self):
        html = f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertEqual(
            sorted(payload.keys()), ["html", "sources", "text"]
        )
        self.assertIsInstance(payload["html"], str)
        self.assertIsInstance(payload["text"], str)
        for item in payload["sources"]:
            self.assertEqual(
                sorted(item.keys()), ["end", "source", "start"]
            )
            if item["source"] is not None:
                self.assertEqual(
                    sorted(item["source"].keys()),
                    ["offset", "slot", "xpath"],
                )
                self.assertIn(item["source"]["slot"], ("text", "tail"))

    def test_mapping_does_not_change_summary_selection(self):
        html = f"""
        <html><body>
            <div class="sidebar">Sponsored links and ads. {'Ad. ' * 20}</div>
            <article><p>{LONG * 4}</p><p>{LONG * 2}</p></article>
        </body></html>
        """
        without = Document(html).summary()
        with_partial = Document(html).summary(html_partial=True)
        enabled = Document(html, source_mapping=True).summary_with_sources()
        enabled_partial = Document(
            html, source_mapping=True
        ).summary_with_sources(html_partial=True)

        self.assertEqual(enabled["html"], without)
        self.assertEqual(enabled_partial["html"], with_partial)

    def test_repeated_calls_are_stable(self):
        html = f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        doc = Document(html, source_mapping=True)
        first = doc.summary_with_sources()
        second = doc.summary_with_sources()
        third = doc.summary_with_sources(html_partial=True)

        self.assertEqual(first, second)
        self.assertEqual(first["html"], Document(html).summary())
        # title() before/after summary stays stable
        title_doc = Document(
            f"<html><head><title>Stable title</title></head>"
            f"<body><article><p>{LONG * 3}</p></article></body></html>",
            source_mapping=True,
        )
        self.assertEqual(title_doc.title(), "Stable title")
        title_doc.summary_with_sources()
        self.assertEqual(title_doc.title(), "Stable title")
        self.assertEqual(third["html"], Document(html).summary(html_partial=True))

    def test_duplicate_paragraphs_keep_distinct_sources(self):
        html = f"""
        <html><body><article>
            <p>Duplicated sentence about a very specific topic number one.</p>
            <p>{LONG * 2}</p>
            <p>Duplicated sentence about a very specific topic number one.</p>
        </article></body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        needle = "Duplicated sentence"
        first = payload["text"].index(needle)
        second = payload["text"].index(needle, first + 1)

        source_one = self.source_at(payload, first)
        source_two = self.source_at(payload, second)
        self.assertNotEqual(source_one["xpath"], source_two["xpath"])
        self.assertEqual(source_one["slot"], "text")
        self.assertEqual(source_two["slot"], "text")
        self.assert_valid_mapping(html, payload)

    def test_nested_emphasis_and_tail_order(self):
        html = f"""
        <html><body><article>
            <p>Open <strong>strong words</strong> middle <em>em words</em>
            closing text with enough length for selection now.</p>
            <p>{LONG * 2}</p>
        </article></body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        # Document order preserved across nested inline elements and tails.
        self.assertIn("Open strong words middle em words", payload["text"])
        self.assert_valid_mapping(html, payload)

        tree = baseline_tree(html).getroottree()
        em = tree.xpath("//em")[0]
        position = payload["text"].index("em words")
        source = self.source_at(payload, position)
        self.assertEqual(source["xpath"], tree.getpath(em))
        self.assertEqual(source["slot"], "text")
        self.assertEqual(source["offset"], 0)

        # The tail following <em> maps to em's tail slot, not the parent.
        tail_needle = "closing text"
        tail_position = payload["text"].index(tail_needle)
        tail_source = self.source_at(payload, tail_position)
        self.assertEqual(tail_source["xpath"], tree.getpath(em))
        self.assertEqual(tail_source["slot"], "tail")

    def test_cjk_and_emoji_count_unicode_code_points(self):
        html = """
        <html><head><meta charset="utf-8"></head><body><article>
            <p>中文段落需要足够长才能被提取器选中，这里补充更多的中文内容。</p>
            <p>Emoji 🎉 and 👨‍👩‍👧 family plus 日本語と한국어 섞인 본문.</p>
            <p>%s</p>
        </article></body></html>
        """ % (LONG * 3)
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertIn("中文段落", payload["text"])
        self.assertIn("🎉", payload["text"])
        self.assertIn("👨‍👩‍👧", payload["text"])
        self.assert_valid_mapping(html, payload)

        position = payload["text"].index("中")
        source = self.source_at(payload, position)
        node = baseline_tree(html).getroottree().xpath(source["xpath"])[0]
        # Offset is a Unicode code point offset, not a UTF- byte offset.
        self.assertEqual(
            getattr(node, source["slot"])[source["offset"]], "中"
        )

    def test_entities_counted_after_decoding(self):
        html = f"""
        <html><body><article>
            <p>a&amp;b&#65;c&eacute;&nbsp;entity sentence with
            enough surrounding words to survive extraction.</p>
            <p>{LONG * 2}</p>
        </article></body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertIn("a&bAcé", payload["text"])
        self.assert_valid_mapping(html, payload)
        position = payload["text"].index("a&bAcé")
        source = self.source_at(payload, position)
        self.assertEqual(source["slot"], "text")
        # The whole decoded token coalesces into one range starting at 0.
        self.assertEqual(source["offset"], 0)
        # '&' is one decoded code point, so 'A' is the 3rd character; its
        # absolute slot offset is range offset + in-range position.
        range_a = next(
            item for item in payload["sources"]
            if item["start"] <= position + 2 < item["end"]
        )
        absolute_offset = (
            range_a["source"]["offset"]
            + (position + 2) - range_a["start"]
        )
        self.assertEqual(absolute_offset, 2)

    def test_tail_migration_on_drop(self):
        # An unlikely-candidate element is dropped; its tail must survive and
        # keep pointing at the tail slot of the removed element.
        html = f"""
        <html><body><article>
            <p>{LONG * 2}</p>
            <div class="comments">removed block</div>orphan tail text that
            follows the dropped block and stays readable after cleanup.
            <p>{LONG * 2}</p>
        </article></body></html>
        """
        doc = Document(html, source_mapping=True)
        payload = doc.summary_with_sources(html_partial=True)
        self.assertIn("orphan tail text", payload["text"])
        self.assertNotIn("removed block", payload["text"])
        self.assert_valid_mapping(html, payload)
        position = payload["text"].index("orphan tail text")
        source = self.source_at(payload, position)
        # lxml migrates the tail onto the previous sibling's tail slot.
        self.assertEqual(source["slot"], "tail")

    def test_div_to_p_transformation_keeps_provenance(self):
        html = f"""
        <html><body><article>
            <div class="article-body">
                Wrapped leading text <b>bold inline</b> trailing words here.
                <p>{LONG * 2}</p>
            </div>
        </article></body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertIn("Wrapped leading text", payload["text"])
        self.assertIn("bold inline", payload["text"])
        self.assert_valid_mapping(html, payload)

    def test_removed_advertisements_are_not_in_map(self):
        ad = "Buy this sponsored product with a limited discount offer. " * 5
        html = f"""
        <html><body>
        <main>
            <article><p>{LONG * 3}</p></article>
            <aside class="sponsor"><p>{ad}</p></aside>
        </main>
        </body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertNotIn("sponsored product", payload["text"])
        self.assert_valid_mapping(html, payload)
        for item in payload["sources"]:
            if item["source"] is None:
                continue
            self.assertNotIn("aside", item["source"]["xpath"])

    def test_synthetic_video_placeholder_has_no_source(self):
        html = f"""
        <html><body><article>
            <p>{LONG * 2}</p>
            <iframe src="https://www.youtube.com/embed/safe"></iframe>
            <p>{LONG * 2}</p>
        </article></body></html>
        """
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertIn("VIDEO", payload["text"])
        position = payload["text"].index("VIDEO")
        synthetic_ranges = [
            item for item in payload["sources"]
            if item["start"] <= position < item["end"]
        ]
        self.assertTrue(synthetic_ranges)
        for item in synthetic_ranges:
            self.assertIsNone(item["source"])
        # Everything else still has a real source.
        sourced = [
            item for item in payload["sources"] if item["source"] is not None
        ]
        self.assertTrue(sourced)

    def test_lenient_retry_keeps_mapping_accurate(self):
        # A tiny ruthless result forces the retry loop with ruthless=False.
        html = f"""
        <html><body>
            <div class="comment wrapper">
                <article><p>{LONG}</p></article>
            </div>
        </body></html>
        """
        doc = Document(html, source_mapping=True, retry_length=100000)
        payload = doc.summary_with_sources()
        self.assertIn("ordinary article text", payload["text"])
        self.assert_valid_mapping(html, payload)

    def test_whitespace_normalization(self):
        html = """
        <html><body><article>
            <p>Word1\t\tWord2
            Word3

            Word4</p>
            <p>%s</p>
        </article></body></html>
        """ % (LONG * 2)
        payload = Document(html, source_mapping=True).summary_with_sources()
        self.assertIn("Word1 Word2 Word3 Word4", payload["text"])
        self.assertNotIn("\t", payload["text"])
        self.assertNotIn("\n", payload["text"])
        self.assert_valid_mapping(html, payload)

    def test_caller_dom_is_not_mutated(self):
        from lxml.etree import tostring

        tree = document_fromstring(
            f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        )
        before = tostring(tree)
        Document(tree, source_mapping=True).summary_with_sources()
        self.assertEqual(tostring(tree), before)

        # Same guarantee when the root element itself is passed in.
        root = document_fromstring(
            f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        )
        before = tostring(root)
        Document(root, source_mapping=True).summary_with_sources()
        self.assertEqual(tostring(root), before)

    def test_lazy_enabling_reparses_baseline(self):
        html = f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        doc = Document(html)
        doc.summary()  # mutate first
        payload = doc.summary_with_sources()
        self.assert_valid_mapping(html, payload)
        self.assertEqual(
            payload["html"], Document(html).summary()
        )

    def test_partial_and_full_document_output(self):
        html = f"<html><body><article><p>{LONG * 3}</p></article></body></html>"
        full = Document(html, source_mapping=True).summary_with_sources()
        partial = Document(
            html, source_mapping=True
        ).summary_with_sources(html_partial=True)
        self.assertIn("<html", full["html"])
        self.assertNotIn("<html", partial["html"])
        self.assert_valid_mapping(html, full)
        self.assert_valid_mapping(html, partial)


class TestSourceTrackerUnit(unittest.TestCase):
    def test_iter_slots_document_order_with_mixed_content(self):
        from readability.sourcemap import iter_slots, TEXT, TAIL

        root = fragment_fromstring(
            "<div><p>t1<em>e1</em>tail1<b>b1</b>tail2</p></div>"
        )
        sequence = [
            (elem.tag, slot) for elem, slot, _ in iter_slots(root)
        ]
        self.assertEqual(
            sequence,
            [
                ("p", TEXT),
                ("em", TEXT),
                ("em", TAIL),
                ("b", TEXT),
                ("b", TAIL),
            ],
        )

    def test_drop_migrates_tail_provenance(self):
        root = fragment_fromstring(
            "<div><p>keep</p><b>gone</b>surviving tail</div>"
        )
        tracker = SourceTracker(root)
        b = root.find(".//b")
        p = root.find(".//p")
        tail_pieces = tracker.pieces(b, "tail")
        tracker.drop(b)
        b.drop_tree()
        # lxml moved the tail to p's tail slot.
        self.assertEqual(p.tail, "surviving tail")
        self.assertEqual(tracker.pieces(p, "tail"), tail_pieces)

    def test_drop_without_previous_sibling_uses_parent_text(self):
        root = fragment_fromstring("<div><b>gone</b>surviving text</div>")
        tracker = SourceTracker(root)
        b = root.find(".//b")
        tail_pieces = tracker.pieces(b, "tail")
        tracker.drop(b)
        b.drop_tree()
        div = root
        self.assertEqual(div.text, "surviving text")
        self.assertEqual(tracker.pieces(div, "text"), tail_pieces)

    def test_null_tracker_is_passive(self):
        from readability.sourcemap import NULL_TRACKER

        root = fragment_fromstring("<div><p>x</p></div>")
        # Must not raise and must record nothing.
        NULL_TRACKER.drop(root.find(".//p"))
        self.assertEqual(NULL_TRACKER.pieces(root, "text"), [])
        text, ranges = build_source_map(root, NULL_TRACKER)
        self.assertEqual(text, "x")
        # Without a tracker every character is necessarily source-less.
        self.assertEqual(
            ranges, [{"start": 0, "end": 1, "source": None}]
        )


class TestJsonCli(unittest.TestCase):
    def test_json_flag_emits_valid_payload(self):
        html = f"""
        <html><head><title>CLI page</title></head>
        <body><article><p>{LONG * 3}</p></article></body></html>
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "readability",
                "--json",
                "/dev/stdin",
            ],
            input=html,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(
            sorted(payload.keys()), ["html", "sources", "text", "title"]
        )
        self.assertEqual(payload["title"], "CLI page")
        self.assertTrue(payload["sources"])

    def test_default_cli_output_is_plain_text(self):
        html = f"""
        <html><head><title>Plain page</title></head>
        <body><article><p>{LONG * 3}</p></article></body></html>
        """
        result = subprocess.run(
            [sys.executable, "-m", "readability", "/dev/stdin"],
            input=html,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertTrue(result.stdout.startswith("Title:"))
        self.assertNotIn('"sources"', result.stdout)


if __name__ == "__main__":
    unittest.main()
