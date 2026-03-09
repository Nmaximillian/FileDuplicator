"""Generate the app icon (FileDuplicator.ico) programmatically."""

from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 24, 32, 48, 64, 128, 256]


def _draw_icon(size: int) -> Image.Image:
    """Draw a duplicate-files icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size  # shorthand

    # Rounded-rect helper (Pillow >= 8.2)
    def rrect(xy, fill, outline, width=1, radius=None):
        r = radius or max(2, s // 16)
        d.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=max(1, width))

    pad = max(1, s // 16)
    doc_w = int(s * 0.52)
    doc_h = int(s * 0.65)

    # Back document (offset right & down)
    ox, oy = int(s * 0.30), int(s * 0.08)
    rrect(
        (ox, oy, ox + doc_w, oy + doc_h),
        fill=(60, 100, 140, 220),
        outline=(100, 160, 220, 255),
        width=max(1, s // 40),
    )

    # Front document (offset left & down more)
    fx, fy = int(s * 0.12), int(s * 0.22)
    rrect(
        (fx, fy, fx + doc_w, fy + doc_h),
        fill=(30, 60, 100, 240),
        outline=(80, 140, 200, 255),
        width=max(1, s // 40),
    )

    # "Lines" on front document
    line_color = (160, 200, 240, 200)
    lw = max(1, s // 32)
    lx1 = fx + int(doc_w * 0.15)
    lx2 = fx + int(doc_w * 0.85)
    for i, frac in enumerate([0.25, 0.42, 0.59]):
        ly = fy + int(doc_h * frac)
        end_x = lx2 if i == 0 else lx2 - int(doc_w * 0.2)
        d.line((lx1, ly, end_x, ly), fill=line_color, width=lw)

    # Magnifying-glass circle (bottom-right)
    mag_r = int(s * 0.22)
    mag_cx = int(s * 0.72)
    mag_cy = int(s * 0.72)
    d.ellipse(
        (mag_cx - mag_r, mag_cy - mag_r, mag_cx + mag_r, mag_cy + mag_r),
        fill=(20, 40, 70, 200),
        outline=(100, 200, 255, 255),
        width=max(2, s // 20),
    )

    # Magnifying-glass handle
    hx = mag_cx + int(mag_r * 0.7)
    hy = mag_cy + int(mag_r * 0.7)
    hx2 = hx + int(s * 0.12)
    hy2 = hy + int(s * 0.12)
    d.line((hx, hy, hx2, hy2), fill=(100, 200, 255, 255), width=max(2, s // 16))

    return img


def generate_icon(output_path: str = "FileDuplicator.ico"):
    images = [_draw_icon(s) for s in SIZES]
    images[-1].save(
        output_path,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[:-1],
    )
    print(f"Icon saved to {output_path}")


if __name__ == "__main__":
    generate_icon()
