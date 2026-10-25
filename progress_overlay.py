"""Small, animated RGBA progress overlay; the finish time is fixed at 80%."""
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont
import typography

FINISH = .8
STYLE_KEYS = ("resolution", "font", "progress_enabled", "progress_text", "progress_width", "progress_y",
              "progress_height", "progress_font_size", "progress_gap", "progress_color", "progress_track_color",
              "progress_text_color", "progress_easing")


def fraction(video_fraction, power=2.2):
    t = max(0, min(1, video_fraction / FINISH))
    return 1 - (1-t)**power


def fill_width(width, video_fraction, power=2.2):
    return width if video_fraction >= FINISH else min(width-1, width * fraction(video_fraction, power))


def width_expression(width, duration, power):
    finish = float(duration) * FINISH
    return f"if(gte(T,{finish:.9f}),{width},min({width-1},{width}*(1-pow(1-clip(T/{finish:.9f},0,1),{float(power):.6f}))))"


def alpha_filter(width, duration, power):
    # Work only on the narrow fill strip, not on every pixel of the video.
    expression = width_expression(width, duration, power)
    return f"format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*clip(({expression})-X,0,1)'"


def artwork(config):
    frame_width = int(config["resolution"])
    frame_height, scale = frame_width * 16 // 9, frame_width / 1080
    width = max(20, round(frame_width * config["progress_width"] / 100))
    bar_height = max(3, round(config["progress_height"] * scale))
    gap = round(config["progress_gap"] * scale)
    text = config["progress_text"].strip()
    caption, line_step, font, stroke = [], 0, None, max(1, round(2*scale))
    if text:
        for size in range(max(12, round(config["progress_font_size"]*scale)), max(10, round(18*scale))-1, -1):
            font = ImageFont.truetype(config["font_path"], size)
            def measure(value):
                return typography.measure_line(value, font, stroke, 0)
            caption = [line for paragraph in text.splitlines() for line in typography.wrap_paragraph(paragraph, measure, width-8*scale)]
            if len(caption) <= 3 and all(measure(line)[1][2]-measure(line)[1][0] <= width-4*scale for line in caption):
                break
        else:
            raise ValueError("Подпись под шкалой слишком длинная: сократите её или увеличьте ширину шкалы.")
        ascent, descent = font.getmetrics()
        line_step = math.ceil((ascent+descent)*1.08) + stroke*2
    height = bar_height + (gap+len(caption)*line_step+stroke*2 if caption else 0)
    base = Image.new("RGBA", (width, height))
    # Supersample rounded caps so the small strip has clean edges at 720p too.
    def pill(color):
        image = Image.new("RGBA", (width*4, bar_height*4))
        ImageDraw.Draw(image).rounded_rectangle((0,0,image.width-1,image.height-1), radius=bar_height*2, fill=color)
        return image.resize((width,bar_height), Image.Resampling.LANCZOS)
    base.alpha_composite(pill(config["progress_track_color"] + "dc"))
    fill = pill(config["progress_color"])
    draw = ImageDraw.Draw(base)
    for index, line in enumerate(caption):
        box = font.getbbox(line or " ", stroke_width=stroke)
        x = (width-(box[2]-box[0]))/2-box[0]
        y = bar_height+gap+stroke+index*line_step-box[1]
        draw.text((x,y),line,font=font,fill=config["progress_text_color"],stroke_width=stroke,stroke_fill="#000000")
    y = round(frame_height*config["progress_y"]/100)
    y = max(0,min(y,frame_height-height))
    geometry = {"x": (frame_width-width)//2,"y":y,"width":width,"height":height,"bar_height":bar_height,
                "frame_width":frame_width,"frame_height":frame_height,"finish_fraction":FINISH,"easing":config["progress_easing"]}
    return base, fill, geometry


def save(config, prefix):
    base, fill, geometry = artwork(config)
    prefix = Path(prefix)
    base_path, fill_path = prefix.with_name(prefix.name+"_base.png"), prefix.with_name(prefix.name+"_fill.png")
    base.save(base_path)
    fill.save(fill_path)
    return {"base":str(base_path),"fill":str(fill_path),"geometry":geometry}


def frame(assets, video_fraction=0):
    with Image.open(assets["base"]) as source:
        result = source.copy()
    with Image.open(assets["fill"]) as source:
        fill = source.copy()
    pixels = math.floor(fill_width(fill.width, video_fraction, assets["geometry"]["easing"]))
    if pixels > 0:
        result.alpha_composite(fill.crop((0,0,pixels,fill.height)))
    return result
