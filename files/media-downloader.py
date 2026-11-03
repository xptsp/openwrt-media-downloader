#!/usr/bin/env python3
import os
import sys
import json
import re
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import time
import subprocess

CONFIG_FILE = "/etc/config/media-downloader"
HTML_FILE = "/usr/share/media-downloader/index.html"

# Global memory log buffer to avoid grinding router flash storage
LOG_BUFFER = []
LOG_MAX_LINES = 150
log_lock = threading.Lock()

def log_message(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    print(formatted_msg)
    with log_lock:
        LOG_BUFFER.append(formatted_msg)
        if len(LOG_BUFFER) > LOG_MAX_LINES:
            LOG_BUFFER.pop(0)

def ensure_config_exists():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            f.write("package media-downloader\n\n")
            f.write("config global 'main'\n")
            f.write("    option bind_address '0.0.0.0'\n")
            f.write("    option port '8080'\n")
            f.write("    option base_dir '/mnt/sda1/Downloads'\n\n")

def get_system_hostname():
    try:
        if os.path.exists("/proc/sys/kernel/hostname"):
            with open("/proc/sys/kernel/hostname", "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return "Router"

def get_storage_metrics(path):
    try:
        target_path = path if path else "/"
        while target_path and not os.path.exists(target_path):
            parent = os.path.dirname(target_path)
            if parent == target_path:
                break
            target_path = parent
        if not os.path.exists(target_path):
            target_path = "/"
        stat = os.statvfs(target_path)
        total_space = (stat.f_blocks * stat.f_frsize) / (1024 * 1024 * 1024)
        free_space = (stat.f_bavail * stat.f_frsize) / (1024 * 1024 * 1024)
        free_pct = (free_space / total_space) * 100 if total_space > 0 else 0
        return {
            "storage_total": f"{total_space:.2f}",
            "storage_free": f"{free_space:.2f}",
            "storage_free_pct": f"{free_pct:.2f}"
        }
    except Exception as e:
        log_message(f"Storage monitoring metrics error: {e}")
        return {"storage_total": "0", "storage_free": "0", "storage_free_pct": "0"}

def read_uci_config():
    ensure_config_exists()
    config_dict = {"global": {"bind_address": "0.0.0.0", "port": "8080", "base_dir": "/mnt/sda1/Downloads"}, "series": {}}
    current_section_type = None
    current_section_name = None
    
    with open(CONFIG_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('package '):
                continue
            sec_match = re.match(r"^config\s+(\w+)\s+'?([^\s']+)'?", line)
            if sec_match:
                current_section_type = sec_match.group(1)
                current_section_name = sec_match.group(2)
                if current_section_type == "series":
                    config_dict["series"][current_section_name] = {"downloaded_episodes": [], "last_update": "0"}
                continue
            opt_match = re.match(r"^option\s+(\w+)\s+'([^']+)'", line)
            if opt_match and current_section_type:
                key, val = opt_match.group(1), opt_match.group(2)
                if current_section_type == "global":
                    config_dict["global"][key] = val
                elif current_section_type == "series" and current_section_name:
                    if key == "downloaded_episodes":
                        config_dict["series"][current_section_name][key] = [x.strip() for x in val.split(',') if x.strip()]
                    else:
                        config_dict["series"][current_section_name][key] = val
                        
    metrics = get_storage_metrics(config_dict["global"].get("base_dir", "/"))
    config_dict["global"].update(metrics)
    config_dict["global"]["hostname"] = get_system_hostname()
    config_dict["global"]["current_time"] = str(int(time.time()))
    return config_dict

def write_uci_config(config_data):
    ensure_config_exists()
    with open(CONFIG_FILE, 'w') as f:
        f.write("package media-downloader\n\n")
        f.write("config global 'main'\n")
        f.write(f"    option bind_address '{config_data['global'].get('bind_address', '0.0.0.0')}'\n")
        f.write(f"    option port '{config_data['global'].get('port', '8080')}'\n")
        f.write(f"    option base_dir '{config_data['global'].get('base_dir', '/mnt/sda1/Downloads')}'\n\n")
        for s_id, s_data in config_data["series"].items():
            f.write(f"config series '{s_id}'\n")
            f.write(f"    option name '{s_data.get('name', '')}'\n")
            f.write(f"    option url '{s_data.get('url', '')}'\n")
            f.write(f"    option path '{s_data.get('path', '')}'\n")
            f.write(f"    option last_update '{s_data.get('last_update', '0')}'\n")
            eps = ",".join(s_data.get('downloaded_episodes', []))
            f.write(f"    option downloaded_episodes '{eps}'\n\n")

def scan_existing_episodes():
    log_message("Initializing folder sync validation scan query...")
    config = read_uci_config()
    changed = False
    for s_id, s_data in config["series"].items():
        path = s_data.get('path', '')
        if os.path.exists(path):
            existing_eps = s_data.get('downloaded_episodes', [])
            updated_eps = set(existing_eps)
            for file in os.listdir(path):
                match = re.search(r'\b[sS]\d+[eE](\d+)\b|\b(?:ep|episode|e)\.?\s*(\d+)\b', file, re.IGNORECASE)
                if match:
                    raw_num = next(g for g in match.groups() if g is not None)
                    ep_num = str(int(raw_num))
                    if ep_num not in updated_eps:
                        updated_eps.add(ep_num)
                        changed = True
                        s_data['last_update'] = str(int(time.time()))
                        log_message(f"[{s_id}] Detected local file matching tracking query. Linked Ep {ep_num}.")
            s_data['downloaded_episodes'] = sorted(list(updated_eps), key=int)
    if changed:
        write_uci_config(config)
    log_message("Folder structural verification sync complete.")

def download_url_file(url, dest_folder, series_name, ep_num):
    try:
        os.makedirs(dest_folder, exist_ok=True)
        parsed_url = urllib.parse.urlparse(url)
        original_filename = os.path.basename(parsed_url.path).lower()
        
        season_num = 1
        combined_text = (url + " " + original_filename).lower()
        isolated_season_text = re.sub(r'episodes?|ep\s*\d+|epi?\s*\d+', '', combined_text)
        season_match = re.search(r'\b(?:season)[-_\s]*(\d+)|\bs(\d+)\b|\bs0(\d+)', isolated_season_text)
        if season_match:
            detected_season = int(next(g for g in season_match.groups() if g is not None))
            if detected_season < 20: 
                season_num = detected_season

        s_str = f"S{season_num:02d}"
        e_str = f"E{int(ep_num):02d}"
        clean_series_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()
        
        ext_match = re.search(r'\.(mkv|mp4|avi|mov)$', original_filename)
        file_ext = ext_match.group(0) if ext_match else ".mkv"
        
        standardized_name = f"{clean_series_name} - {s_str}{e_str}{file_ext}"
        dest_path = os.path.join(dest_folder, standardized_name)
        
        log_message(f"Normalized download target layout: '{standardized_name}'")
        log_message(f"Handing high-speed binary stream extraction off to system curl core engine...")

        curl_cmd = [
            "curl", "-L", 
            "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-H", "Accept: */*",
            "-H", "Accept-Language: en-US,en;q=0.5",
            "--connect-timeout", "15",
            "--retry", "3",
            "-o", dest_path, 
            url
        ]
        
        result = subprocess.run(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 100:
            log_message(f"Download transaction complete: saved to {dest_path}")
            return True
        else:
            error_msg = result.stderr.decode('utf-8', errors='ignore')
            log_message(f"Curl file stream collection failed: {error_msg}")
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False
    except Exception as e:
        log_message(f"File transaction write crash on {url}: {e}")
        return False

def run_downloader_cycle():
    log_message("Starting scheduled web crawling sequence cycle...")
    scan_existing_episodes()
    config = read_uci_config()
    
    desktop_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }

    for s_id, s_data in config["series"].items():
        url = s_data.get('url', '')
        dest_path = s_data.get('path', '')
        if not url or not dest_path:
            continue
            
        try:
            req = urllib.request.Request(url, headers=desktop_headers)
            with urllib.request.urlopen(req) as res:
                html = res.read().decode('utf-8', errors='ignore')
            
            # Diagnostic save hook to safely inspect raw data inputs 
            with open("/tmp/downloader_debug.html", "w", encoding="utf-8") as df:
                df.write(html)
            
            # Slice away sidebars if present
            content_start = html.find('class="entry-content"')
            if content_start != -1:
                content_end = html.find('class="recommended"', content_start)
                if content_end == -1:
                    content_end = html.find('class="related"', content_start)
                if content_end == -1:
                    content_end = html.find('</article>', content_start)
                html = html[content_start:content_end] if content_end != -1 else html[content_start:]

            links_found = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)

            url_slug_match = re.search(r'thenkiri\.com/([^/]+)', url.lower())
            series_slug = url_slug_match.group(1).strip() if url_slug_match else ""
            
            # 🎯 PURE GENERIC KEYWORD EXTRACTOR: Isolates descriptive words dynamically
            slug_words = [w for w in series_slug.split('-') if w not in ['s01', 's02', 'complete', 'korean', 'drama', 'series', 'tv', 'download']]
            
            # DYNAMIC FALLBACK: Grab the last descriptive word, or fall back directly to the show's unique configuration ID
            show_keyword = slug_words[-1] if slug_words else s_id

            for episode_page_url, link_text in links_found:
                clean_text = re.sub(r'<[^>]*>', '', link_text).strip()
                clean_text_lower = clean_text.lower()
                url_lower = episode_page_url.lower()
                
                # 🛡️ SYSTEM FILTER: Instantly drop core web standard links, socials, and site feeds
                if any(x in url_lower for x in ["gmpg.org", "wordpress", "telegram.me", "t.me", "tiktok.com", "twitter.com", "/feed/"]):
                    continue

                # 🛡️ THE BRAND PROTECTION SHIELD: The link text OR the destination URL must match your unique show keyword
                name_match = (show_keyword in clean_text_lower) or (show_keyword in url_lower)
                if not name_match:
                    continue
                
                if not re.search(r'ep|episode|s\d+|down', clean_text_lower):
                    continue

                ep_numbers = [n for n in re.findall(r'\b\d+\b', clean_text) if int(n) != 1 and int(n) != 2 and int(n) != 2025 and int(n) != 2026]
                # 🔄 Fallback: If no episode number was in the text link, check ONLY the filename layout string!
                if not ep_numbers:
                    filename_only = url_lower.split('/')[-1]
                    ep_numbers = [n for n in re.findall(r'(?:ep|e|episode)[-_\s]*(\d+)', filename_only) if int(n) != 1 and int(n) != 2 and int(n) != 2025 and int(n) != 2026]
                if not ep_numbers:
                    continue

                for ep_num_raw in ep_numbers:
                    ep_num = str(int(ep_num_raw))

                    live_config = read_uci_config()
                    live_series_data = live_config["series"].get(s_id, {})
                    live_downloaded = set(live_series_data.get('downloaded_episodes',))

                    if ep_num in live_downloaded:
                        continue

                    log_message(f"[{s_id}] Target landing node located for Ep {ep_num}: {episode_page_url}")

                    try:
                        sub_req = urllib.request.Request(episode_page_url, headers=desktop_headers)
                        with urllib.request.urlopen(sub_req) as sub_res:
                            sub_html = sub_res.read().decode('utf-8', errors='ignore')

                        final_download_url = None

                        if "downloadwella.com" in episode_page_url or "downloawella.com" in episode_page_url:
                            form_inputs = re.findall(r'name=["\'](op|id|rand|referer|method_free|method_premium)["\']\s+value=["\']([^"\']*)["\']', sub_html)
                            form_data = {name: val for name, val in form_inputs}

                            if 'id' in form_data:
                                form_data['op'] = 'download2'
                                form_data['method_free'] = 'Free Download'
                                if 'method_premium' in form_data:
                                    del form_data['method_premium']

                                encoded_data = urllib.parse.urlencode(form_data).encode('utf-8')
                                post_req = urllib.request.Request(
                                    episode_page_url,
                                    data=encoded_data,
                                    headers={
                                        'User-Agent': desktop_headers['User-Agent'],
                                        'Content-Type': 'application/x-www-form-urlencoded',
                                        'Referer': episode_page_url
                                    }
                                )
                                with urllib.request.urlopen(post_req) as post_res:
                                    generated_html = post_res.read().decode('utf-8', errors='ignore')
                                media_match = re.search(r'href=["\'](https?://[^"\']+\.(?:mkv|mp4)[^"\']*)["\']', generated_html, re.IGNORECASE)
                                if media_match:
                                    final_download_url = media_match.group(1)

                        if not final_download_url:
                            direct_links = re.findall(r'href=["\'](https?://[^"\']+\.(?:mkv|mp4)[^"\']*)["\']', sub_html)
                            if direct_links:
                                final_download_url = direct_links

                        if final_download_url:
                            log_message(f"[{s_id}] True binary stream path resolved! Fetching Ep {ep_num}...")
                            if download_url_file(final_download_url, dest_path, live_series_data.get('name', s_id), ep_num):
                                live_downloaded.add(ep_num)
                                live_series_data['downloaded_episodes'] = sorted(list(live_downloaded), key=int)
                                live_series_data['last_update'] = str(int(time.time()))
                                write_uci_config(live_config)
                        else:
                            log_message(f"[{s_id}] Failed to automatically decrypt download tokens for Ep {ep_num}.")
                    except Exception as sub_err:
                        log_message(f"[{s_id}] Error solving destination targets for ep {ep_num}: {sub_err}")
        except Exception as e:
            log_message(f"[{s_id}] Main engine execution exception crash: {e}")

    log_message("Scheduled sync parsing operation loop finalized.")

class RouterHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return
    def do_GET(self):
        base_dir = os.path.dirname(HTML_FILE)
        if self.path in ['/', '/index.html']:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            try:
                with open(HTML_FILE, 'r', encoding='utf-8') as f:
                    self.wfile.write(f.read().encode('utf-8'))
            except Exception as e:
                self.wfile.write(f"Error loading interface layout: {e}".encode('utf-8'))
        elif self.path == '/style.css':
            css_path = os.path.join(base_dir, 'style.css')
            if os.path.exists(css_path):
                self.send_response(200)
                self.send_header('Content-Type', 'text/css; charset=utf-8')
                self.end_headers()
                with open(css_path, 'r', encoding='utf-8') as f:
                    self.wfile.write(f.read().encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/manifest.json':
            manifest_path = os.path.join(base_dir, 'manifest.json')
            if os.path.exists(manifest_path):
                self.send_response(200)
                self.send_header('Content-Type', 'application/manifest+json; charset=utf-8')
                self.end_headers()
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    self.wfile.write(f.read().encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/sw.js':
            sw_path = os.path.join(base_dir, 'sw.js')
            if os.path.exists(sw_path):
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.send_header('Service-Worker-Allowed', '/')
                self.end_headers()
                with open(sw_path, 'r', encoding='utf-8') as f:
                    self.wfile.write(f.read().encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path in ['/app-icon.png', '/favicon.ico']:
            img_path = os.path.join(base_dir, 'app-icon.png')
            if os.path.exists(img_path):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                with open(img_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/wallpaper.webp':
            img_path = os.path.join(base_dir, 'wallpaper.webp')
            if os.path.exists(img_path):
                self.send_response(200)
                self.send_header('Content-Type', 'image/webp')
                self.end_headers()
                with open(img_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/api/series':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(read_uci_config()).encode('utf-8'))
        elif self.path == '/api/logs':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            with log_lock:
                self.wfile.write(json.dumps(LOG_BUFFER).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/series/'):
            s_id = self.path.split('/')[-1]
            content_length = int(self.headers['Content-Length'])
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            config = read_uci_config()
            
            if s_id == '_config':
                if "base_dir" in data:
                    config["global"]["base_dir"] = data["base_dir"]
                write_uci_config(config)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                return

            if not re.match(r'^[a-z]+$', s_id):
                self.send_response(400)
                self.end_headers()
                return
            if s_id not in config["series"]:
                config["series"][s_id] = {"downloaded_episodes": [], "last_update": "0"}
            config["series"][s_id].update({"name": data.get("name"), "url": data.get("url"), "path": data.get("path")})
            write_uci_config(config)
            log_message(f"Saved tracking profile registration section: {data.get('name')}")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
        elif self.path == '/api/scan':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
            threading.Thread(target=scan_existing_episodes).start()
        elif self.path == '/api/download':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
            threading.Thread(target=run_downloader_cycle).start()

    def do_DELETE(self):
        if self.path.startswith('/api/series/'):
            s_id = self.path.split('/')[-1]
            config = read_uci_config()
            if s_id in config["series"]:
                name = config["series"][s_id].get('name', s_id)
                del config["series"][s_id]
                write_uci_config(config)
                log_message(f"Removed monitoring registration handles for: {name}")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "deleted"}).encode('utf-8'))
                return
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    log_message("Initializing System Core Media Downloader Runtime Environment...")
    if len(sys.argv) > 1 and sys.argv[1] == '--cron':
        run_downloader_cycle()
        sys.exit(0)
    active_config = read_uci_config()
    host_ip = active_config["global"].get("bind_address", "0.0.0.0")
    try:
        host_port = int(active_config["global"].get("port", "8080"))
    except ValueError:
        host_port = 8080
    server = HTTPServer((host_ip, host_port), RouterHTTPHandler)
    log_message(f"Web interface socket control engine bound cleanly to http://{host_ip}:{host_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
