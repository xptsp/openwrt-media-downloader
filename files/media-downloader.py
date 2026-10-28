#!/usr/bin/env python3
import os
import sys
import json
import re
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

CONFIG_FILE = "/etc/config/media-downloader"
HTML_FILE = "/usr/share/media-downloader/index.html"

def get_system_hostname():
    try:
        if os.path.exists("/proc/sys/kernel/hostname"):
            with open("/proc/sys/kernel/hostname", "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return "Router"

def ensure_config_exists():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            f.write("package media-downloader\n\n")
            f.write("config global 'main'\n")
            f.write("    option bind_address '0.0.0.0'\n")
            f.write("    option port '8080'\n")
            f.write("    option base_dir '/mnt/sda1/Downloads'\n\n")

import time

def read_uci_config():
    ensure_config_exists()
    config_dict = {"global": {"bind_address": "0.0.0.0", "port": "8080", "base_dir": "/mnt/sda1/Downloads", "hostname": get_system_hostname()}, "series": {}}
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
    config = read_uci_config()
    changed = False
    for s_id, s_data in config["series"].items():
        path = s_data.get('path', '')
        if os.path.exists(path):
            existing_eps = s_data.get('downloaded_episodes', [])
            updated_eps = set(existing_eps)
            for file in os.listdir(path):
                match = re.search(r'(?:ep|episode|e)\s*(\d+)', file, re.IGNORECASE)
                if match:
                    ep_num = str(int(match.group(1)))
                    if ep_num not in updated_eps:
                        updated_eps.add(ep_num)
                        changed = True
                        s_data['last_update'] = str(int(time.time()))
                        
            s_data['downloaded_episodes'] = sorted(list(updated_eps), key=int)
    if changed:
        write_uci_config(config)

def download_url_file(url, dest_folder):
    try:
        os.makedirs(dest_folder, exist_ok=True)
        parsed_url = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename or '.' not in filename:
            filename = "downloaded_episode.mkv"
            
        dest_path = os.path.join(dest_folder, filename)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(dest_path, 'wb') as out_file:
            block_size = 1024 * 64
            while True:
                buffer = response.read(block_size)
                if not buffer:
                    break
                out_file.write(buffer)
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}", file=sys.stderr)
        return False

def run_downloader_cycle():
    config = read_uci_config()
    for s_id, s_data in config["series"].items():
        url = s_data.get('url', '')
        dest_path = s_data.get('path', '')
        downloaded = set(s_data.get('downloaded_episodes', []))
        
        if not url or not dest_path:
            continue
            
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as res:
                html = res.read().decode('utf-8', errors='ignore')
            
            episode_blocks = re.findall(r'(?:Episode|Ep\.)\s*(\d+)(.*?)(?=(?:Episode|Ep\.)\s*\d+|<h[1-3]|class=["\']recommended|class=["\']you-might-also-like|$)', html, re.DOTALL | re.IGNORECASE)
            
            for ep_num_raw, block_html in episode_blocks:
                ep_num = str(int(ep_num_raw))
                if ep_num in downloaded:
                    continue

                link_match = re.search(r'href=["\'](https?://[^"\']+)["\']', block_html)
                if not link_match:
                    continue
                    
                episode_page_url = link_match.group(1)
                print(f"[{s_id}] Target landing host found for Ep {ep_num}: {episode_page_url}")

                try:
                    # Step 1: Open the initial file hosting text wrapper page
                    sub_req = urllib.request.Request(episode_page_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(sub_req) as sub_res:
                        sub_html = sub_res.read().decode('utf-8', errors='ignore')
                    
                    final_download_url = None
                    
                    # Step 2: Handle downloadwella.com style free file lockers
                    if "downloadwella.com" in episode_page_url:
                        # Extract the required hidden dynamic form arguments
                        form_inputs = re.findall(r'name=["\'](op|id|rand|referer|method_free|method_premium)["\']\s+value=["\']([^"\']*)["\']', sub_html)
                        form_data = {name: val for name, val in form_inputs}
                        
                        # Ensure standard operational properties match Free tier parameters
                        if 'id' in form_data:
                            form_data['op'] = 'download2'
                            form_data['method_free'] = 'Free Download'
                            if 'method_premium' in form_data:
                                del form_data['method_premium']
                            
                            # Encode inputs into raw form bytes payload
                            encoded_data = urllib.parse.urlencode(form_data).encode('utf-8')
                            
                            # Submit the generation form back via a POST request
                            post_req = urllib.request.Request(
                                episode_page_url, 
                                data=encoded_data,
                                headers={
                                    'User-Agent': 'Mozilla/5.0',
                                    'Content-Type': 'application/x-www-form-urlencoded',
                                    'Referer': episode_page_url
                                }
                            )
                            
                            with urllib.request.urlopen(post_req) as post_res:
                                generated_html = post_res.read().decode('utf-8', errors='ignore')
                            
                            # Locate the uniquely spawned stream anchor that now exists on the output layout
                            media_match = re.search(r'href=["\'](https?://[^"\']+\.(?:mkv|mp4)[^"\']*)["\']', generated_html, re.IGNORECASE)
                            if media_match:
                                final_download_url = media_match.group(1)
                    
                    # Fallback Strategy: If not downloadwella or form extraction fails, use baseline extension filter
                    if not final_download_url:
                        direct_links = re.findall(r'href=["\'](https?://[^"\']+\.(?:mkv|mp4)[^"\']*)["\']', sub_html)
                        if direct_links:
                            final_download_url = direct_links

                    # Step 3: Download the true binary stream file down to storage
                    if final_download_url:
                        print(f"[{s_id}] Bypassed locker! Streaming true link: {final_download_url}")
                        if download_url_file(final_download_url, dest_path):
                            downloaded.add(ep_num)
                            s_data['downloaded_episodes'] = sorted(list(downloaded), key=int)
                            s_data['last_update'] = str(int(time.time()))
                            
                            write_uci_config(config)
                    else:
                        print(f"[{s_id}] Failed to automatically unpack download locker tokens for Ep {ep_num}.")
                            
                except Exception as sub_err:
                    print(f"[{s_id}] Error solving destination targets for ep {ep_num}: {sub_err}", file=sys.stderr)
                                
        except Exception as e:
            print(f"[{s_id}] Main engine critical exception crash: {e}", file=sys.stderr)

class RouterHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return
        
    def do_GET(self):
        if self.path in ['/', '/index.html']:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            try:
                with open(HTML_FILE, 'r', encoding='utf-8') as f:
                    self.wfile.write(f.read().encode('utf-8'))
            except Exception as e:
                self.wfile.write(f"Error loading interface layout: {e}".encode('utf-8'))
                
        elif self.path == '/manifest.json':
            base_dir = os.path.dirname(HTML_FILE)
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
            base_dir = os.path.dirname(HTML_FILE)
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
            base_dir = os.path.dirname(HTML_FILE)
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
            base_dir = os.path.dirname(HTML_FILE)
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
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/series/'):
            s_id = self.path.split('/')[-1]
            if not re.match(r'^[a-z]+$', s_id):
                self.send_response(400)
                self.end_headers()
                return
            content_length = int(self.headers['Content-Length'])
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            
            config = read_uci_config()
            if "base_dir" in data:
                config["global"]["base_dir"] = data["base_dir"]
                
            if s_id not in config["series"]:
                config["series"][s_id] = {"downloaded_episodes": []}
                
            config["series"][s_id].update({"name": data.get("name"), "url": data.get("url"), "path": data.get("path")})
            write_uci_config(config)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
        elif self.path == '/api/scan':
            scan_existing_episodes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
        elif self.path == '/api/download':
            threading.Thread(target=run_downloader_cycle).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))

    def do_DELETE(self):
        if self.path.startswith('/api/series/'):
            s_id = self.path.split('/')[-1]
            config = read_uci_config()
            if s_id in config["series"]:
                del config["series"][s_id]
                write_uci_config(config)
                
                # Send explicit execution confirmation headers to unblock browser threads
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers() # 👈 THIS line explicitly closes the link connection
                self.wfile.write(json.dumps({"status": "deleted"}).encode('utf-8'))
                return
                
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--cron':
        run_downloader_cycle()
        sys.exit(0)
    
    # Read dynamic configuration parameters at startup execution
    active_config = read_uci_config()
    host_ip = active_config["global"].get("bind_address", "0.0.0.0")
    try:
        host_port = int(active_config["global"].get("port", "8080"))
    except ValueError:
        host_port = 8080
        
    server = HTTPServer((host_ip, host_port), RouterHTTPHandler)
    print(f"Media Downloader Engine online on http://{host_ip}:{host_port}...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
