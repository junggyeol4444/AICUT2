import io
import unittest

from backend.http_utils import read_json_object


class HttpUtilsTest(unittest.TestCase):
    def test_reads_bounded_json_object(self):
        payload = b'{"project_id":"one"}'
        value = read_json_object(
            io.BytesIO(payload), str(len(payload)), "application/json; charset=utf-8",
            max_bytes=1024,
        )
        self.assertEqual(value, {"project_id": "one"})

    def test_rejects_oversized_truncated_and_non_object_payloads(self):
        with self.assertRaisesRegex(ValueError, "허용 크기"):
            read_json_object(io.BytesIO(b"{}"), "100", "application/json", max_bytes=10)
        with self.assertRaisesRegex(ValueError, "짧습니다"):
            read_json_object(io.BytesIO(b"{}"), "3", "application/json", max_bytes=10)
        with self.assertRaisesRegex(ValueError, "최상위"):
            read_json_object(io.BytesIO(b"[]"), "2", "application/json", max_bytes=10)

    def test_rejects_invalid_length_content_type_and_json(self):
        for length in ("invalid", "-1"):
            with self.subTest(length=length), self.assertRaises(ValueError):
                read_json_object(io.BytesIO(), length, "application/json", max_bytes=10)
        with self.assertRaisesRegex(ValueError, "application/json"):
            read_json_object(io.BytesIO(b"{}"), "2", "text/plain", max_bytes=10)
        with self.assertRaisesRegex(ValueError, "올바른 JSON"):
            read_json_object(io.BytesIO(b"{"), "1", "application/json", max_bytes=10)


if __name__ == "__main__":
    unittest.main()
