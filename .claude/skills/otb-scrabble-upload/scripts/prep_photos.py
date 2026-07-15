#!/usr/bin/env python3
"""Deterministic photo preprocessing for OTB game reconstruction (SKILL.md Step 1).

Replaces the ad-hoc PIL code the model used to write per run. Two modes:

Scoresheet:
  python3 prep_photos.py scoresheet IMG_1520.jpeg --outdir OUT [--rotate 90]
      [--zoom x0,y0,x1,y1 --label turn12]
  - rotates upright (default: 90° if the photo is landscape; if the output
    reads upside-down, rerun with --rotate 270), writes:
      sheet_full.png            the rotated sheet
      sheet_left.png/right.png  left/right halves at 2x (the two play columns)
  - --zoom crops a region (coords in the ROTATED image, as read from
    sheet_full.png) at 3x for re-reading an ambiguous cell; --label names it.

Board:
  python3 prep_photos.py board IMG_1521.jpeg --outdir OUT --corners-probe
  - writes board_coords.png: the photo overlaid with a 250px coordinate grid,
    plus corner_nw/sw/se/ne.png crops. Read the four OUTER corners of the
    playing grid (pixel coords) off these, then:
  python3 prep_photos.py board IMG_1521.jpeg --outdir OUT \
      --corners NWx,NWy,SWx,SWy,SEx,SEy,NEx,NEy
  - writes board_grid.png: perspective-warped to 1500x1500 with red gridlines
    every 100px and row (1-15) / column (A-O) labels. Expect ±0.3 cell drift:
    rows/words are reliable, exact column offsets are NOT — leave placement
    to the solver. Premium squares are parallax-free anchors.
"""
import sys, os, argparse
from PIL import Image, ImageDraw


def fit(img, max_side):
    """Scale (up or down) so the longest side is max_side."""
    s = max_side / max(img.width, img.height)
    if abs(s - 1) < 0.05:
        return img
    return img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)


def save(img, outdir, name):
    path = os.path.join(outdir, name)
    img.save(path)
    print(f"wrote {path} ({img.width}x{img.height})")


def scoresheet(args):
    img = Image.open(args.image)
    rot = args.rotate
    if rot is None:
        rot = 90 if img.width > img.height else 0
    if rot:
        img = img.transpose({90: Image.ROTATE_90, 180: Image.ROTATE_180,
                             270: Image.ROTATE_270}[rot])
    print(f"rotation: {rot}° — CHECK sheet_full.png: if text is sideways rerun with "
          "--rotate 90 (heads pointing right/east) or 270 (heads left/west); "
          "if upside-down, --rotate 180")
    if args.zoom:
        x0, y0, x1, y1 = map(int, args.zoom.split(','))
        crop = img.crop((x0, y0, x1, y1))
        save(fit(crop, 1500), args.outdir, f"zoom_{args.label or f'{x0}_{y0}'}.png")
        return
    save(fit(img, 2000), args.outdir, 'sheet_full.png')
    # halves at full source resolution, capped: vision reads ~1500px, so a
    # half-sheet at ~1400 wide is the sharpest a single Read can use
    for name, box in (('left', (0, 0, img.width // 2, img.height)),
                      ('right', (img.width // 2, 0, img.width, img.height))):
        save(fit(img.crop(box), 2800), args.outdir, f'sheet_{name}.png')


def board(args):
    img = Image.open(args.image)
    if args.corners_probe:
        marked = img.copy()
        d = ImageDraw.Draw(marked)
        for x in range(0, marked.width, 250):
            d.line([(x, 0), (x, marked.height)], fill='red', width=2)
            d.text((x + 4, 4), str(x), fill='red')
        for y in range(0, marked.height, 250):
            d.line([(0, y), (marked.width, y)], fill='red', width=2)
            d.text((4, y + 4), str(y), fill='red')
        save(marked, args.outdir, 'board_coords.png')
        w, h = img.width, img.height
        for name, box in (('nw', (0, 0, w // 2, h // 2)),
                          ('sw', (0, h // 2, w // 2, h)),
                          ('se', (w // 2, h // 2, w, h)),
                          ('ne', (w // 2, 0, w, h // 2))):
            save(img.crop(box), args.outdir, f'corner_{name}.png')
        print("read the grid's four OUTER corner pixel coords off board_coords.png "
              "(the corner_*.png crops help), then rerun with --corners")
        return
    if not args.corners:
        sys.exit('board mode needs --corners-probe or --corners')
    q = tuple(map(float, args.corners.split(',')))
    if len(q) != 8:
        sys.exit('--corners needs 8 numbers: NWx,NWy,SWx,SWy,SEx,SEy,NEx,NEy')
    warped = img.transform((1500, 1500), Image.QUAD, q, Image.BICUBIC)
    d = ImageDraw.Draw(warped)
    for i in range(16):
        d.line([(i * 100, 0), (i * 100, 1500)], fill='red', width=2)
        d.line([(0, i * 100), (1500, i * 100)], fill='red', width=2)
    for i in range(15):
        d.text((i * 100 + 42, 2), chr(65 + i), fill='red')      # columns A-O
        d.text((2, i * 100 + 42), str(i + 1), fill='red')       # rows 1-15
    save(warped, args.outdir, 'board_grid.png')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['scoresheet', 'board'])
    ap.add_argument('image')
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--rotate', type=int, choices=[0, 90, 180, 270])
    ap.add_argument('--zoom')
    ap.add_argument('--label')
    ap.add_argument('--corners-probe', action='store_true')
    ap.add_argument('--corners')
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    scoresheet(args) if args.mode == 'scoresheet' else board(args)


if __name__ == '__main__':
    main()
