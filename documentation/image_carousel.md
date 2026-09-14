# Ad Carousel — Fetching Images

Digital Engine exposes a read-only ad carousel over HTTPS. Downstream applications can list available creatives and fetch each image by filename.

## Base URL

```
https://digitalengine.leporia.net/ad_carousel
```

## Fetch the image index

Send a `GET` request to the carousel root:

```http
GET /ad_carousel HTTP/1.1
Host: digitalengine.leporia.net
```

Example with `curl`:

```sh
curl -sS https://digitalengine.leporia.net/ad_carousel
```

### Response

- **Status:** `200 OK`
- **Content-Type:** `application/json`
- **Cache-Control:** `public, max-age=60`

The body is a JSON array produced by nginx autoindex. Each entry describes one file in the carousel directory:

```json
[
  {
    "name": "summer-promo.jpg",
    "type": "file",
    "mtime": "Thu, 10 Sep 2026 12:34:56 GMT"
  },
  {
    "name": "brand-banner.png",
    "type": "file",
    "mtime": "Thu, 10 Sep 2026 12:35:10 GMT"
  }
]
```

When the directory is empty, the response is an empty array:

```json
[]
```

Hidden files (names starting with `.`) are not included in the index.

### Filtering to images

The index may contain any uploaded filename. Downstream clients should keep entries whose `type` is `"file"` and whose `name` ends with a supported image extension, for example:

- `.jpg`, `.jpeg`
- `.png`
- `.gif`
- `.webp`
- `.svg`

## Fetch an individual image

Each indexed file is available at:

```
https://digitalengine.leporia.net/ad_carousel/<filename>
```

Example:

```sh
curl -sS -O https://digitalengine.leporia.net/ad_carousel/summer-promo.jpg
```

Or in HTML:

```html
<img src="https://digitalengine.leporia.net/ad_carousel/summer-promo.jpg" alt="Summer promo">
```

Image responses use the correct MIME type for the file extension (for example `image/jpeg`, `image/png`).

## Typical downstream workflow

1. `GET https://digitalengine.leporia.net/ad_carousel`
2. Parse the JSON array.
3. Keep file entries with image extensions.
4. Build absolute URLs: `https://digitalengine.leporia.net/ad_carousel/` + `name`
5. Fetch or display each image URL.

### JavaScript example

```javascript
const BASE = "https://digitalengine.leporia.net/ad_carousel";
const IMAGE_EXT = /\.(jpe?g|png|gif|webp|svg)$/i;

const entries = await fetch(BASE).then((r) => r.json());
const images = entries
  .filter((entry) => entry.type === "file" && IMAGE_EXT.test(entry.name))
  .map((entry) => `${BASE}/${encodeURIComponent(entry.name)}`);
```

### Python example

```python
import re
from urllib.parse import quote

import httpx

BASE = "https://digitalengine.leporia.net/ad_carousel"
IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg)$", re.I)

entries = httpx.get(BASE, timeout=10).json()
images = [
    f"{BASE}/{quote(entry['name'])}"
    for entry in entries
    if entry.get("type") == "file" and IMAGE_EXT.search(entry["name"])
]
```

## Uploading creatives

Images are served from the host directory:

```
/mnt/dockerprojects/digital_engine/ad_carousel/
```

Copy or sync image files into that folder. New files appear in the index on the next request (responses may be cached for up to 60 seconds).

There is no upload API; file placement on the host (or a mapped drive pointing at that path) is the supported workflow.

## Notes

- The carousel is read-only from the public URL.
- Filenames are case-sensitive in URLs.
- Use `encodeURIComponent` (or equivalent) when building URLs from index names that contain spaces or special characters.
- Re-fetch the index periodically if your application needs to pick up newly added or removed ads.
