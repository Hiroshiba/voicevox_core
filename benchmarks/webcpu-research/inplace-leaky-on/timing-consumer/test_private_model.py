"""Synthetic transport cases; production source/model pins are never replaced."""
import io, unittest
import prepare_private_model as model

class Response(io.BytesIO):
    def __init__(self, raw=b'abcde', status=200, headers=None, url=model.FINAL_URL):
        super().__init__(raw); self.status = status; self.headers = headers or {}; self.url = url
    def geturl(self): return self.url

class Tests(unittest.TestCase):
    def test_exact_stream_with_or_without_declared_length(self):
        for headers in [{}, {'Content-Length': '5'}]:
            self.assertEqual(model.read_archive(Response(headers=headers), expected_size=5), b'abcde')
    def test_chunked_short_reads_are_accumulated_without_truncation(self):
        class Chunked(Response):
            def read(self, size=-1): return super().read(min(size, 2))
        self.assertEqual(model.read_archive(Chunked(), expected_size=5), b'abcde')
    def test_partial_truncated_oversized_or_wrong_origin_rejected(self):
        cases = [Response(status=206), Response(headers={'Content-Range': 'bytes 0-4/5'}), Response(raw=b'abcd'),
                 Response(raw=b'abcdef'), Response(headers={'Content-Length': '4'}), Response(url='https://invalid.example/archive')]
        for response in cases:
            with self.assertRaises(ValueError): model.read_archive(response, expected_size=5)
    def test_original_pins_and_actual_verified_size_retained(self):
        self.assertEqual(model.ARCHIVE_BYTES, 172305822)
        self.assertEqual(model.ARCHIVE_SHA, '1b02f8d4743c70be2f501e51f47b4c248fa1160a7677c041ddf1e753353f7081')
        self.assertEqual(model.MODEL_SHA, '51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9')

if __name__ == '__main__': unittest.main()
