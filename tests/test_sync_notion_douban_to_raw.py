"""Offline synthetic fixtures only. No source records, credentials, or network."""
import contextlib
import io
from pathlib import Path
import unittest
import sys
import yaml
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_notion_douban_to_raw as m


A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


def page(**properties):
    return {"id": A, "created_time": "2020-01-02T00:00:00Z", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Synthetic Film"}]}, **properties}}


def listing(kind, values, more=False, cursor=None):
    return {"object": "list", "results": [{"object": "property_item", "type": kind, kind: value}
            for value in values], "has_more": more, "next_cursor": cursor}


def title(text):
    return listing("title", [{"plain_text": text}])


def relation(ids=(), more=False):
    return {"type": "relation", "id": "r%3Ax", "has_more": more,
            "relation": [{"id": i} for i in ids]}


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(m.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self.reader = m.RelationTitleReader("synthetic-key")

    def test_number_formula_unchanged(self):
        for value in (None, 0, 4, 8.75):
            for prop in ({"type": "number", "number": value},
                         {"type": "formula", "formula": {"type": "number", "number": value}}):
                self.assertEqual(m.page_rating(page(评分=prop)), value)

    def test_star_select_preserves_original_not_ten_point_score(self):
        for star in ("⭐️", "⭐"):
            for count in range(1, 6):
                label = star * count
                self.assertEqual(m.page_rating(page(评分={"type": "select", "select": {"name": label}})), label)

    def test_empty_rating(self):
        for selected in (None, {"name": ""}, {"name": "  "}):
            self.assertIsNone(m.page_rating(page(评分={"type": "select", "select": selected})))
        self.assertIsNone(m.page_rating(page()))

    def test_unknown_rating_label_preserved_without_normalization(self):
        for label in ("4/5", "五星推荐", "⭐️" * 6, "  unrated  ", "one|two\r\nthree"):
            with self.assertWarnsRegex(UserWarning, "without numeric normalization"):
                self.assertEqual(m.page_rating(page(评分={"type": "select", "select": {"name": label}})), label)

    def test_unknown_rating_schema_fails_not_silent_missing(self):
        for selected in ({"name": 4}, []):
            with self.assertRaises(ValueError):
                m.page_rating(page(评分={"type": "select", "select": selected}))
        with self.assertRaises(ValueError):
            m.page_rating(page(评分={"type": "rollup", "rollup": {}}))

    def test_rating_yaml_preserves_raw_value_and_index_escapes(self):
        label = 'one|two\r\nthree "quoted"'
        p = page(评分={"type": "select", "select": {"name": label}})
        with self.assertWarns(UserWarning):
            body, _ = m.render_movie_note(p, {"id": C})
        import json
        line = next(line for line in body.splitlines() if line.startswith("rating: "))
        self.assertEqual(json.loads(line[len("rating: "):]), label)
        index = m.render_index([{"title": "Synthetic Film", "director": "A|B\nC", "rating": label, "path": "film.md"}])
        self.assertIn('one\\|two<br>three "quoted"', index)
        self.assertIn("A\\|B<br>C", index)
        self.assertNotIn("\r", index)

    def test_new_string_fields_yaml_roundtrip_controls_and_emoji(self):
        # This must use a YAML reader, not just a JSON quote-line decoder.
        for codepoint in (*range(0x7f, 0xa0), 0x2028, 0x2029):
            with self.subTest(codepoint=codepoint):
                control = chr(codepoint)
                rating = "⭐️before" + control + "after⭐️"
                director = "Director " + control + " Example ⭐️"
                p = page(评分={"type": "select", "select": {"name": rating}}, 导演=relation([B]))
                with patch.object(m, "notion_request", return_value=title(director)):
                    resolved = m.page_director(p, m.RelationTitleReader("synthetic-key"))
                with self.assertWarns(UserWarning):
                    body, _ = m.render_movie_note(p, {"id": C}, director=resolved)
                header = body.split("\n---\n", 1)[0][4:]
                loaded = yaml.safe_load(header)
                self.assertEqual(loaded["rating"], rating)
                self.assertEqual(loaded["director"], director)
                self.assertNotIn(control, header)
                self.assertIn("\\u" + format(codepoint, "04x"), header)
                self.assertIn("⭐️", header)

    def test_mapping_warnings_and_errors_do_not_include_private_payloads(self):
        private = "private-label-or-title-DO-NOT-LOG"
        with self.assertWarns(UserWarning) as warning:
            m.page_rating(page(评分={"type": "select", "select": {"name": private}}))
        self.assertNotIn(private, str(warning.warning))
        with patch.object(m, "notion_request", side_effect=RuntimeError("https://example.invalid/" + B + private)):
            with self.assertRaises(RuntimeError) as error:
                self.reader.title(B)
        for value in (private, B, "https://"):
            self.assertNotIn(value, str(error.exception))

    def test_legacy_director_types_and_author_fallback(self):
        for kind in ("title", "rich_text", "url"):
            value = "Director A" if kind == "url" else [{"plain_text": "Director A"}]
            self.assertEqual(m.page_director(page(导演={"type": kind, kind: value})), "Director A")
        self.assertEqual(m.page_director(page(作者={"type": "rich_text", "rich_text": [{"plain_text": "Author A"}]})), "Author A")

    def test_relation_order_and_same_run_cache(self):
        p = page(导演=relation([C, B, C]))
        with patch.object(m, "notion_request", side_effect=[title("Director C"), title("Director B")]) as req:
            self.assertEqual(m.page_director(p, self.reader), "Director C、Director B、Director C")
            self.assertEqual(m.page_director(p, self.reader), "Director C、Director B、Director C")
            self.assertEqual(req.call_count, 2)
            self.assertTrue(all(call.args[1] == "GET" and "/properties/title?" in call.args[2] for call in req.call_args_list))

    def test_relation_pagination_restarts_complete_property(self):
        p = page(导演=relation([C], more=True))
        responses = [listing("relation", [{"id": C}], True, "next/one"),
                     listing("relation", [{"id": B}]), title("Director C"), title("Director B")]
        with patch.object(m, "notion_request", side_effect=responses) as req:
            self.assertEqual(m.page_director(p, self.reader), "Director C、Director B")
            paths = [call.args[2] for call in req.call_args_list]
            self.assertIn("/properties/r%3Ax?", paths[0])
            self.assertNotIn("%253A", paths[0])
            self.assertIn("start_cursor=next%2Fone", paths[1])

    def test_title_pagination(self):
        with patch.object(m, "notion_request", side_effect=[listing("title", [{"plain_text": "Director "}], True, "next"), title("A")]):
            self.assertEqual(self.reader.title(B), "Director A")

    def test_relation_pagination_cannot_drop_observed_values(self):
        for values in ([], [{"id": C}], [{"id": B}, {"id": C}]):
            with patch.object(m, "notion_request", return_value=listing("relation", values)):
                with self.assertRaisesRegex(ValueError, "changed or truncated"):
                    m.page_director(page(导演=relation([C], more=True)), self.reader)

    def test_dry_run_does_not_write(self):
        p = page(导演=relation([B]))
        with patch.object(m, "parse_args", return_value=type("Args", (), {"dry_run": True, "max_pages": 1})()), \
             patch.object(m, "load_notion_api_key", return_value="synthetic-key"), \
             patch.object(m, "resolve_database", return_value={"id": C}), \
             patch.object(m, "paginate_database", return_value=[p, p]), \
             patch.object(m, "notion_request", return_value=title("Director A")) as req, \
             patch.object(m, "write_text") as write, contextlib.redirect_stdout(io.StringIO()):
            m.main()
            write.assert_not_called()
            self.assertEqual(req.call_count, 1)

    def test_empty_relation_no_requests(self):
        with patch.object(m, "notion_request") as req:
            self.assertEqual(m.page_director(page(导演=relation()), self.reader), "")
            req.assert_not_called()

    def test_missing_reader_fails(self):
        with self.assertRaises(ValueError):
            m.page_director(page(导演=relation([B])))

    def test_permission_failure_sanitized_and_not_cached(self):
        with patch.object(m, "notion_request", side_effect=RuntimeError("403 secret/person/page-data")):
            with self.assertRaisesRegex(RuntimeError, "read failed; sync incomplete") as caught:
                self.reader.title(B)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(self.reader.titles, {})

    def test_schema_and_cursor_failures(self):
        for response in ({}, {"object": "list", "results": [], "has_more": True},
                         listing("relation", [{"id": B}]), title("")):
            with self.subTest(response=response), patch.object(m, "notion_request", return_value=response):
                with self.assertRaises(ValueError):
                    self.reader.title(B)
        response = listing("title", [{"plain_text": "x"}], True, "repeat")
        with patch.object(m, "notion_request", return_value=response) as req:
            with self.assertRaisesRegex(ValueError, "pagination"):
                self.reader.title(B)
            self.assertEqual(req.call_count, 2)

    def test_invalid_relation_schema_and_identifier_fail(self):
        for prop in ({"type": "relation", "relation": []}, relation(["../unknown"]),
                     {"type": "people", "people": []}):
            with self.assertRaises(ValueError):
                m.page_director(page(导演=prop), self.reader)

    def test_output_slots_and_path_unchanged(self):
        p = page(导演=relation([B]), 评分={"type": "select", "select": {"name": "⭐️⭐️⭐️⭐️"}})
        with patch.object(m, "notion_request", return_value=title("Director A")):
            director = m.page_director(p, self.reader)
        body, path = m.render_movie_note(p, {"id": C}, director=director)
        self.assertIn('director: "Director A"', body)
        self.assertIn('rating: "⭐️⭐️⭐️⭐️"', body)
        self.assertIn("- 导演：Director A", body)
        self.assertIn("- 评分：⭐️⭐️⭐️⭐️", body)
        self.assertEqual(path.relative_to(m.OUTPUT_ROOT).as_posix(), "2020/2020-01/Synthetic Film.md")
        index = m.render_index([{"title": "Synthetic Film", "director": director,
            "rating": m.page_rating(p), "path": "2020/2020-01/Synthetic Film.md"}])
        self.assertIn("| Synthetic Film | Director A | ⭐️⭐️⭐️⭐️ |", index)
        self.assertIn("| 片名 | 导演 | 评分 | 状态 | 日期 | 文件 |", index)

    def test_main_resolves_once_for_note_and_index(self):
        p = page(导演=relation([B]), 评分={"type": "select", "select": {"name": "⭐️⭐️⭐️⭐️"}})
        with patch.object(m, "parse_args", return_value=type("Args", (), {"dry_run": False, "max_pages": 0})()), \
             patch.object(m, "load_notion_api_key", return_value="synthetic-key"), \
             patch.object(m, "resolve_database", return_value={"id": C}), \
             patch.object(m, "paginate_database", return_value=[p]), \
             patch.object(m, "notion_request", return_value=title("Director A")) as req, \
             patch.object(m, "write_text") as write, contextlib.redirect_stdout(io.StringIO()):
            m.main()
            self.assertEqual(req.call_count, 1)
            self.assertEqual(write.call_count, 2)
            for call in write.call_args_list:
                self.assertIn("Director A", call.args[1])
                self.assertIn("⭐️⭐️⭐️⭐️", call.args[1])

    def test_main_failure_does_not_write_or_claim_completion(self):
        p = page(导演=relation([B]))
        output = io.StringIO()
        with patch.object(m, "parse_args", return_value=type("Args", (), {"dry_run": False, "max_pages": 0})()), \
             patch.object(m, "load_notion_api_key", return_value="synthetic-key"), \
             patch.object(m, "resolve_database", return_value={"id": C}), \
             patch.object(m, "paginate_database", return_value=[p]), \
             patch.object(m, "notion_request", side_effect=RuntimeError("403")), \
             patch.object(m, "write_text") as write, contextlib.redirect_stdout(output):
            with self.assertRaises(RuntimeError):
                m.main()
            write.assert_not_called()
        self.assertNotIn("WROTE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
