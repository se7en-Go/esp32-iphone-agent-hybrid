"""Synthetic SpringBoard-like evidence, distinct from a mere wallpaper change."""

from PIL import ImageDraw


def draw_home(image, *, dock=True, chrome=False):
    draw = ImageDraw.Draw(image)
    w, h = image.size
    colors = ("#b12632", "#f2a516", "#274cac", "#338348")
    for row in range(3):
        for col, color in enumerate(colors):
            x, y, r = (.16 + col*.225)*w, (.20 + row*.145)*h, .067*w
            draw.rounded_rectangle((x-r, y-r, x+r, y+r), radius=.025*w, fill=color)
            draw.text((x-r, y+r+.012*h), "App", fill="black")
    if dock:
        draw.rounded_rectangle((.035*w, .83*h, .965*w, .975*h), radius=.05*w, fill="#e5e5ed")
        for col, color in enumerate(colors):
            x, y, r = (.16 + col*.225)*w, .905*h, .067*w
            draw.rounded_rectangle((x-r, y-r, x+r, y+r), radius=.025*w, fill=color)
    if chrome:
        draw.rectangle((0, .08*h, w, .14*h), fill="white")
        draw.line((0, .14*h, w, .14*h), fill="black", width=2)
        draw.text((.38*w, .1*h), "APP TITLE", fill="black")
