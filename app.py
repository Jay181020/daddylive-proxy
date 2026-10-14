#!/usr/bin/env python3
"""
=============================================================================
LetsWatch Roku - DaddyLive 24/7 Cloud Proxy (Render / Koyeb / Standalone)
=============================================================================
"""

import os
import sys
import time
import json
import zlib
import gzip
import struct
import urllib.request
import urllib.parse
import re
import threading

PORT = int(os.environ.get("PORT", 8989))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(BASE_DIR, "daddylive_channels.json")
if not os.path.exists(CHANNELS_FILE):
    CHANNELS_FILE = os.path.join(BASE_DIR, "letswatch-roku", "assets", "daddylive_channels.json")

def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Fast Byte-Level Image Deobfuscator (PNG Paeth + GZIP -> MPEG-TS)
# ---------------------------------------------------------------------------

def paeth(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c

def unwrap_stream_segment(bytes_data):
    if not bytes_data or len(bytes_data) < 16:
        return None

    # Check 1: webpExifTS (RIFF ... WEBP)
    if bytes_data[0:4] == b'RIFF' and bytes_data[8:12] == b'WEBP':
        off = 12
        while off + 8 <= len(bytes_data):
            tag = bytes_data[off:off+4]
            n = struct.unpack('<I', bytes_data[off+4:off+8])[0]
            off += 8
            if off + n > len(bytes_data):
                break
            if tag == b'EXIF':
                data = bytes_data[off:off+n]
                if len(data) >= 188 and data[0] == 0x47 and data[188] == 0x47:
                    return data
            off += n + (n & 1)

    # Check 2: pngIendTS (MPEG-TS directly trailing IEND chunk)
    if bytes_data[0] == 0x89 and bytes_data[1] == 0x50:
        off = 8
        while off + 8 <= len(bytes_data):
            length = struct.unpack('>I', bytes_data[off:off+4])[0]
            if length > len(bytes_data) - off - 12:
                break
            chunk_type = bytes_data[off+4:off+8]
            off += 8 + length + 4
            if chunk_type == b'IEND':
                if off < len(bytes_data) and bytes_data[off] == 0x47 and off + 188 < len(bytes_data) and bytes_data[off+188] == 0x47:
                    return bytes_data[off:]
                break

    # Check 3: Full PNG Paeth Scanline Reconstruction
    if len(bytes_data) >= 8 and bytes_data[0:8] == b'\x89PNG\r\n\x1a\n':
        w, h, depth, ctype = 0, 0, 0, 0
        idat_chunks = []
        off = 8
        while off + 8 <= len(bytes_data):
            length = struct.unpack('>I', bytes_data[off:off+4])[0]
            tag = bytes_data[off+4:off+8]
            off += 8
            if off + length > len(bytes_data):
                break
            if tag == b'IHDR' and length >= 13:
                w, h = struct.unpack('>II', bytes_data[off:off+8])
                depth = bytes_data[off+8]
                ctype = bytes_data[off+9]
            elif tag == b'IDAT':
                idat_chunks.append(bytes_data[off:off+length])
            elif tag == b'IEND':
                break
            off += length + 4

        if w > 0 and h > 0 and idat_chunks:
            bpp = 3 if ctype == 2 else (4 if ctype == 6 else 3)
            stride = w * bpp
            raw_idat = b''.join(idat_chunks)
            try:
                decompressed = zlib.decompress(raw_idat)
            except Exception:
                decompressed = None

            if decompressed and len(decompressed) >= h * (stride + 1):
                rgb = bytearray(w * h * 3)
                prev = bytearray(stride)
                src_off = 0
                dst = 0

                for y in range(h):
                    ftype = decompressed[src_off]
                    src_off += 1
                    row = decompressed[src_off : src_off + stride]
                    src_off += stride
                    recon = bytearray(stride)

                    if ftype == 0:
                        recon[:] = row
                    elif ftype == 1:
                        for i in range(bpp):
                            recon[i] = row[i]
                        for i in range(bpp, stride):
                            recon[i] = (row[i] + recon[i - bpp]) & 0xFF
                    elif ftype == 2:
                        for i in range(stride):
                            recon[i] = (row[i] + prev[i]) & 0xFF
                    elif ftype == 3:
                        for i in range(bpp):
                            recon[i] = (row[i] + (prev[i] >> 1)) & 0xFF
                        for i in range(bpp, stride):
                            recon[i] = (row[i] + ((recon[i - bpp] + prev[i]) >> 1)) & 0xFF
                    elif ftype == 4:
                        for i in range(bpp):
                            recon[i] = (row[i] + paeth(0, prev[i], 0)) & 0xFF
                        for i in range(bpp, stride):
                            recon[i] = (row[i] + paeth(recon[i - bpp], prev[i], prev[i - bpp])) & 0xFF
                    else:
                        recon[:] = row

                    if ctype == 2:
                        rgb[dst : dst + stride] = recon
                        dst += stride
                    else:
                        for i in range(0, stride, 4):
                            rgb[dst] = recon[i]
                            rgb[dst+1] = recon[i+1]
                            rgb[dst+2] = recon[i+2]
                            dst += 3
                    prev = recon

                TPIX = bytes([84, 73, 75, 84, 73, 75, 80, 88])
                if len(rgb) >= 12 and bytes(rgb[0:8]) == TPIX:
                    n = struct.unpack('>I', rgb[8:12])[0]
                    if 12 + n <= len(rgb):
                        gz = rgb[12 : 12 + n]
                        if len(gz) >= 2 and gz[0] == 0x1f and gz[1] == 0x8b:
                            try:
                                ts = gzip.decompress(gz)
                                if len(ts) > 0 and ts[0] == 0x47:
                                    return ts
                            except Exception:
                                pass

    # Check 4: Fallback Tokens
    TRAW = bytes([84, 73, 75, 84, 73, 75, 82, 65, 87])
    idx = bytes_data.find(TRAW)
    if idx != -1:
        ts = bytes_data[idx + len(TRAW):]
        if len(ts) and ts[0] == 0x47:
            return ts

    TSGZ = bytes([84, 73, 75, 84, 73, 75, 84, 83, 71, 90])
    idx = bytes_data.find(TSGZ)
    if idx != -1:
        try:
            return gzip.decompress(bytes_data[idx + len(TSGZ):])
        except Exception:
            pass

    return None

# ---------------------------------------------------------------------------
# DaddyLive Engine Core
# ---------------------------------------------------------------------------

class DaddyLiveProxy:
    def __init__(self):
        self.channel_urls = {}
        self.segment_cache = {}
        self.cache_lock = threading.Lock()
        self.max_cache_entries = 40
        self.total_segments_served = 0
        self.catalog_data = None
        self.load_catalog()

    def load_catalog(self):
        if os.path.exists(CHANNELS_FILE):
            try:
                with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                    self.catalog_data = json.load(f)
                log(f"[CATALOG] Loaded channels catalog ({len(self.catalog_data)} categories)")
            except Exception as e:
                log(f"[WARN] Failed to load {CHANNELS_FILE}: {e}")

    def resolve_channel(self, channel_id):
        now = time.time()
        if channel_id in self.channel_urls:
            cached_url, exp = self.channel_urls[channel_id]
            if now < exp:
                return cached_url

        url = f"https://dembed.st/premiumtv/daddy.php?id={channel_id}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Referer': f'https://dlive.sx/stream/stream-{channel_id}.php',
            'Origin': 'https://dlive.sx'
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                content = resp.read().decode('utf-8', errors='ignore')
                matches = re.findall(r'SRC\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]', content)
                if matches:
                    src_url = matches[0]
                    self.channel_urls[channel_id] = (src_url, now + 900)
                    log(f"[RESOLVED] Channel {channel_id} -> {src_url}")
                    return src_url
        except Exception as e:
            log(f"[ERROR] Resolving channel {channel_id}: {e}")
        return None

    def get_manifest(self, channel_id, host_header, proto=""):
        src_url = self.resolve_channel(channel_id)
        if not src_url:
            return None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Referer': f'https://dembed.st/premiumtv/daddy.php?id={channel_id}',
            'Origin': 'https://dembed.st'
        }
        req = urllib.request.Request(src_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                content = resp.read().decode('utf-8', errors='ignore')

            rewritten_lines = []
            segment_urls_to_prefetch = []
            scheme = proto if proto else ("https" if ":443" in host_header or not ":" in host_header else "http")

            for line in content.splitlines():
                line_str = line.strip()
                if line_str.startswith('http') or '.image' in line_str:
                    quoted_url = urllib.parse.quote_plus(line_str)
                    rewritten_url = f"{scheme}://{host_header}/seg?url={quoted_url}&cid={channel_id}"
                    rewritten_lines.append(rewritten_url)
                    segment_urls_to_prefetch.append((line_str, channel_id))
                else:
                    rewritten_lines.append(line)

            if segment_urls_to_prefetch:
                for seg_url, cid in segment_urls_to_prefetch[-2:]:
                    threading.Thread(target=self.prefetch_segment, args=(seg_url, cid), daemon=True).start()

            return '\n'.join(rewritten_lines)
        except Exception as e:
            log(f"[ERROR] Fetching manifest for {channel_id}: {e}")
            if channel_id in self.channel_urls:
                del self.channel_urls[channel_id]
        return None

    def prefetch_segment(self, seg_url, channel_id):
        with self.cache_lock:
            if seg_url in self.segment_cache:
                return
        self.fetch_and_decode_segment(seg_url, channel_id)

    def fetch_and_decode_segment(self, seg_url, channel_id):
        with self.cache_lock:
            if seg_url in self.segment_cache:
                return self.segment_cache[seg_url]

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Referer': f'https://dembed.st/premiumtv/daddy.php?id={channel_id}',
            'Origin': 'https://dembed.st'
        }
        req = urllib.request.Request(seg_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw_bytes = resp.read()

            ts_bytes = unwrap_stream_segment(raw_bytes)
            if ts_bytes:
                with self.cache_lock:
                    if len(self.segment_cache) >= self.max_cache_entries:
                        keys = list(self.segment_cache.keys())
                        for k in keys[:15]:
                            del self.segment_cache[k]
                    self.segment_cache[seg_url] = ts_bytes
                    self.total_segments_served += 1
                return ts_bytes
        except Exception as e:
            log(f"[ERROR] Decoding segment {seg_url[:40]}: {e}")
        return None

proxy_instance = DaddyLiveProxy()

# ---------------------------------------------------------------------------
# FastAPI REST Routing (100% Free on Render / Koyeb)
# ---------------------------------------------------------------------------

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse

api = FastAPI(title="LetsWatch DaddyLive Proxy")

@api.get("/ping")
def ping():
    return PlainTextResponse("pong")

@api.get("/live/{cid}.m3u8")
def live_manifest(cid: str, request: Request):
    host = request.headers.get("host", "")
    proto = request.headers.get("x-forwarded-proto", "https")
    manifest = proxy_instance.get_manifest(cid, host, proto)
    if manifest:
        return Response(
            content=manifest,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )
    return Response(content=f"Failed to resolve channel {cid}", status_code=502)

@api.get("/seg")
def live_segment(url: str, cid: str = "0"):
    ts_bytes = proxy_instance.fetch_and_decode_segment(url, cid)
    if ts_bytes:
        return Response(
            content=ts_bytes,
            media_type="video/mp2t",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes"
            }
        )
    return Response(content="Failed to decode segment to MPEG-TS", status_code=502)

@api.get("/channels")
def get_channels():
    if proxy_instance.catalog_data:
        return JSONResponse(content=proxy_instance.catalog_data)
    return JSONResponse(content={})

@api.get("/anime")
@api.get("/anime/resolve")
def resolve_anime(url: str = ""):
    if not url:
        return JSONResponse(content={"status": "error", "message": "Missing 'url' parameter"}, status_code=400)
    try:
        from anime_resolver import resolve_anime_wco
        res = resolve_anime_wco(url)
        return JSONResponse(content=res, status_code=200 if res.get("status") == "ready" else 502)
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

@api.get("/", response_class=HTMLResponse)
def home():
    return """<!DOCTYPE html>
<html>
<head>
<title>LetsWatch Roku DaddyLive Proxy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #fff; padding: 40px 20px; text-align: center; }
.card { background: #131b2e; border: 1px solid #1f2d4d; border-radius: 14px; padding: 36px; max-width: 600px; margin: 0 auto; box-shadow: 0 12px 40px rgba(0,0,0,0.5); }
h1 { color: #00e5ff; margin: 0 0 12px 0; font-size: 26px; }
.badge { display: inline-block; background: #00e5ff18; color: #00e5ff; border: 1px solid #00e5ff55; padding: 6px 18px; border-radius: 20px; font-weight: 700; font-size: 13px; margin-bottom: 24px; letter-spacing: 0.5px; }
p { color: #90a4ae; line-height: 1.6; font-size: 15px; margin: 10px 0; }
.status-pill { background: #090d15; border: 1px solid #243555; border-radius: 8px; padding: 14px; margin: 20px 0; font-family: monospace; color: #40c4ff; font-size: 15px; }
</style>
</head>
<body>
<div class="card">
  <h1>LETS WATCH PROXY</h1>
  <div class="badge">ONLINE &bull; 899 CHANNELS ACTIVE</div>
  <p>Real-time PNG Deobfuscation &amp; MPEG-TS HLS Transcoder</p>
  <div class="status-pill">Ping Endpoint: /ping &bull; Status: 200 OK</div>
  <p>Enter this domain in your Roku TV under <b>Live TV &rarr; * (Options) &rarr; Remote Proxy Setup</b>.</p>
</div>
</body>
</html>"""

if __name__ == "__main__":
    import uvicorn
    log(f"Starting LetsWatch DaddyLive Proxy on port {PORT}...")
    uvicorn.run(api, host="0.0.0.0", port=PORT)
