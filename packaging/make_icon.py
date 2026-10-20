"""Иконка приложения: SF Symbol на градиенте → .icns (нужен pyobjc, запускать на Mac)."""
import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSGradient, NSGraphicsContext,
                    NSImage, NSImageSymbolConfiguration, NSMakeRect, NSPNGFileType)


def render(px: int) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    inset = px * 0.1  # отступ как у иконок macOS
    r = NSMakeRect(inset, inset, px - 2 * inset, px - 2 * inset)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, px * 0.18, px * 0.18)
    NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.33, 0.36, 0.95, 1),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.62, 0.30, 0.90, 1)).drawInBezierPath_angle_(path, -90)
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(px * 0.42, 5) \
        .configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_([NSColor.whiteColor()]))
    sym = NSImage.imageWithSystemSymbolName_accessibilityDescription_("captions.bubble.fill", None) \
        .imageWithSymbolConfiguration_(cfg)
    w, h = sym.size()
    sym.drawInRect_(NSMakeRect((px - w) / 2, (px - h) / 2, w, h))
    NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))


out = Path(sys.argv[1])
with tempfile.TemporaryDirectory() as t:
    iconset = Path(t) / "icon.iconset"
    iconset.mkdir()
    for s in (16, 32, 128, 256, 512):
        (iconset / f"icon_{s}x{s}.png").write_bytes(render(s))
        (iconset / f"icon_{s}x{s}@2x.png").write_bytes(render(s * 2))
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
print(out)
