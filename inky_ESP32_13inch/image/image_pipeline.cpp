#include "image_pipeline.h"

#include "EPD_13in3e.h"

#include <PNGdec.h>
#include <JPEGDEC.h>
#include <esp_heap_caps.h>
#include <math.h>
#include <string.h>

namespace {

constexpr int kOutW = EPD_13IN3E_WIDTH;
constexpr int kOutH = EPD_13IN3E_HEIGHT;
constexpr size_t kOutBytes = static_cast<size_t>(kOutW) * kOutH / 2;

struct PaletteColor {
    uint8_t code;
    uint8_t r, g, b;
};

constexpr PaletteColor kPalette[] = {
    {EPD_13IN3E_BLACK, 0, 0, 0},
    {EPD_13IN3E_WHITE, 255, 255, 255},
    {EPD_13IN3E_YELLOW, 255, 255, 0},
    {EPD_13IN3E_RED, 255, 0, 0},
    {EPD_13IN3E_BLUE, 0, 0, 255},
    {EPD_13IN3E_GREEN, 0, 255, 0},
};

struct PipelineState {
    uint8_t *out = nullptr;
    int srcW = 0;
    int srcH = 0;
    float scale = 1.0f;
    float cropX = 0.0f;
    float cropY = 0.0f;
    int logicalW = kOutW;
    int logicalH = kOutH;
    uint16_t orientation = 0;
    uint8_t sourceOrientation = 1;
    int rawW = 0;
    int rawH = 0;
    uint16_t *pngScanline = nullptr;
    size_t pngScanlinePixels = 0;
};

PipelineState gPipe;
PNG gPngDecoder;

void initOutputBuffer(uint8_t *outBuffer) {
    memset(outBuffer, (EPD_13IN3E_WHITE << 4) | EPD_13IN3E_WHITE, kOutBytes);
}

void setupCropFill(int srcW, int srcH) {
    gPipe.srcW = srcW;
    gPipe.srcH = srcH;
    gPipe.scale = fmaxf(static_cast<float>(gPipe.logicalW) / srcW,
                        static_cast<float>(gPipe.logicalH) / srcH);
    const float scaledW = srcW * gPipe.scale;
    const float scaledH = srcH * gPipe.scale;
    gPipe.cropX = (scaledW - gPipe.logicalW) * 0.5f;
    gPipe.cropY = (scaledH - gPipe.logicalH) * 0.5f;
}

uint8_t quantizeRgb(uint8_t r, uint8_t g, uint8_t b) {
    uint32_t bestDist = UINT32_MAX;
    uint8_t bestCode = EPD_13IN3E_WHITE;
    for (const auto &c : kPalette) {
        const int dr = static_cast<int>(r) - c.r;
        const int dg = static_cast<int>(g) - c.g;
        const int db = static_cast<int>(b) - c.b;
        const uint32_t dist =
            static_cast<uint32_t>(dr * dr + dg * dg + db * db);
        if (dist < bestDist) {
            bestDist = dist;
            bestCode = c.code;
        }
    }
    return bestCode;
}

void writePackedPixel(int lx, int ly, uint8_t code) {
    if (!gPipe.out || lx < 0 || ly < 0 ||
        lx >= gPipe.logicalW || ly >= gPipe.logicalH) {
        return;
    }
    int ox = lx;
    int oy = ly;
    switch (gPipe.orientation) {
        case 90:
            ox = ly;
            oy = kOutH - 1 - lx;
            break;
        case 180:
            ox = kOutW - 1 - lx;
            oy = kOutH - 1 - ly;
            break;
        case 270:
            ox = kOutW - 1 - ly;
            oy = lx;
            break;
        default:
            break;
    }
    if (ox < 0 || oy < 0 || ox >= kOutW || oy >= kOutH) {
        return;
    }
    const size_t pixelIndex = static_cast<size_t>(oy) * kOutW + ox;
    const size_t byteIndex = pixelIndex / 2;
    if (pixelIndex & 1) {
        gPipe.out[byteIndex] = (gPipe.out[byteIndex] & 0xF0) | (code & 0x0F);
    } else {
        gPipe.out[byteIndex] = (gPipe.out[byteIndex] & 0x0F) | ((code & 0x0F) << 4);
    }
}

void writeSourcePixel(int sx, int sy, uint8_t r, uint8_t g, uint8_t b) {
    int tx = sx;
    int ty = sy;
    if (gPipe.sourceOrientation == 3) {
        tx = gPipe.rawW - 1 - sx;
        ty = gPipe.rawH - 1 - sy;
    } else if (gPipe.sourceOrientation == 6) {
        tx = gPipe.rawH - 1 - sy;
        ty = sx;
    } else if (gPipe.sourceOrientation == 8) {
        tx = sy;
        ty = gPipe.rawW - 1 - sx;
    }
    const int ox0 =
        static_cast<int>(floorf(tx * gPipe.scale - gPipe.cropX));
    const int ox1 =
        static_cast<int>(ceilf((tx + 1) * gPipe.scale - gPipe.cropX));
    const int oy0 =
        static_cast<int>(floorf(ty * gPipe.scale - gPipe.cropY));
    const int oy1 =
        static_cast<int>(ceilf((ty + 1) * gPipe.scale - gPipe.cropY));
    const uint8_t code = quantizeRgb(r, g, b);

    for (int oy = oy0; oy < oy1; ++oy) {
        for (int ox = ox0; ox < ox1; ++ox) {
            writePackedPixel(ox, oy, code);
        }
    }
}

int jpegDrawCallback(JPEGDRAW *draw) {
    if (!gPipe.out) {
        return 0;
    }

    for (int row = 0; row < draw->iHeight; ++row) {
        const int sy = draw->y + row;
        const uint16_t *src =
            draw->pPixels + static_cast<size_t>(row) * draw->iWidth;
        for (int col = 0; col < draw->iWidth; ++col) {
            const int sx = draw->x + col;
            const uint16_t rgb565 = src[col];
            const uint8_t r =
                static_cast<uint8_t>(((rgb565 >> 11) & 0x1F) * 255 / 31);
            const uint8_t g =
                static_cast<uint8_t>(((rgb565 >> 5) & 0x3F) * 255 / 63);
            const uint8_t b =
                static_cast<uint8_t>((rgb565 & 0x1F) * 255 / 31);
            writeSourcePixel(sx, sy, r, g, b);
        }
    }
    return 1;
}

int pngDrawCallback(PNGDRAW *draw) {
    if (!gPipe.out) {
        return 0;
    }

    if (!gPipe.pngScanline ||
        static_cast<size_t>(draw->iWidth) > gPipe.pngScanlinePixels) {
        return 0;
    }
    gPngDecoder.getLineAsRGB565(draw, gPipe.pngScanline,
                                PNG_RGB565_LITTLE_ENDIAN, 0xFFFFFFFF);

    const int sy = draw->y;
    for (int x = 0; x < draw->iWidth; ++x) {
        const uint16_t rgb565 = gPipe.pngScanline[x];
        const uint8_t r =
            static_cast<uint8_t>(((rgb565 >> 11) & 0x1F) * 255 / 31);
        const uint8_t g =
            static_cast<uint8_t>(((rgb565 >> 5) & 0x3F) * 255 / 63);
        const uint8_t b = static_cast<uint8_t>((rgb565 & 0x1F) * 255 / 31);
        writeSourcePixel(x, sy, r, g, b);
    }
    return 1;
}

int scaleDivisor(int scaleType) {
    if (scaleType <= 1) {
        return 1;
    }
    return scaleType;
}

int pickJpegScale(int w, int h) {
    if (static_cast<int64_t>(w) * h > static_cast<int64_t>(kOutW) * kOutH * 4) {
        return JPEG_SCALE_EIGHTH;
    }
    if (static_cast<int64_t>(w) * h > static_cast<int64_t>(kOutW) * kOutH * 2) {
        return JPEG_SCALE_QUARTER;
    }
    if (static_cast<int64_t>(w) * h > static_cast<int64_t>(kOutW) * kOutH) {
        return JPEG_SCALE_HALF;
    }
    return 0;
}

bool endsWithIgnoreCase(const char *name, const char *suffix) {
    if (!name || !suffix) {
        return false;
    }
    const size_t nameLen = strlen(name);
    const size_t suffixLen = strlen(suffix);
    if (suffixLen > nameLen) {
        return false;
    }
    for (size_t i = 0; i < suffixLen; ++i) {
        char a = name[nameLen - suffixLen + i];
        char b = suffix[i];
        if (a >= 'A' && a <= 'Z') {
            a = static_cast<char>(a - 'A' + 'a');
        }
        if (b >= 'A' && b <= 'Z') {
            b = static_cast<char>(b - 'A' + 'a');
        }
        if (a != b) {
            return false;
        }
    }
    return true;
}

uint16_t readExif16(const uint8_t *p, bool little) {
    return little ? static_cast<uint16_t>(p[0] | (p[1] << 8))
                  : static_cast<uint16_t>((p[0] << 8) | p[1]);
}

uint32_t readExif32(const uint8_t *p, bool little) {
    if (little) {
        return static_cast<uint32_t>(p[0]) |
               (static_cast<uint32_t>(p[1]) << 8) |
               (static_cast<uint32_t>(p[2]) << 16) |
               (static_cast<uint32_t>(p[3]) << 24);
    }
    return (static_cast<uint32_t>(p[0]) << 24) |
           (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) | p[3];
}

uint8_t jpegExifOrientation(const uint8_t *data, size_t len) {
    if (!data || len < 16 || data[0] != 0xFF || data[1] != 0xD8) return 1;
    size_t pos = 2;
    while (pos + 4 < len && data[pos] == 0xFF) {
        const uint8_t marker = data[pos + 1];
        if (marker == 0xDA || marker == 0xD9) break;
        const uint16_t segmentLength =
            static_cast<uint16_t>((data[pos + 2] << 8) | data[pos + 3]);
        if (segmentLength < 2 || pos + 2 + segmentLength > len) break;
        const uint8_t *payload = data + pos + 4;
        const size_t payloadLength = segmentLength - 2;
        if (marker == 0xE1 && payloadLength >= 14 &&
            memcmp(payload, "Exif\0\0", 6) == 0) {
            const uint8_t *tiff = payload + 6;
            const size_t tiffLength = payloadLength - 6;
            const bool little = tiff[0] == 'I' && tiff[1] == 'I';
            const bool big = tiff[0] == 'M' && tiff[1] == 'M';
            if (!little && !big) return 1;
            const uint32_t ifdOffset = readExif32(tiff + 4, little);
            if (ifdOffset + 2 > tiffLength) return 1;
            const uint8_t *ifd = tiff + ifdOffset;
            const uint16_t count = readExif16(ifd, little);
            if (ifdOffset + 2 + static_cast<size_t>(count) * 12 > tiffLength) {
                return 1;
            }
            for (uint16_t i = 0; i < count; ++i) {
                const uint8_t *entry = ifd + 2 + i * 12;
                if (readExif16(entry, little) != 0x0112) continue;
                const uint16_t orientation = readExif16(entry + 8, little);
                return orientation == 3 || orientation == 6 || orientation == 8
                           ? static_cast<uint8_t>(orientation)
                           : 1;
            }
        }
        pos += 2 + segmentLength;
    }
    return 1;
}

bool decodeJpegToBuffer(const uint8_t *data, size_t len, uint8_t *outBuffer) {
    JPEGDEC jpeg;
    if (jpeg.openRAM(const_cast<uint8_t *>(data), static_cast<int>(len),
                     jpegDrawCallback) != 1) {
        return false;
    }

    const int fullW = jpeg.getWidth();
    const int fullH = jpeg.getHeight();
    if (fullW <= 0 || fullH <= 0) {
        jpeg.close();
        return false;
    }

    const int scale = pickJpegScale(fullW, fullH);
    const int div = scaleDivisor(scale);
    gPipe.rawW = (fullW + div - 1) / div;
    gPipe.rawH = (fullH + div - 1) / div;
    gPipe.sourceOrientation = jpegExifOrientation(data, len);
    const bool sourceLandscape =
        gPipe.sourceOrientation == 6 || gPipe.sourceOrientation == 8;
    setupCropFill(sourceLandscape ? gPipe.rawH : gPipe.rawW,
                  sourceLandscape ? gPipe.rawW : gPipe.rawH);
    gPipe.out = outBuffer;
    initOutputBuffer(outBuffer);
    jpeg.setUserPointer(&gPipe);

    const bool ok = (jpeg.decode(0, 0, scale) == 1);
    jpeg.close();
    gPipe.out = nullptr;
    return ok;
}

bool decodePngToBuffer(const uint8_t *data, size_t len, uint8_t *outBuffer) {
    if (gPngDecoder.openRAM(const_cast<uint8_t *>(data), static_cast<int>(len),
                            pngDrawCallback) != 0) {
        return false;
    }

    const int srcW = gPngDecoder.getWidth();
    const int srcH = gPngDecoder.getHeight();
    if (srcW <= 0 || srcH <= 0) {
        gPngDecoder.close();
        return false;
    }

    gPipe.rawW = srcW;
    gPipe.rawH = srcH;
    gPipe.sourceOrientation = 1;
    setupCropFill(srcW, srcH);
    gPipe.pngScanline = static_cast<uint16_t *>(
        heap_caps_malloc(static_cast<size_t>(srcW) * sizeof(uint16_t),
                         MALLOC_CAP_SPIRAM));
    if (!gPipe.pngScanline) {
        gPipe.pngScanline = static_cast<uint16_t *>(
            malloc(static_cast<size_t>(srcW) * sizeof(uint16_t)));
    }
    if (!gPipe.pngScanline) {
        gPngDecoder.close();
        return false;
    }
    gPipe.pngScanlinePixels = static_cast<size_t>(srcW);
    gPipe.out = outBuffer;
    initOutputBuffer(outBuffer);

    const bool ok = (gPngDecoder.decode(&gPipe, 0) == 0);
    gPngDecoder.close();
    gPipe.out = nullptr;
    heap_caps_free(gPipe.pngScanline);
    gPipe.pngScanline = nullptr;
    gPipe.pngScanlinePixels = 0;
    return ok;
}

}  // namespace

bool imageDecodeCropFill(const uint8_t *data, size_t len, const char *filename,
                         uint8_t *outBuffer, size_t outCapacity,
                         size_t *outLen, uint16_t orientation) {
    if (!data || !filename || !outBuffer || !outLen ||
        outCapacity < kOutBytes) {
        return false;
    }

    if (orientation != 0 && orientation != 90 &&
        orientation != 180 && orientation != 270) {
        orientation = 0;
    }
    gPipe.orientation = orientation;
    gPipe.logicalW = (orientation == 90 || orientation == 270) ? kOutH : kOutW;
    gPipe.logicalH = (orientation == 90 || orientation == 270) ? kOutW : kOutH;

    bool decoded = false;
    if (endsWithIgnoreCase(filename, ".jpg") ||
        endsWithIgnoreCase(filename, ".jpeg")) {
        decoded = decodeJpegToBuffer(data, len, outBuffer);
    } else if (endsWithIgnoreCase(filename, ".png")) {
        decoded = decodePngToBuffer(data, len, outBuffer);
    } else {
        Serial.printf("Unsupported image type: %s\n", filename);
        return false;
    }

    if (!decoded) {
        Serial.println("Image decode failed");
        return false;
    }

    Serial.printf("Crop-fill decode complete for %s (%dx%d logical, %u deg)\n",
                  filename, gPipe.logicalW, gPipe.logicalH,
                  static_cast<unsigned>(orientation));
    *outLen = kOutBytes;
    return true;
}
