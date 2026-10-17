import urllib.request
import urllib.parse
import re
import json
import time

DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# Cache resolved episode streams for 20 minutes: (ep_key, ua) -> (timestamp, result_dict)
_RESOLVE_CACHE = {}

def _build_wco_page_urls(ep_url):
    clean = ep_url.strip()
    if clean.startswith('http://') or clean.startswith('https://'):
        parsed = urllib.parse.urlparse(clean)
        slug = parsed.path
        if parsed.query:
            slug += '?' + parsed.query
    else:
        slug = clean

    if not slug.startswith('/'):
        slug = '/' + slug

    ep_name = slug.strip('/')
    urls = []
    if '/' not in ep_name and '?' not in ep_name:
        urls.append(f'https://www.wcostream.tv/anime/?name={ep_name}&preview=true')
    urls.append(f'https://www.wcostream.tv{slug}')
    if '/' not in ep_name and '?' not in ep_name:
        urls.append(f'https://www.wcofun.net/anime/?name={ep_name}&preview=true')
    urls.append(f'https://www.wcofun.net{slug}')
    return urls

def fetch_wco_episode_html(ep_url):
    headers = {
        'User-Agent': DEFAULT_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://www.wcostream.tv/'
    }
    for full_url in _build_wco_page_urls(ep_url):
        try:
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode('utf-8', 'ignore')
                if len(html) > 1000 and 'Just a moment...' not in html:
                    return html, full_url
        except Exception:
            continue
    return '', ''

def resolve_anime_wco(ep_url, client_ua=None):
    """
    Given an episode URL or path (e.g. '/darkwing-duck-season-1-episode-4-getting-antsy'),
    resolves both the classic MP4 stream (embed.wcostream.com) and modern HLS stream (vhs.wcostream.com).
    Uses client_ua if provided so the upstream token matches the player's User-Agent.
    """
    ua = client_ua if client_ua else DEFAULT_UA
    cache_key = (ep_url.strip(), ua)
    now = time.time()
    if cache_key in _RESOLVE_CACHE:
        ts, cached = _RESOLVE_CACHE[cache_key]
        if now - ts < 1200:
            return cached

    html, page_url = fetch_wco_episode_html(ep_url)
    if not html:
        return {"status": "error", "error": "Failed to fetch WCO episode page"}

    result = {
        "status": "error",
        "url": "",
        "mp4_url": "",
        "hls_url": "",
        "quality": "HD",
        "headers": {}
    }

    # 1. Resolve Classic MP4 Stream (embed.wcostream.com)
    iframe_match = re.search(r'<iframe[^>]*src=[\'"]([^\'"]*embed\.wcostream\.com[^\'"]*)[\'"]', html, re.I)
    if not iframe_match:
        iframe_match = re.search(r'<iframe[^>]*src=[\'"]([^\'"]*/inc/embed/[^\'"]*)[\'"]', html, re.I)

    if iframe_match:
        iframe_url = iframe_match.group(1).replace('&amp;', '&').strip()
        if not iframe_url.startswith('http'):
            iframe_url = 'https://embed.wcostream.com' + iframe_url

        vjs_url = iframe_url.replace("index.php", "video-js.php").replace(" ", "%20")
        vjs_headers = {
            'User-Agent': ua,
            'Referer': 'https://embed.wcostream.com/'
        }
        try:
            vjs_req = urllib.request.Request(vjs_url, headers=vjs_headers)
            with urllib.request.urlopen(vjs_req, timeout=10) as vr:
                vjs_html = vr.read().decode('utf-8', 'ignore')
                cookies = vr.headers.get_all('Set-Cookie') or []
                cookie_header = "; ".join([c.split(';')[0] for c in cookies])

            gvl_match = re.search(r'getvidlink\.php[^\s"\'\)]*', vjs_html, re.I)
            if gvl_match:
                gvl_path = gvl_match.group(0).replace('&amp;', '&').strip()
                gvl_url = "https://embed.wcostream.com/inc/embed/" + gvl_path if not gvl_path.startswith("http") else gvl_path
                gvl_url = gvl_url.replace(" ", "%20")

                gvl_headers = {
                    'User-Agent': ua,
                    'Referer': vjs_url,
                    'X-Requested-With': 'XMLHttpRequest'
                }
                if cookie_header:
                    gvl_headers['Cookie'] = cookie_header

                gvl_req = urllib.request.Request(gvl_url, headers=gvl_headers)
                with urllib.request.urlopen(gvl_req, timeout=10) as gr:
                    gvl_data = json.loads(gr.read().decode('utf-8', 'ignore'))

                server = gvl_data.get('server', '')
                chosen_evid = gvl_data.get('fhd') or gvl_data.get('hd') or gvl_data.get('enc')
                quality = "1080p FHD" if gvl_data.get('fhd') else ("720p HD" if gvl_data.get('hd') else "SD")

                if server and chosen_evid:
                    step4_url = f"{server}/getvid?evid={chosen_evid}&json"
                    final_url = f"{server}/getvid?evid={chosen_evid}"
                    try:
                        step4_req = urllib.request.Request(step4_url, headers=gvl_headers)
                        with urllib.request.urlopen(step4_req, timeout=8) as s4r:
                            s4_data = s4r.read().decode('utf-8', 'ignore').strip()
                            if s4_data.startswith('"') and s4_data.endswith('"'):
                                candidate = s4_data[1:-1].replace('\\/', '/')
                                if candidate.startswith('http'):
                                    final_url = candidate
                            else:
                                pj = json.loads(s4_data)
                                if isinstance(pj, dict) and pj.get('url', '').startswith('http'):
                                    final_url = pj['url']
                    except Exception:
                        pass

                    if final_url.endswith('&json'):
                        final_url = final_url[:-5]

                    result["status"] = "ready"
                    result["url"] = final_url
                    result["mp4_url"] = final_url
                    result["quality"] = quality
                    result["headers"] = {
                        "User-Agent": ua,
                        "Referer": "https://embed.wcostream.com/",
                        "Cookie": cookie_header
                    }
        except Exception as e:
            result["mp4_error"] = str(e)

    # 2. Resolve Modern VHS HLS Stream (vhs.wcostream.com)
    vhs_match = re.search(r'<iframe[^>]*src=[\'"]([^\'"]*vhs\.wcostream\.com[^\'"]*)[\'"]', html, re.I)
    if vhs_match:
        raw_vhs = vhs_match.group(1).replace('&amp;', '&').strip()
        if raw_vhs.startswith('//'):
            raw_vhs = 'https:' + raw_vhs
        elif not raw_vhs.startswith('http'):
            raw_vhs = 'https://vhs.wcostream.com' + raw_vhs

        vjs_target = raw_vhs
        if 'video-js/' not in vjs_target:
            if 'vhs.wcostream.com/?' in vjs_target:
                vjs_target = vjs_target.replace('vhs.wcostream.com/?', 'vhs.wcostream.com/video-js/?')
            else:
                vjs_target = vjs_target.replace('vhs.wcostream.com/', 'vhs.wcostream.com/video-js/')

        try:
            vhs_req = urllib.request.Request(vjs_target, headers={
                'User-Agent': ua,
                'Referer': 'https://vhs.wcostream.com/'
            })
            with urllib.request.urlopen(vhs_req, timeout=10) as vhr:
                vhs_html = vhr.read().decode('utf-8', 'ignore')

            m_stream = re.search(r'https?://[^\s"\']+(?:wcostream\.com|cizgifilmlerizle\.com)/getvid/[^\s"\']+', vhs_html, re.I)
            if m_stream:
                hls_url = m_stream.group(0).replace('&amp;', '&').strip('"\'')
                if 'cizgifilmlerizle.com' in hls_url:
                    hls_url = hls_url.replace('https://', 'http://')
                result["hls_url"] = hls_url
                result["hls_headers"] = {
                    "User-Agent": ua,
                    "Referer": "https://vhs.wcostream.com/"
                }
                if not result["url"]:
                    result["status"] = "ready"
                    result["url"] = hls_url
                    result["quality"] = "1080p FHD (HLS)"
                    result["headers"] = result["hls_headers"]
        except Exception as e:
            result["hls_error"] = str(e)

    if result["status"] == "ready":
        _RESOLVE_CACHE[cache_key] = (now, result)

    return result

def rewrite_wco_m3u8(m3u8_text, manifest_url, scheme, host):
    out_lines = []
    def _repl_uri(m):
        raw_u = m.group(1)
        abs_u = urllib.parse.urljoin(manifest_url, raw_u)
        proxied = f"{scheme}://{host}/anime/hls/proxy?url={urllib.parse.quote_plus(abs_u)}"
        return f'URI="{proxied}"'

    for line in m3u8_text.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
        elif s.startswith('#'):
            if 'URI="' in s:
                s = re.sub(r'URI="([^"]+)"', _repl_uri, s)
            out_lines.append(s)
        else:
            abs_u = urllib.parse.urljoin(manifest_url, s)
            proxied = f"{scheme}://{host}/anime/hls/proxy?url={urllib.parse.quote_plus(abs_u)}"
            out_lines.append(proxied)
    return '\n'.join(out_lines)

if __name__ == '__main__':
    res = resolve_anime_wco('/darkwing-duck-season-1-episode-4-getting-antsy')
    print("Resolved result:")
    print(json.dumps(res, indent=2))
