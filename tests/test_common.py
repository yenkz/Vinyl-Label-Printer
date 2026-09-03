import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from vinyl_labels import common


class Response:
    status_code = 200

    def __init__(self, content):
        self.content = content


class CoverDownloadTests(unittest.TestCase):
    def test_invalid_response_is_not_saved_as_a_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(common, "COVERS_DIR", Path(directory)),
                patch.object(common.requests, "get", return_value=Response(b"<html>nope</html>")),
            ):
                self.assertIsNone(common.download_cover("https://example.invalid", 12))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_valid_image_is_normalized_to_jpeg_atomically(self):
        source = io.BytesIO()
        Image.new("RGBA", (3, 2), (255, 0, 0, 128)).save(source, format="PNG")
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(common, "COVERS_DIR", Path(directory)),
                patch.object(common.requests, "get", return_value=Response(source.getvalue())),
            ):
                relative = common.download_cover("https://example.invalid", 12)
                destination = Path(directory) / "12.jpg"
                self.assertEqual(relative, f"{common.config.COVERS_DIR}/12.jpg")
                with Image.open(destination) as saved:
                    self.assertEqual(
                        (saved.format, saved.mode, saved.size), ("JPEG", "RGB", (3, 2))
                    )
                self.assertFalse((Path(directory) / ".12.jpg.tmp").exists())


if __name__ == "__main__":
    unittest.main()
