"""Shared text geometry for the live preview, poster and exported video."""
from functools import lru_cache
import math
import unicodedata

from PIL import Image, ImageDraw, ImageFont


STYLE_KEYS = ("resolution", "font", "font_size", "min_font_size", "text_width", "text_x", "text_y",
              "stroke", "text_color", "stroke_color", "line_spacing", "letter_spacing",
              "paragraph_spacing", "text_align", "text_opacity", "text_rotation", "keep_text_in_frame")


def clusters(text):
    """Keep combining marks, emoji modifiers and ZWJ sequences together."""
    result = []
    for char in text:
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        join = (unicodedata.combining(char) or 0xFE00 <= code <= 0xFE0F or 0x1F3FB <= code <= 0x1F3FF
                or char == "\u200d" or (result and result[-1].endswith("\u200d"))
                or (regional and result and len(result[-1]) == 1 and 0x1F1E6 <= ord(result[-1]) <= 0x1F1FF))
        if result and join:
            result[-1] += char
        else:
            result.append(char)
    return result


def measure_line(text, font, stroke, tracking):
    if not text:
        return [], (0, 0, 0, 0)
    if tracking == 0:
        return [(0, text)], font.getbbox(text, anchor="ls", stroke_width=stroke)
    runs, bounds, prefix = [], None, ""
    for index, cluster in enumerate(clusters(text)):
        # Keep the font's pair kerning while adding tracking between clusters.
        x = font.getlength(prefix + cluster) - font.getlength(cluster) + index * tracking
        box = font.getbbox(cluster, anchor="ls", stroke_width=stroke)
        box = (x + box[0], box[1], x + box[2], box[3])
        bounds = box if bounds is None else (min(bounds[0], box[0]), min(bounds[1], box[1]), max(bounds[2], box[2]), max(bounds[3], box[3]))
        runs.append((x, cluster))
        prefix += cluster
    return runs, bounds


def wrap_paragraph(text, measure, maximum):
    result, line = [], ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if measure(candidate)[1][2] - measure(candidate)[1][0] <= maximum:
            line = candidate
            continue
        if line:
            result.append(line)
        line = ""
        for cluster in clusters(word):
            candidate = line + cluster
            if line and measure(candidate)[1][2] - measure(candidate)[1][0] > maximum:
                result.append(line)
                line = cluster
            else:
                line = candidate
    result.append(line)
    return result


def render_overlay(text, config, target):
    width = int(config["resolution"])
    height, factor = width * 16 // 9, width / 1080
    stroke = round(config["stroke"] * factor)
    tracking = config["letter_spacing"] * factor
    maximum = width * config["text_width"] / 100
    keep = config["keep_text_in_frame"]
    angle = config["text_rotation"]
    cosine, sine = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))

    def layout(size):
        font = ImageFont.truetype(config["font_path"], size)
        @lru_cache(maxsize=2048)
        def measure(value):
            return measure_line(value, font, stroke, tracking)
        ascent, descent = font.getmetrics()
        line_step = (ascent + descent) * config["line_spacing"] + stroke * 2
        rows, baseline = [], 0.0
        paragraphs = text.splitlines() or [text]
        for paragraph_index, paragraph in enumerate(paragraphs):
            for line in wrap_paragraph(paragraph, measure, maximum):
                runs, box = measure(line)
                rows.append({"text": line, "runs": runs, "bbox": box, "baseline": baseline})
                baseline += line_step
            if paragraph_index < len(paragraphs)-1:
                baseline += config["paragraph_spacing"] * factor
        block_width = max((row["bbox"][2]-row["bbox"][0] for row in rows), default=0)
        visible = [row for row in rows if row["text"]]
        top = min((row["baseline"] + row["bbox"][1] for row in visible), default=0)
        bottom = max((row["baseline"] + row["bbox"][3] for row in visible), default=1)
        block_height = max(1, bottom - top)
        rotated_width = block_width * cosine + block_height * sine
        rotated_height = block_height * cosine + block_width * sine
        fits = block_width <= maximum and (not keep or (rotated_width + 4 <= width and rotated_height + 4 <= height))
        # Keep memory bounded for free positioning with extreme text/spacing.
        fits = fits and block_height <= height * 2 and block_width <= width
        return {"font": font, "rows": rows, "width": block_width, "height": block_height,
                "top": top, "line_step": line_step, "fits": fits}

    lower = max(8, round(config["min_font_size"] * factor))
    upper = max(lower, round(config["font_size"] * factor))
    chosen, size = None, None
    # Binary search avoids rerendering every possible font size on each keystroke.
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = layout(middle)
        if candidate["fits"]:
            chosen, size, lower = candidate, middle, middle + 1
        else:
            upper = middle - 1
    if chosen is None:
        raise ValueError("Текст не помещается: увеличьте ширину, уменьшите интервалы или минимальный размер шрифта.")

    padding = 3
    tile = Image.new("RGBA", (max(1, math.ceil(chosen["width"]) + padding*2), max(1, math.ceil(chosen["height"]) + padding*2)))
    draw = ImageDraw.Draw(tile)
    line_boxes = []
    for row in chosen["rows"]:
        box = row["bbox"]
        row_width = box[2] - box[0]
        offset = 0 if config["text_align"] == "left" else chosen["width"] - row_width if config["text_align"] == "right" else (chosen["width"] - row_width) / 2
        x, y = padding + offset - box[0], padding + row["baseline"] - chosen["top"]
        for run_x, value in row["runs"]:
            draw.text((x + run_x, y), value, font=chosen["font"], anchor="ls", fill=config["text_color"],
                      stroke_width=stroke, stroke_fill=config["stroke_color"])
        line_boxes.append([round(offset, 2), round(row["baseline"] + box[1] - chosen["top"], 2),
                           round(offset + row_width, 2), round(row["baseline"] + box[3] - chosen["top"], 2)])
    if angle:
        tile = tile.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=True)
    ink = tile.getchannel("A").getbbox()
    if ink:
        tile = tile.crop(ink)
    if config["text_opacity"] < 100:
        opacity = config["text_opacity"] / 100
        tile.putalpha(tile.getchannel("A").point(lambda value: round(value * opacity)))
    center_x, center_y = width * config["text_x"] / 100, height * config["text_y"] / 100
    if keep:
        center_x = max(tile.width/2, min(center_x, width-tile.width/2))
        center_y = max(tile.height/2, min(center_y, height-tile.height/2))
    left, top = round(center_x-tile.width/2), round(center_y-tile.height/2)
    image = Image.new("RGBA", (width, height))
    image.alpha_composite(tile, (left, top))
    image.save(target)
    return {"font_size": size, "lines": len(chosen["rows"]), "bbox": image.getchannel("A").getbbox(),
            "width": width, "height": height, "position": {"x": (left+tile.width/2)/width*100, "y": (top+tile.height/2)/height*100},
            "limits": {"x": [tile.width/2/width*100, 100-tile.width/2/width*100] if keep else [0, 100],
                       "y": [tile.height/2/height*100, 100-tile.height/2/height*100] if keep else [0, 100]},
            "line_boxes": line_boxes, "line_step": round(chosen["line_step"], 2), "rotation": angle}
