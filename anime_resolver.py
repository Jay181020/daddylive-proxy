import urllib.request
import re
import json

def resolve_anime_wco(ep_url):
    """
    Given an episode URL or path (e.g. '/naruto-episode-1-english-dubbed' or full URL),
    resolves the direct streaming video URL (MP4).
    """
    if not ep_url.startswith('http'):
        if not ep_url.startswith('/'):
            ep_url = '/' + ep_url
        full_url = 'https://www.wcostream.tv' + ep_url
    else:
        full_url = ep_url.replace('wco.tv', 'wcostream.tv')

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.wcostream.tv/'
    }

    req = urllib.request.Request(full_url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode('utf-8', 'ignore')

    # Find embed iframe
    iframe_match = re.search(r'<iframe[^>]*src=[\'"]([^\'"]*embed\.wcostream\.com[^\'"]*)[\'"]', html)
    if not iframe_match:
        # Fallback to general iframe search
        iframe_match = re.search(r'<iframe[^>]*src=[\'"]([^\'"]*/inc/embed/[^\'"]*)[\'"]', html)
        if iframe_match:
            iframe_url = 'https://embed.wcostream.com' + iframe_match.group(1) if not iframe_match.group(1).startswith('http') else iframe_match.group(1)
        else:
            return {"error": "Embed iframe not found on page"}
    else:
        iframe_url = iframe_match.group(1)

    # Request video-js.php to obtain session cookie
    vjs_url = iframe_url.replace("index.php", "video-js.php")
    vjs_req = urllib.request.Request(vjs_url, headers=headers)
    with urllib.request.urlopen(vjs_req, timeout=12) as vr:
        vjs_html = vr.read().decode('utf-8', 'ignore')
        cookies = vr.headers.get_all('Set-Cookie') or []
        cookie_header = "; ".join([c.split(';')[0] for c in cookies])

    # Find getvidlink.php
    gvl_match = re.search(r'getvidlink\.php[^\s"\'\)]*', vjs_html)
    if not gvl_match:
        return {"error": "getvidlink.php parameter not found"}

    gvl_path = gvl_match.group(0)
    gvl_url = "https://embed.wcostream.com/inc/embed/" + gvl_path if not gvl_path.startswith("http") else gvl_path

    gvl_headers = dict(headers)
    gvl_headers['Referer'] = vjs_url
    gvl_headers['X-Requested-With'] = 'XMLHttpRequest'
    if cookie_header:
        gvl_headers['Cookie'] = cookie_header

    gvl_req = urllib.request.Request(gvl_url, headers=gvl_headers)
    with urllib.request.urlopen(gvl_req, timeout=12) as gr:
        gvl_json_raw = gr.read().decode('utf-8', 'ignore')
        gvl_data = json.loads(gvl_json_raw)

    server = gvl_data.get('server')
    if not server:
        return {"error": "Server not present in getvidlink response"}

    chosen_evid = gvl_data.get('fhd') or gvl_data.get('hd') or gvl_data.get('enc')
    quality = "1080p FHD" if gvl_data.get('fhd') else ("720p HD" if gvl_data.get('hd') else "SD")
    if not chosen_evid:
        return {"error": "No stream evid found in response"}

    # Final step: get direct stream node
    step4_url = f"{server}/getvid?evid={chosen_evid}&json"
    step4_req = urllib.request.Request(step4_url, headers=gvl_headers)
    with urllib.request.urlopen(step4_req, timeout=12) as s4r:
        s4_data = s4r.read().decode('utf-8', 'ignore').strip()
        if s4_data.startswith('"') and s4_data.endswith('"'):
            final_url = s4_data[1:-1].replace('\\/', '/')
        else:
            try:
                pj = json.loads(s4_data)
                final_url = pj.get('url', '')
            except Exception:
                final_url = s4_data

    return {
        "status": "ready",
        "url": final_url,
        "quality": quality,
        "headers": {
            "User-Agent": headers['User-Agent'],
            "Referer": "https://embed.wcostream.com/",
            "Cookie": cookie_header
        }
    }

if __name__ == '__main__':
    res = resolve_anime_wco('/naruto-episode-1-english-dubbed')
    print("Resolved result:")
    print(json.dumps(res, indent=2))
