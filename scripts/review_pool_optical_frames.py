"""Compact crops for every pool addition proposal; full frames remain beside them."""
import argparse
import json

from improve_ngt_light import CONFIG, folder
from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser(__doc__); parser.add_argument("--experiment", action="append", required=True)
    args = parser.parse_args(); cfg = json.loads(CONFIG.read_text())
    if set(args.experiment)-set(cfg["experiments"]):
        parser.error("Explicit pool trials required")
    for eid in args.experiment:
        out = folder(eid, cfg)
        cases = [r for r in json.loads((out/"parent_frame_cases.json").read_text()) if r["proposed_addition"]]
        for start in range(0, len(cases), 60):
            canvas = Image.new("RGB", (10*132, 6*156), "white"); draw = ImageDraw.Draw(canvas)
            for j, row in enumerate(cases[start:start+60]):
                with Image.open(out/row["image"]) as image:
                    px, py = row["candidate_xy"]
                    crop = image.crop((px-45, py-45, px+45, py+45)).resize((128, 128))
                x, y = j%10*132, j//10*156
                canvas.paste(crop, (x, y+24))
                draw.text((x+3, y+4), f"row {row['row_index']}", fill="black")
                draw.ellipse((x+62, y+86, x+66, y+90), outline="red")
            canvas.save(out/f"crops_{start//60:02}.jpg", quality=95)
        print(eid, len(cases), "proposal crops")


if __name__ == "__main__":
    main()
