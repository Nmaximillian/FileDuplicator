"""Generate a macOS .icns icon from the existing .ico file.

Requires Pillow and macOS (uses iconutil).
Run on macOS only: python generate_icns.py
"""

import os
import subprocess
import sys
import tempfile

from PIL import Image

ICONSET_SIZES = [16, 32, 64, 128, 256, 512]


def generate_icns(ico_path: str = "FileDuplicator.ico", output_path: str = "FileDuplicator.icns"):
    """Convert a .ico file to a .icns using Pillow + iconutil."""
    if sys.platform != "darwin":
        print("ERROR: .icns generation requires macOS (iconutil).")
        sys.exit(1)

    img = Image.open(ico_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        iconset_dir = os.path.join(tmpdir, "FileDuplicator.iconset")
        os.makedirs(iconset_dir)

        for size in ICONSET_SIZES:
            # Standard resolution
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(os.path.join(iconset_dir, f"icon_{size}x{size}.png"), "PNG")
            # @2x (Retina)
            size2x = size * 2
            if size2x <= 1024:
                resized2x = img.resize((size2x, size2x), Image.Resampling.LANCZOS)
                resized2x.save(os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png"), "PNG")

        # iconutil converts the .iconset directory to a .icns file
        result = subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", output_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"iconutil error: {result.stderr}")
            sys.exit(1)

    print(f"macOS icon saved to {output_path}")


if __name__ == "__main__":
    generate_icns()
