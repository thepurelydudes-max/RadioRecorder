from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import sys

import app


class IconResourcesTests(unittest.TestCase):
    def test_icon_files_are_in_source_tree(self):
        root = Path(app.__file__).resolve().parent / 'assets'
        self.assertTrue((root / 'radiorecorder.ico').is_file())
        self.assertTrue((root / 'radiorecorder_64.png').is_file())

    def test_find_asset_source_layout(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'assets').mkdir()
            img = root / 'assets' / 'radiorecorder_64.png'
            img.touch()
            with patch.object(app, 'APP_DIR', root):
                self.assertEqual(app.find_asset(img.name), img)

    def test_find_asset_pyinstaller_internal_layout(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as meipass:
            assets = Path(meipass) / 'assets'
            assets.mkdir()
            ico = assets / 'radiorecorder.ico'
            ico.touch()
            with patch.object(app, 'APP_DIR', Path(tmp)), patch.object(sys, '_MEIPASS', meipass, create=True):
                self.assertEqual(app.find_asset(ico.name), ico)


if __name__ == '__main__':
    unittest.main()
