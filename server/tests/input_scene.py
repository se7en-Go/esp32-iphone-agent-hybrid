"""A focused field with stable surrounding context, independent of OCR."""

import io

from PIL import Image, ImageDraw


def input_frame(*, typed=False, replaced=False, focused=True):
    image = Image.new("RGB", (360, 780), (225, 225, 225))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 60, 340, 90), fill="black" if replaced else (60, 90, 120))
    draw.rectangle((20, 580, 340, 635), fill="white", outline=(120, 120, 120), width=2)
    if typed:
        draw.text((32, 600), "hello world", fill="black")
    if focused:
        draw.rectangle((105 if typed else 32, 596, 107 if typed else 34, 620), fill=(0, 122, 255))
    data = io.BytesIO()
    image.save(data, "PNG")
    return data.getvalue()
