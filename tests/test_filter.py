"""Offline compatibility checks for saved download-filter expressions."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import chdir
from datetime import datetime
from pathlib import Path
import re
import tempfile
import unittest

from module.filter import BaseFilter, Filter
from utils.meta_data import MetaData, NoneObj


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = Filter()
        self.filter.set_meta_data(MetaData(
            message_date=datetime(2024, 1, 2, 3, 4, 5), message_id=42,
            message_caption="example", media_file_size=150 * 1024**2,
            media_width=1920, media_height=1080, media_file_name="clip.mp4",
            media_duration=30, media_type="video", file_extension="mp4",
            sender_id=123, sender_name="tester", message_thread_id=77,
        ))

    def test_saved_expressions_units_dates_regex_and_aliases(self):
        cases = {
            "media_file_size > 100MB and media_duration < 60": True,
            "media_file_size > 1GB || media_width >= 1920": True,
            "media_height <= 720 && media_duration != 0": False,
            "message_date >= 2024-01-01 00:00:00 and message_date < 2024-02-01 00:00:00": True,
            "message_date == 2024-01-02 03:04:05": True,
            "media_file_name == r'.*[.]mp4'": True,
            "media_file_name != r'.*[.]mkv'": True,
            "r'.*[.]mp4' == media_file_name": True,
            "r'.*[.]mp4' != media_file_name": False,
            "media_file_name == 'clip'": False,
            "file_size == media_file_size and file_name == media_file_name": True,
            "id == message_id && caption == message_caption": True,
            "topic_id == message_thread_id and sender_id == 123": True,
            "media_type == 'video' and file_extension == 'mp4'": True,
            "message_caption == 'example'": True,
            "1B == 1 and 1KB == 1024 and 1MB == 1024KB": True,
            "1GB == 1024MB and 1TB == 1024GB": True,
            "1 == 1\r\nand\t2 == 2": True,
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertIs(self.filter.exec(expression), expected)

    def test_arithmetic_boolean_precedence_and_eager_evaluation(self):
        parser = self.filter.filter
        for expression, expected in {
            "1 + 2 * 3": 7, "(1 + 2) * 3": 9, "8 / 2 * 3": 12.0,
            "8 - 2 - 1": 5, "-2 * -3": 6, "--2": 2, "1--2": 3,
            "0 or 2 and 3": 3, "(0 or 2) and 3": 3,
            "1 || 0 && 0": 1, "1 == 2 < 3": True, "1 == 1 == 1": True,
        }.items():
            with self.subTest(expression=expression):
                result = parser.exec(expression)
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))
        with self.assertRaises(ZeroDivisionError):
            parser.exec("0 and 1 / 0")
        with self.assertRaisesRegex(ValueError, "Undefined name unknown"):
            parser.exec("1 or unknown")
        self.assertFalse(self.filter.exec("2"))

    def test_missing_values_and_legacy_type_checks(self):
        self.filter.set_meta_data(MetaData(message_id=1))
        for op in ("==", "!=", ">", "<", ">=", "<="):
            self.assertFalse(self.filter.exec(f"media_width {op} 100"))
        parser = self.filter.filter
        parser.names["absent"] = NoneObj()
        self.assertTrue(parser.exec("absent == 1"))
        self.assertEqual(parser.exec("absent + 1"), 1)
        with self.assertRaisesRegex(ValueError, "1 is int"):
            parser.exec("1 + absent")
        self.assertFalse(parser.exec("media_width == media_height"))
        self.assertEqual(parser.exec("3 / 2 + 1"), 2.5)
        with self.assertRaisesRegex(ValueError, "1 is int but 1.5 is not"):
            parser.exec("1 + 3 / 2")
        with self.assertRaisesRegex(ValueError, "datetime"):
            parser.exec("2024-01-01 00:00:00 < 1")
        # A single '=' remains a literal-name comparison, never an assignment.
        self.assertTrue(parser.exec("file_name = 'file_name'"))
        self.assertIsNone(parser.names["file_name"])
        with self.assertRaisesRegex(ValueError, "file_size is str"):
            parser.exec("file_size = 1")

    def test_invalid_syntax_and_unsupported_python_are_rejected(self):
        for expression in (
            "", "1 +", "(1 == 1", "1 < 2 < 3", "1 @ + 2", "1 $ + 2",
            "file_name[0] == 'c'", "file_name.upper() == 'CLIP.MP4'",
            "media_duration > 1.5", "media_file_size > 1mb", "+1 == 1",
        ):
            with self.subTest(expression=expression):
                valid, error = self.filter.check_filter(expression)
                self.assertFalse(valid)
                self.assertTrue(error)
        valid, error = self.filter.check_filter("unknown == 1")
        self.assertFalse(valid)
        self.assertEqual(error, "Undefined name unknown")
        with self.assertRaises(re.error):
            self.filter.exec("media_file_name == r'['")
        self.assertEqual(self.filter.check_filter("1 +"), (False, "Syntax error at EOF"))

    def test_independent_metadata_and_reset(self):
        other = Filter()
        other.set_meta_data(MetaData(message_id=7))
        self.assertTrue(self.filter.exec("id == 42"))
        self.assertTrue(other.exec("id == 7"))
        self.filter.set_meta_data(MetaData(message_id=99))
        self.assertTrue(self.filter.exec("id == 99"))
        self.assertTrue(other.exec("id == 7"))
        self.filter.filter.reset()
        with self.assertRaisesRegex(ValueError, "meta data cannot be empty"):
            self.filter.exec("id == 99")

    def test_keywords_do_not_split_field_names(self):
        parser = BaseFilter()
        parser.names = {"android": 1, "origin": 2, "and_value": 3, "or_value": 4}
        self.assertTrue(parser.exec("android == 1 and origin == 2 && and_value < or_value"))

    def test_long_expressions_and_concurrent_filter_instances(self):
        self.assertEqual(BaseFilter().exec(" + ".join(["1"] * 1500)), 1500)

        def evaluate(value):
            parser = BaseFilter()
            parser.names["value"] = value
            return [parser.exec(f"value == {value}") for _ in range(30)]

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(all(results) for results in pool.map(evaluate, range(8))))

    def test_parser_writes_no_generated_files_and_does_not_execute_code(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            parser = BaseFilter()
            self.assertEqual(parser.exec("2 * 3"), 6)
            with self.assertRaises(ValueError):
                parser.exec("__import__('os').system('touch unexpected')")
            self.assertEqual(list(Path(directory).iterdir()), [])
