#!/usr/bin/env python3
import secrets
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
import queue

##############################################################################################
# Variables
##############################################################################################
CONFIG_FILE = "/etc/config/media-downloader"
HTML_FILE = "/usr/share/media-downloader/index.html"

# Global memory log buffer to avoid grinding router flash storage
ACTIVE_DOWNLOADS = {}
progress_lock = threading.Lock()
LOG_BUFFER = []
LOG_MAX_LINES = 150
log_lock = threading.Lock()

ACTIVE_SESSIONS = set()
session_lock = threading.Lock()

##############################################################################################
# 🌐 GLOBAL BACKGROUND TASK QUEUE MATRIX
##############################################################################################
# Thread-safe queue containing dictionaries of tasks: {"series_id": ..., "episode": ..., "url": ..., "path": ...}
DOWNLOAD_QUEUE = queue.Queue()

# Shared dictionary to hold progress updates for the CGI frontend /api/progress endpoint
ACTIVE_DOWNLOADS = {}

# Thread lock to guarantee safe data mutations across parallel runtime loops
PROGRESS_LOCK = threading.Lock()

##############################################################################################
# Supporting functions
##############################################################################################
def check_ffmpeg_available():
	"""Dynamically probe the host OS to see if FFmpeg is installed in the PATH"""
	import shutil
	return shutil.whoami() if hasattr(shutil, 'whoami') else shutil.which("ffmpeg") is not None

##############################################################################################
def log_message(msg):
	timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
	formatted_msg = f"[{timestamp}] {msg}"
	print(formatted_msg)
	with log_lock:
		LOG_BUFFER.append(formatted_msg)
		if len(LOG_BUFFER) > LOG_MAX_LINES:
			LOG_BUFFER.pop(0)

##############################################################################################
def ensure_config_exists():
	if not os.path.exists(CONFIG_FILE):
		os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
		with open(CONFIG_FILE, 'w') as f:
			f.write("package media-downloader\n\n")
			f.write("config global 'main'\n")
			f.write("    option bind_address '0.0.0.0'\n")
			f.write("    option port '8080'\n")
			f.write("    option base_dir '/mnt/sda1/Downloads'\n")
			f.write("    option auth_enabled '0'\n")
			f.write("    option auth_username 'admin'\n")
			f.write("    option auth_password 'admin'\n\n")

##############################################################################################
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

##############################################################################################
def read_uci_config():
	ensure_config_exists()
	config_dict = {
		"global": {
			"bind_address": "0.0.0.0",
			"port": "8080",
			"base_dir": "/mnt/sda1/Downloads",
			"auth_enabled": "0",
			"auth_username": "admin",
			"auth_password": "admin"
		},
		"series": {}
	}
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
	config_dict["global"]["current_time"] = str(int(time.time()))
	return config_dict

##############################################################################################
def write_uci_config(config_data):
	ensure_config_exists()
	with open(CONFIG_FILE, 'w') as f:
		f.write("package media-downloader\n\n")
		f.write("config global 'main'\n")
		f.write(f"    option bind_address '{config_data['global'].get('bind_address', '0.0.0.0')}'\n")
		f.write(f"    option port '{config_data['global'].get('port', '8080')}'\n")
		f.write(f"    option base_dir '{config_data['global'].get('base_dir', '/mnt/sda1/Downloads')}'\n")
		f.write(f"    option auth_enabled '{config_data['global'].get('auth_enabled', '0')}'\n")
		f.write(f"    option auth_username '{config_data['global'].get('auth_username', 'admin')}'\n")
		f.write(f"    option auth_password '{config_data['global'].get('auth_password', 'admin')}'\n\n")
		for s_id, s_data in config_data["series"].items():
			f.write(f"config series '{s_id}'\n")
			f.write(f"    option name '{s_data.get('name', '')}'\n")
			f.write(f"    option url '{s_data.get('url', '')}'\n")
			f.write(f"    option path '{s_data.get('path', '')}'\n")
			f.write(f"    option last_update '{s_data.get('last_update', '0')}'\n")
			eps = ",".join(s_data.get('downloaded_episodes', []))
			f.write(f"    option downloaded_episodes '{eps}'\n\n")

##############################################################################################
def scan_existing_episodes():
	"""Scans download folders to synchronize the historical database,

	safely purging corrupted stubs under 10MB to trigger self-healing redownloads.
	"""
	import os, re
	config = read_uci_config()
	if not config.get("series"):
		return

	# 🛡️ 10MB Threshold Boundary (10 * 1024 * 1024 bytes)
	# Any video payload under this weight is flagged as an abandoned transfer
	CORRUPT_THRESHOLD_BYTES = 10485760
	changed = False

	for s_id, s_data in config["series"].items():
		folder_path = s_data.get("path", "")
		if not folder_path or not os.path.exists(folder_path):
			continue

		existing_eps = set()
		try:
			for filename in os.listdir(folder_path):
				filename_lower = filename.lower()
				if not filename_lower.endswith(('.mkv', '.mp4', '.avi', '.ts')):
					continue

				full_file_path = os.path.join(folder_path, filename)

				# 🪓 THE FILESYSTEM CLEANUP MATRIX CORE
				try:
					file_size = os.path.getsize(full_file_path)
					if file_size < CORRUPT_THRESHOLD_BYTES:
						log_message(f"[{s_id}] Self-Healing Active: Found broken stub ({filename} - {file_size / 1048576:.2f}MB). Purging asset...")
						os.remove(full_file_path)

						# Wipe matching orphaned metadata text logs if they exist
						base_name, _ = os.path.splitext(filename)
						nfo_path = os.path.join(folder_path, f"{base_name}.nfo")
						if os.path.exists(nfo_path): os.remove(nfo_path)
						continue # Skip mapping this target to history arrays completely
				except OSError as fs_err:
					log_message(f"[{s_id}] Filesystem stat tracking exception: {fs_err}")
					continue

				# Parse out the valid, non-corrupted episode numbers
				match = re.search(r'\b[sS]\d+[eE](\d+)\b|\b(?:ep|episode|e)\.?\s*(\d+)\b', filename)
				if match:
					ep_num = str(int(match.group(1) or match.group(2)))
					existing_eps.add(ep_num)

			# Sift and update local history tracking configurations strings
			current_history = sorted(list(existing_eps), key=int)
			if config["series"][s_id].get("downloaded_episodes") != current_history:
				config["series"][s_id]["downloaded_episodes"] = current_history
				changed = True

		except Exception as dir_err:
			log_message(f"[{s_id}] Directory scanner baseline exception: {dir_err}")

	if changed:
		write_uci_config(config)
		log_message("System filesystem state synchronized. Corrupted tracking keys successfully removed.")

	# 🚀 Return a structured response dictionary for the web API handler
	return {"success": True, "changed": changed}

##############################################################################################
def background_download_worker():
	"""Independent background loop thread runner that sequentially extracts items

	from the thread-safe queue matrix and processes them using completely isolated parameters.
	"""
	import time, os
	global DOWNLOAD_QUEUE

	log_message("[Queue Engine] Thread initialization matrix active. Background download worker standing by...")

	while True:
		try:
			# Blocks indefinitely until an item becomes available in the queue
			task = DOWNLOAD_QUEUE.get()
			
			s_id = task.get("series_id")
			ep_num = task.get("episode")
			download_url = task.get("url")
			dest_folder = task.get("path")
			series_name = task.get("series_name", s_id)
			
			log_message(f"[Queue Engine] Pulling task from buffer: Starting [{series_name}] Episode {ep_num}...")

			# Extract the exact physical directory path structure safely
			target_dir = os.path.dirname(dest_folder)

			if dest_folder.lower().endswith(('.mkv', '.mp4', '.avi')):
				target_folder = os.path.dirname(dest_folder)
			else:
				target_folder = dest_folder

			success = download_url_file(download_url, target_folder, series_name, ep_num)

			if success:
				try:
					live_config = read_uci_config()
					if s_id in live_config.get("series", {}):
						live_downloaded = set(live_config["series"][s_id].get('downloaded_episodes', []))
						live_downloaded.add(ep_num)
						
						live_config["series"][s_id]['downloaded_episodes'] = sorted(list(live_downloaded), key=int)
						live_config["series"][s_id]['last_update'] = str(int(time.time()))
						
						write_uci_config(live_config)
						log_message(f"[Queue Engine] Successfully committed Episode {ep_num} database tracking updates for '{series_name}'.")
				except Exception as db_err:
					log_message(f"[Queue Engine] Database record update hazard warning: {db_err}")
			else:
				log_message(f"[Queue Engine] Alert: Download pipeline failed or aborted for [{series_name}] Ep {ep_num}.")

			DOWNLOAD_QUEUE.task_done()

		except Exception as fatal_worker_err:
			log_message(f"[Queue Engine] Critical error encountered inside thread management core: {fatal_worker_err}")
			time.sleep(5)

##############################################################################################
def download_url_file(url, dest_folder, series_name, ep_num):
	try:
		os.makedirs(dest_folder, exist_ok=True)
		parsed_url = urllib.parse.urlparse(url)
		original_filename = os.path.basename(parsed_url.path).lower()

		# 🕵️‍♂️ Step 1: Parse the current Season indicator
		season_num = 1
		combined_text = (url + " " + original_filename).lower()
		isolated_season_text = re.sub(r'episodes?|ep\s*\d+|epi?\s*\d+', '', combined_text)
		season_match = re.search(r'\b(?:season)[-_\s]*(\d+)|\bs(\d+)\b|\bs0(\d+)', isolated_season_text)
		if season_match:
			detected_season = int(next(g for g in season_match.groups() if g is not None))
			if detected_season < 20:
				season_num = detected_season

		# 🏷️ Step 2: Assemble Standardized Target Filename
		s_str = f"S{season_num:02d}"
		e_str = f"E{int(ep_num):02d}"
		clean_series_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()

		ext_match = re.search(r'\.(mkv|mp4|avi|mov)$', original_filename)
		file_ext = ext_match.group(0) if ext_match else ".mkv"

		standardized_name = f"{clean_series_name} - {s_str}{e_str}{file_ext}"
		dest_path = os.path.join(dest_folder, standardized_name)

		standardized_name = f"{clean_series_name} - {s_str}{e_str}{file_ext}"
		dest_path = os.path.join(dest_folder, standardized_name)

		log_message(f"Normalized download target layout: '{standardized_name}'")

		#======================================================================
		# 🔀 NEW CHIEF ROUTING VALVE: Intercept HLS Playlists before calling curl
		#======================================================================
		if ".m3u8" in url:
			if check_ffmpeg_available():
				log_message(f"[{series_name}] HLS playlist target identified. Spawning native FFmpeg multiplexer...")
				ffmpeg_cmd = [
					"ffmpeg", "-y", "-i", url,
					"-c", "copy",  # Direct copy stream format ensures your NAS CPU stays flat
					"-bsf:a", "aac_adtstoasc",
					dest_path
				]

				process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

				# Keep your web interface alive by sending mock intermediate progress signals
				with progress_lock:
					ACTIVE_DOWNLOADS[series_name] = {
						"episode": ep_num,
						"pct": 50,  # Multiplexers can't easily guess time limits, so we pass a working flag
						"filename": standardized_name
					}

				process.wait()

				with progress_lock:
					if series_name in ACTIVE_DOWNLOADS:
						del ACTIVE_DOWNLOADS[series_name]

				if process.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 100:
					log_message(f"HLS multiplex transaction complete: saved to {dest_path}")
					return True
				else:
					log_message(f"FFmpeg pipeline exited with critical fault code: {process.returncode}")
					if os.path.exists(dest_path):
						os.remove(dest_path)
					return False
			else:
				log_message(f"[{series_name}] Error: Endpoint requires HLS stream parsing, but FFmpeg is not installed on this host environment.")
				return False

		#======================================================================
		# 🚀 Step 3 (Original): Fall back to standard direct file download loop
		#======================================================================
		log_message(f"Spawning live curl process pipe with progress tracking...")
		curl_cmd = [
			"curl", "-#", "-L",
			"-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
			"-H", "Accept: */*",
			"-H", "Accept-Language: en-US,en;q=0.5",
			"--connect-timeout", "15",
			"--retry", "3",
			"-o", dest_path,
			url
		]

		# Spawn the process asynchronously so we can capture lines on the fly
		process = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

		# Spawn the process asynchronously
		process = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

		#======================================================================
		# 📊 STEP 4: Bulletproof \r-Aware Progress Scraper
		#======================================================================
		buffer = ""
		while True:
			# Read small character chunks dynamically from the stream
			chunk = process.stderr.read(10)
			if not chunk and process.poll() is not None:
				break # Exit cleanly when curl exits execution

			buffer += chunk

			# Curl updates lines using carriage returns (\r), so split by \r
			if '\r' in buffer:
				lines = buffer.split('\r')
				# Process all complete status updates, save the last partial chunk
				buffer = lines[-1]

				for status_line in lines[:-1]:
					# Look for percentage updates (e.g., "## 45.2%")
					pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', status_line)
					if pct_match:
						pct_val = int(float(pct_match.group(1)))
						with progress_lock:
							ACTIVE_DOWNLOADS[series_name] = {
								"episode": ep_num,
								"pct": pct_val,
								"filename": standardized_name
							}

		# Block until process execution finishes completely
		process.wait()

		# Wipe download job out of memory allocation dictionary when done
		with progress_lock:
			if series_name in ACTIVE_DOWNLOADS:
				del ACTIVE_DOWNLOADS[series_name]

		if process.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 100:
			log_message(f"Download transaction complete: saved to {dest_path}")
			return True
		else:
			log_message(f"Curl file stream collection failed with return code: {process.returncode}")
			if os.path.exists(dest_path):
				os.remove(dest_path)
			return False
	except Exception as e:
		log_message(f"File transaction write crash on {url}: {e}")
		return False

##############################################################################################
def run_downloader_cycle():
    # Generate a short, unique runtime signature string based on the exact clock ticks
    cycle_token = f"CYCLE-SIG-{int(time.time())}"

    log_message(f"Starting scheduled web crawling sequence cycle... [{cycle_token}]")
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

            with open("/tmp/downloader_debug.html", "w", encoding="utf-8") as df:
                df.write(html)

            # Trim recommendations out of the HTML map instantly
            content_start = html.find('class="entry-content"')
            if content_start != -1:
                content_end = html.find('class="recommended"', content_start)
                if content_end == -1:
                    content_end = html.find('class="related"', content_start)
                if content_end == -1:
                    content_end = html.find('id="comments"', content_start)
                if content_end == -1:
                    content_end = html.find('</article>', content_start)
                html = html[content_start:content_end] if content_end != -1 else html[content_start:]

            # Extract ALL links inside the sanitized body block
            links_found = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
            url_slug_match = re.search(r'dramakey\.com/([^/]+)', url.lower())
            if not url_slug_match:
                url_slug_match = re.search(r'thenkiri\.com/([^/]+)', url.lower())
            series_slug = url_slug_match.group(1).strip() if url_slug_match else ""

            clean_series_slug = series_slug.replace('/', '').strip()
            slug_words = [w for w in clean_series_slug.split('-') if w not in ['s01', 's02', 'complete', 'korean', 'drama', 'series', 'tv', 'download']]
            show_keyword = slug_words[-1] if slug_words else s_id

            for target_url, link_text in links_found:
                clean_text = re.sub(r'<[^>]*>', '', link_text).strip()
                clean_text_lower = clean_text.lower()
                url_lower = target_url.lower()

                if any(x in url_lower for x in ["gmpg.org", "wordpress", "telegram.me", "t.me", "tiktok.com", "twitter.com", "/feed/", "#comment"]):
                    continue

                is_file_locker = "downloadwella" in url_lower or "downloawella" in url_lower or url_lower.endswith(('.mkv', '.mp4', '.avi'))
                if not is_file_locker and clean_series_slug and (clean_series_slug not in url_lower):
                    continue

                if not re.search(r'ep|episode|s\d+|down', clean_text_lower) and not re.search(r's\d+e\d+', url_lower):
                    continue

                ep_numbers = [n for n in re.findall(r'(?:ep|e|episode)[-_\s]*(\d+)', clean_text_lower) if int(n) <= 150]
                if not ep_numbers:
                    ep_numbers = [n for n in re.findall(r's\d+e(\d+)', url_lower) if int(n) <= 150]
                if not ep_numbers:
                    ep_numbers = [n for n in re.findall(r'\b\d+\b', clean_text) if 1 <= int(n) <= 150]

                if not ep_numbers:
                    continue

                for ep_num_raw in ep_numbers:
                    ep_num = str(int(ep_num_raw))

                    live_config = read_uci_config()
                    live_series_data = live_config["series"].get(s_id, {})
                    live_downloaded = set(live_series_data.get('downloaded_episodes',))

                    if ep_num in live_downloaded:
                        continue

                    log_message(f"[{s_id}] Direct download link found for Ep {ep_num}: {target_url}")
                    
                    final_download_url = None

                    # 🎯 THE DOWNLOADWELLA BYPASS VALVE
                    # Connect to the landing layout, read its embedded variables, and post back to bypass the wait walls
                    if "downloadwella.com" in url_lower or "downloawella.com" in url_lower:
                        try:
                            wella_req = urllib.request.Request(target_url, headers=desktop_headers)
                            with urllib.request.urlopen(wella_req) as wella_res:
                                locker_html = wella_res.read().decode('utf-8', errors='ignore')

                            form_inputs = re.findall(r'name=["\'](op|id|rand|referer|method_free|method_premium)["\']\s+value=["\']([^"\']*)["\']', locker_html)
                            form_data = {name: val for name, val in form_inputs}

                            if 'id' in form_data:
                                form_data['op'] = 'download2'
                                form_data['method_free'] = 'Free Download'
                                if 'method_premium' in form_data:
                                    del form_data['method_premium']

                                encoded_data = urllib.parse.urlencode(form_data).encode('utf-8')
                                post_req = urllib.request.Request(
                                    target_url,
                                    data=encoded_data,
                                    headers={
                                        'User-Agent': desktop_headers['User-Agent'],
                                        'Content-Type': 'application/x-www-form-urlencoded',
                                        'Referer': target_url
                                    }
                                )
                                with urllib.request.urlopen(post_req) as post_res:
                                    generated_html = post_res.read().decode('utf-8', errors='ignore')
                                
                                media_match = re.search(r'href=["\'](https?://[^"\']+\.(?:mkv|mp4)[^"\']*)["\']', generated_html, re.IGNORECASE)
                                if media_match:
                                    final_download_url = media_match.group(1)
                        except Exception as locker_err:
                            log_message(f"[{s_id}] Failed intermediate locker generation pass: {locker_err}")

                    # Fall back to direct media streams if present and it wasn't a DownloadWella link
                    if not final_download_url and target_url.lower().endswith(('.mkv', '.mp4', '.avi')):
                        final_download_url = target_url

                    if final_download_url:
                        log_message(f"[{s_id}] True binary stream path resolved! Dispatching to background queue...")

                        series_title = live_series_data.get('name', s_id)
                        sanitized_title = re.sub(r'[^a-zA-Z0-9\s.-]', '', series_title).strip()

                        # Extract season number from the resolved file URL, target URL, or series slug
                        season_match = re.search(r'\bs(\d{1,2})(?:e\d+|\b)', f"{final_download_url} {target_url} {clean_series_slug}", re.IGNORECASE)
                        season_num = season_match.group(1).zfill(2) if season_match else "01"

                        # Use dest_path directly since it already points to the series directory
                        filename = f"{sanitized_title} - S{season_num}E{ep_num.zfill(2)}.mkv"
                        full_target_file_path = os.path.join(dest_path, filename)

                        DOWNLOAD_QUEUE.put({
                            "series_id": s_id,
                            "episode": ep_num,
                            "url": final_download_url,
                            "path": full_target_file_path,
                            "series_name": series_title
                        })
                    else:
                        log_message(f"[{s_id}] Failed to automatically extract true video tokens for Ep {ep_num}.")

        except Exception as e:
            log_message(f"[{s_id}] Main engine execution exception crash: {e}")

    log_message(f"Scheduled sync parsing operation loop finalized. [{cycle_token}]")

##############################################################################################
# Web Server handlers
##############################################################################################
class RouterHTTPHandler(BaseHTTPRequestHandler):
	###########################################################################
	def is_authenticated(self):
		config = read_uci_config()
		if config["global"].get("auth_enabled", "0") == "0":
			return True

		cookie_header = self.headers.get('Cookie', '')
		cookies = urllib.parse.parse_qs(cookie_header.replace(' ', ''))
		session_id = cookies.get('session_id', [None])[0]

		with session_lock:
			if session_id and session_id in ACTIVE_SESSIONS:
				return True
		return False

	###########################################################################
	def serve_login_page(self, error_msg=""):
		self.send_response(200)
		self.send_header('Content-Type', 'text/html; charset=utf-8')
		self.end_headers()

		error_div = f'<div style="color:#dc3545; background:#f8d7da; padding:10px; border-radius:4px; margin-bottom:15px; font-size:0.85rem; border:1px solid #f5c6cb;">⚠️ {error_msg}</div>' if error_msg else ""

	###########################################################################
	def serve_login_page(self, error_msg=""):
		self.send_response(200)
		self.send_header('Content-Type', 'text/html; charset=utf-8')
		self.end_headers()

		error_div = f'<div style="color:#dc3545; background:#f8d7da; padding:10px; border-radius:4px; margin-bottom:15px; font-size:0.85rem; border:1px solid #f5c6cb;">⚠️ {error_msg}</div>' if error_msg else ""

		html_login = f"""<!DOCTYPE html>
		<html lang="en">
		<head>
			<meta charset="UTF-8">
			<meta name="viewport" content="width=device-width, initial-scale=1.0">
			<title>Media Downloader - Login</title>
			<link rel="stylesheet" href="/style.css">
			<script>
				function toggleFieldVisibility(fieldId, element) {{
					var field = document.getElementById(fieldId);
					if (field.type === "password") {{
						field.type = "text";
						element.innerText = "🙈";
					}} else {{
						field.type = "password";
						element.innerText = "👁️";
					}}
				}}
			</script>
		</head>
		<body class="login-body">
			<div class="login-card">
				<h2>
					<img src="/app-icon.png" width="128" height="128" style="display: block;">
					<span class="brand-title">Media Downloader</span>
				</h2>
				{error_div}
				<form action="/login" method="POST">
					<div class="form-group">
						<label>Username:</label>
						<input type="text" name="username" required autofocus placeholder="Enter username">
					</div>
					<div class="form-group">
						<label>Password:</label>
						<div style="position: relative; display: flex; align-items: center;">
							<input type="password" id="login_password" name="password" required placeholder="Enter password" style="padding-right: 45px;">
							<span onclick="toggleFieldVisibility('login_password', this)" style="position: absolute; right: 12px; cursor: pointer; font-size: 0.85rem; font-weight: bold; color: var(--secondary-gray); user-select: none;">👁️</span>
						</div>
					</div>
					<button type="submit">Sign In</button>
				</form>
			</div>
		</body>
		</html>"""
		self.wfile.write(html_login.encode('utf-8'))

	###########################################################################
	def log_message(self, format, *args):
		return

	###########################################################################
	def do_GET(self):
		# 🔓 ASSET PASS GATES: Allow unauthenticated requests to pull design layout tools
		base_dir = os.path.dirname(HTML_FILE)
		if self.path in ['/style.css', '/wallpaper.webp', '/favicon.ico', '/app-icon.png']:
			pass
		elif not self.is_authenticated():
			self.serve_login_page()
			return

		# =====================================================================
		# 🏠 SECURE PATH INTERCEPTOR: Deliver main template on core paths
		# =====================================================================
		if self.path in ['/', '/index.html', '/feeds', '/logs', '/settings', '/queue']:
			# Locate your original index.html static server file block
			# and ensure it executes smoothly here
			try:
				index_file_path = os.path.join(base_dir, 'index.html')
				self.send_response(200)
				self.send_header('Content-Type', 'text/html; charset=utf-8')
				self.end_headers()
				with open(index_file_path, 'rb') as f:
					self.wfile.write(f.read())
			except Exception as e:
				self.send_error(500, f"Dashboard extraction fault: {e}")
			return
		#======================================================================
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
			return
		#======================================================================
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
			return
		#======================================================================
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
			return
		#======================================================================
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
			return
		#======================================================================
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
			return
		#======================================================================
		elif self.path == '/api/series':
			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps(read_uci_config()).encode('utf-8'))
			return
		#======================================================================
		elif self.path == '/api/logs':
			self.send_response(200)
			self.send_header('Content-Type', 'application/json; charset=utf-8')
			self.end_headers()
			with log_lock:
				self.wfile.write(json.dumps(LOG_BUFFER).encode('utf-8'))
			return
		#======================================================================
		elif self.path == '/api/logout':
			# Isolate the active cookie session ID to wipe it out of router RAM memory
			cookie_header = self.headers.get('Cookie', '')
			cookies = urllib.parse.parse_qs(cookie_header.replace(' ', ''))
			session_id = cookies.get('session_id', [None])[0]

			if session_id:
				with session_lock:
					if session_id in ACTIVE_SESSIONS:
						ACTIVE_SESSIONS.remove(session_id)

			# 🧼 Set-Cookie to a past date to instruct the browser to scrub it instantly, then bounce to home login page
			self.send_response(303)
			self.send_header('Set-Cookie', 'session_id=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Strict')
			self.send_header('Location', '/')
			self.end_headers()
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/queue':
			with DOWNLOAD_QUEUE.mutex:
				queued_items = [
					{
						"series_name": item.get("series_name", item.get("series_id", "Unknown")),
						"episode": item.get("episode", "?")
					}
					for item in list(DOWNLOAD_QUEUE.queue)
				]
			with progress_lock:
				active_items = dict(ACTIVE_DOWNLOADS)

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({
				"active": active_items,
				"queued": queued_items
			}).encode('utf-8'))
			return
		else:
			self.send_response(404)
			self.end_headers()

	###########################################################################
	def do_POST(self):
		if self.path == '/login':
			content_length = int(self.headers['Content-Length'])
			post_data = self.rfile.read(content_length).decode('utf-8')
			params = urllib.parse.parse_qs(post_data)

			user_submitted = params.get('username', [None])[0]
			password_submitted = params.get('password', [None])[0]

			config = read_uci_config()
			configured_user = config["global"].get("auth_username", "admin")
			configured_password = config["global"].get("auth_password", "admin")

			if user_submitted == configured_user and password_submitted == configured_password:
				new_session_id = secrets.token_hex(24)
				with session_lock:
					ACTIVE_SESSIONS.add(new_session_id)

				self.send_response(303)
				self.send_header('Set-Cookie', f'session_id={new_session_id}; Path=/; HttpOnly; SameSite=Strict')
				self.send_header('Location', '/')
				self.end_headers()
				return
			else:
				self.serve_login_page("Invalid username or password credentials.")
				return
		#======================================================================
		if not self.is_authenticated():
			self.send_response(401)
			self.end_headers()
			return
		#======================================================================
		if self.path == '/api/logs/clear':
			with log_lock:
				LOG_BUFFER.clear()
				LOG_BUFFER.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] System Activity Log cleared by administrator.")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
		#======================================================================
		if self.path.startswith('/api/series/'):
			s_id = self.path.split('/')[-1]
			content_length = int(self.headers['Content-Length'])
			data = json.loads(self.rfile.read(content_length).decode('utf-8'))
			config = read_uci_config()

			if s_id == '_config':
				if "base_dir" in data:
					config["global"]["base_dir"] = data["base_dir"]
				if "auth_enabled" in data:
					config["global"]["auth_enabled"] = str(data["auth_enabled"])
				if "auth_username" in data:
					config["global"]["auth_username"] = data["auth_username"]
				if "auth_password" in data:
					config["global"]["auth_password"] = data["auth_password"]

				write_uci_config(config)
				self.send_response(200)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
				return

			# This allows numbers, hyphens, and underscores in the tracking profile names
			if not re.match(r'^[a-z0-9_-]+$', s_id):
				self.send_response(400)
				self.end_headers()
				return

			# 🛡️ FIX: Added the clean bracket array properly to prevent SyntaxErrors
			if s_id not in config["series"]:
				config["series"][s_id] = {"downloaded_episodes": [], "last_update": "0"}

			if s_id not in config["series"]:
				config["series"][s_id] = {"downloaded_episodes": [], "last_update": "0"}

			# Capture current/historical state before applying updates to check for unchecked episodes
			existing_eps = set(config["series"][s_id].get("downloaded_episodes", []))
			series_folder_path = data.get("path", config["series"][s_id].get("path", ""))
			series_friendly_name = data.get("name", config["series"][s_id].get("name", s_id))

			# Safely map user text parameter updates
			config["series"][s_id].update({
				"name": series_friendly_name,
				"url": data.get("url"),
				"path": series_folder_path
			})

			# 📋 THE PHYSICAL FILE PURGE SYSTEM
			if "downloaded_episodes" in data:
				# Convert submitted numbers into standardized integers
				submitted_eps = set(str(int(x)) for x in data.get("downloaded_episodes", []))

				# 🎯 ISOLATE THE PRUNED TARGETS: What was in the database, but is now missing from the checkboxes?
				pruned_eps = existing_eps - submitted_eps

				if pruned_eps and os.path.exists(series_folder_path):
					for ep_to_delete in pruned_eps:
						log_message(f"[{s_id}] Pruning trigger activated. Evicting Episode {ep_to_delete} from storage drive...")

						# Standardize the file lookup layout targets (e.g., E02 or Ep 2)
						# This ensures we catch your standardised files safely
						s_str = f"E{int(ep_to_delete):02d}"

						try:
							for filename in os.listdir(series_folder_path):
								filename_lower = filename.lower()

								# Check if the filename explicitly matches this exact episode signature
								# e.g., "The Remarried Empress - S01E01.mkv"
								is_target_file = (s_str.lower() in filename_lower) or \
												 (f"ep{ep_to_delete}" in filename_lower.replace(" ", "")) or \
												 (f"episode{ep_to_delete}" in filename_lower.replace(" ", ""))

								if is_target_file:
									full_target_file_path = os.path.join(series_folder_path, filename)
									if os.path.exists(full_target_file_path):
										# 🪓 UPGRADED: Handle directories (.trickplay) and single files (.nfo / .mkv) recursively
										if os.path.isdir(full_target_file_path):
											import shutil
											shutil.rmtree(full_target_file_path)
											log_message(f"[{s_id}] Successfully wiped physical directory cache: {filename}")
										else:
											os.remove(full_target_file_path)
											log_message(f"[{s_id}] Successfully deleted physical file asset: {filename}")
						except Exception as disk_err:
							log_message(f"[{s_id}] File eviction system filesystem alert: {disk_err}")

				# Save the filtered surviving data records back into the active tracking profile
				config["series"][s_id]["downloaded_episodes"] = sorted(list(submitted_eps), key=int)

			write_uci_config(config)
			log_message(f"Saved tracking profile changes for section: {series_friendly_name}")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/scan-storage':
			try:
				result = scan_existing_episodes()
				payload = result if isinstance(result, dict) else {"success": True}
			except Exception as scan_err:
				log_message(f"[API Error] /api/scan-storage failed: {scan_err}")
				payload = {"success": False, "error": str(scan_err)}

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps(payload).encode('utf-8'))
			return
		#======================================================================
		elif self.path == '/api/sync-updates':
			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			threading.Thread(target=run_downloader_cycle).start()
		#======================================================================
		else:
			self.send_response(404)
			self.end_headers()

	###########################################################################
	def do_DELETE(self):
		#======================================================================
		if not self.is_authenticated():
			self.send_response(401)
			self.end_headers()
			return
		#======================================================================
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
		#======================================================================
		else:
			self.send_response(404)
			self.end_headers()

##############################################################################################
# Standalone script code to launch our app!
##############################################################################################
if __name__ == '__main__':
	# =====================================================================
	# 🚀 BACKGROUND THREAD QUEUE INITIALIZATION BOOTLOADER
	# =====================================================================
	try:
		# Instantiating the thread to target our background downloader function
		queue_thread = threading.Thread(target=background_download_worker, name="DownloadQueueWorker")
		
		# 🛡️ Setting daemon=True ensures this thread terminates when the main server exits
		queue_thread.daemon = True
		
		# Ignition: Spin up the background thread engine
		queue_thread.start()
		
		log_message("[System Boot] Background download queue thread initialized and listening successfully.")
		
	except Exception as thread_boot_err:
		log_message(f"[System Boot] Critical failure launching queue thread subsystem: {thread_boot_err}")

	# =====================================================================
	# 🚀 START WEB SERVER
	# =====================================================================
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
