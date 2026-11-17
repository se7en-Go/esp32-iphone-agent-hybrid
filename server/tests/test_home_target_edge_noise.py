"""Measured Home navigation may tolerate sparse stationary JPEG edge noise only."""

import io
import math
import time
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from test_home_probe_direction import menu_frame

from hidagent.freshness import FrameSnapshot
from hidagent.navigation_context import from_navigation
from hidagent.text_target import TargetChangedError, TextTargetGuard


def encoded(image):
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def scene(*, icon_noise=20, navigation=True):
    original = menu_frame()
    image = Image.open(io.BytesIO(original)).convert("RGB")
    observation = FrameSnapshot(original, time.time(), None, 1, True)
    context = from_navigation("home", observation)
    assert context is not None and context.kind == "menu"
    point = context.target.center[0], context.target.center[1] + .006
    cx, cy = point[0]*500, point[1]*1080
    bounds = tuple((math.floor if i < 2 else math.ceil)(v*(500 if i % 2 == 0 else 1080))
                   + (-6 if i < 2 else 6) for i, v in enumerate(context.target.box))
    gray = image.convert("L")

    def edge_points(box, count):
        points = []
        for y in range(box[1], box[3]):
            for x in range(box[0], box[2]):
                if x % 2 or y % 2 or math.hypot(x-cx, y-cy) <= 20:
                    continue
                low, high = gray.crop((x-1, y-1, x+2, y+2)).getextrema()
                if high-low >= 100:
                    points.append((x, y))
        assert len(points) >= count
        return points[:count]

    icon_points = edge_points(bounds, icon_noise)
    label_points = edge_points((265, 932, 320, 960), 8)
    current = image.copy()
    ImageDraw.Draw(current).ellipse((cx-12, cy-12, cx+12, cy+12), fill=(130, 130, 130))
    for point_xy in icon_points + label_points:
        pixel = current.getpixel(point_xy)
        current.putpixel(point_xy, tuple(value-30 if value >= 128 else value+30 for value in pixel))
    measured = encoded(current)
    screen = SimpleNamespace(frame=measured, updated=time.time(), frame_ts=None, generation=1,
                             live=True, recent_frames=())
    guard = TextTargetGuard(screen, original, context.target, observation=observation,
                            navigation_context=context if navigation else None)
    return guard, screen, point, measured, current, icon_points, bounds


def test_stationary_measured_home_ignores_only_bounded_icon_edge_noise():
    guard, _, point, measured, _, _, _ = scene()
    guard(cursor=(point, measured))


@pytest.mark.parametrize("proof", ["ordinary_tap", "missing", "mismatched", "outside_target"])
def test_noise_exception_requires_original_home_scope_and_current_measured_pointer(proof):
    guard, _, point, measured, _, _, _ = scene(navigation=proof != "ordinary_tap")
    cursor = (point, measured)
    if proof == "missing":
        cursor = None
    elif proof == "mismatched":
        cursor = (point, menu_frame())
    elif proof == "outside_target":
        cursor = ((point[0], .91), measured)
    with pytest.raises(TargetChangedError):
        guard(cursor=cursor)


def test_total_candidate_noise_budget_is_not_per_component():
    guard, _, point, measured, _, _, _ = scene(icon_noise=25)
    with pytest.raises(TargetChangedError):
        guard(cursor=(point, measured))


@pytest.mark.parametrize("edit", ["icon", "label", "panel"])
def test_real_icon_label_and_panel_edits_survive_measured_pointer_evidence(edit):
    guard, screen, point, _, current, _, bounds = scene()
    draw = ImageDraw.Draw(current)
    if edit == "icon":
        # The upper glyph edge is outside the acquired cursor crop.
        draw.rectangle((280, bounds[1]+5, 302, bounds[1]+9), fill="black")
    elif edit == "label":
        draw.rectangle((273, 937, 310, 946), fill="black")
    else:
        draw.rectangle((483, 780, 485, 815), fill="white")
    screen.frame = encoded(current)
    # Even a newly measured pointer cannot forgive a changed original icon,
    # label or panel. The target anchor remains the original observation.
    with pytest.raises(TargetChangedError):
        guard(cursor=(point, screen.frame))


def test_edit_after_measurement_cannot_be_filtered_as_old_edge_noise():
    guard, screen, point, measured, current, points, _ = scene()
    x, y = points[0]
    pixel = current.getpixel((x, y))
    current.putpixel((x, y), tuple(value+8 if value < 128 else value-8 for value in pixel))
    screen.frame = encoded(current)
    with pytest.raises(TargetChangedError):
        guard(cursor=(point, measured))


def test_full_component_crossing_icon_bounds_is_never_clipped_into_noise():
    guard, screen, point, _, current, _, bounds = scene()
    # A complete component is too large and extends beyond the permitted halo.
    # Taking only its inside fragment would hide a structural change.
    ImageDraw.Draw(current).line((bounds[0]-10, bounds[1]+2, bounds[0]+10, bounds[1]+2),
                                fill="white", width=2)
    screen.frame = encoded(current)
    with pytest.raises(TargetChangedError):
        guard(cursor=(point, screen.frame))
