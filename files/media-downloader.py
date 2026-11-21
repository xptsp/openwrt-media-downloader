#!/usr/bin/env python3
import secrets
import os
import sys
import json
import re
import hashlib
import base64
import hmac
import tempfile
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar
from html import unescape
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
PUSH_SUBSCRIPTIONS_FILE = "/usr/libexec/media-downloader/media-downloader.push"
VAPID_PRIVATE_KEY_FILE = "/usr/libexec/media-downloader/media-downloader.vapid"
SESSION_MAX_AGE = 30 * 24 * 60 * 60

# Global memory log buffer to avoid grinding router flash storage
ACTIVE_DOWNLOADS = {}
progress_lock = threading.Lock()
LOG_BUFFER = []
LOG_MAX_LINES = 150
log_lock = threading.Lock()

ACTIVE_SESSIONS = {}
session_lock = threading.Lock()
push_subscriptions_lock = threading.Lock()
vapid_key_lock = threading.Lock()

##############################################################################################
# 🌐 GLOBAL BACKGROUND TASK QUEUE MATRIX
##############################################################################################
# Thread-safe queue containing dictionaries of tasks: {"series_id": ..., "episode": ..., "url": ..., "path": ...}
DOWNLOAD_QUEUE = queue.Queue()
DOWNLOAD_TASK_LOCK = threading.Lock()
SCHEDULED_DOWNLOAD_TASKS = set()

def download_task_key(task):
	return (str(task.get("series_id", "")), str(task.get("episode", "")))

def enqueue_download_task(task):
	key = download_task_key(task)
	with DOWNLOAD_TASK_LOCK:
		if key in SCHEDULED_DOWNLOAD_TASKS:
			return False
		SCHEDULED_DOWNLOAD_TASKS.add(key)
		try:
			DOWNLOAD_QUEUE.put(task)
		except Exception:
			SCHEDULED_DOWNLOAD_TASKS.discard(key)
			raise
	return True

def release_download_task(task):
	with DOWNLOAD_TASK_LOCK:
		SCHEDULED_DOWNLOAD_TASKS.discard(download_task_key(task))

def remove_queued_download_task(series_id, episode):
	key = (str(series_id), str(episode))
	with DOWNLOAD_TASK_LOCK:
		with DOWNLOAD_QUEUE.mutex:
			queued_tasks = list(DOWNLOAD_QUEUE.queue)
			removed_tasks = [task for task in queued_tasks if download_task_key(task) == key]
			if not removed_tasks:
				return 0
			DOWNLOAD_QUEUE.queue.clear()
			DOWNLOAD_QUEUE.queue.extend(
				task for task in queued_tasks if download_task_key(task) != key
			)
			DOWNLOAD_QUEUE.unfinished_tasks = max(0, DOWNLOAD_QUEUE.unfinished_tasks - len(removed_tasks))
			SCHEDULED_DOWNLOAD_TASKS.discard(key)
			if DOWNLOAD_QUEUE.unfinished_tasks == 0:
				DOWNLOAD_QUEUE.all_tasks_done.notify_all()
	return len(removed_tasks)

def clear_pending_download_tasks():
	with DOWNLOAD_TASK_LOCK:
		with DOWNLOAD_QUEUE.mutex:
			cleared_tasks = list(DOWNLOAD_QUEUE.queue)
			cleared_count = len(cleared_tasks)
			DOWNLOAD_QUEUE.queue.clear()
			DOWNLOAD_QUEUE.unfinished_tasks = max(0, DOWNLOAD_QUEUE.unfinished_tasks - cleared_count)
			for task in cleared_tasks:
				SCHEDULED_DOWNLOAD_TASKS.discard(download_task_key(task))
			if DOWNLOAD_QUEUE.unfinished_tasks == 0:
				DOWNLOAD_QUEUE.all_tasks_done.notify_all()
	return cleared_count

# Shared dictionary to hold progress updates for the CGI frontend /api/progress endpoint
ACTIVE_DOWNLOADS = {}

# Thread lock to guarantee safe data mutations across parallel runtime loops
PROGRESS_LOCK = threading.Lock()

# Flags
ACTIVE_CURL_PROC = None
ABORT_SYNC_FLAG = False
SYNC_LOCK = threading.Lock()

##############################################################################################
# Supporting functions
##############################################################################################
def send_push_notification(title, message, tags="tv,white_check_mark", override_topic=None):
	"""Sends an instant mobile push notification via ntfy.sh if enabled and a topic is configured."""
	config = read_uci_config()
	if not override_topic and config["global"].get("ntfy_enabled", "1") != "1":
		return False

	topic = (override_topic or config["global"].get("ntfy_topic", "")).strip().lstrip("/")
	if not topic:
		return False

	try:
		req = urllib.request.Request(
			f"https://ntfy.sh/{urllib.parse.quote(topic)}",
			data=message.encode("utf-8"),
			headers={
				"Title": title,
				"Tags": tags
			},
			method="POST"
		)
		with urllib.request.urlopen(req, timeout=8) as res:
			return 200 <= res.status < 300
	except Exception as notify_err:
		log_message(f"[Notify Warning] Push alert failed: {notify_err}")
		return False

##############################################################################################
def base64url_encode(value):
	return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

##############################################################################################
def base64url_decode(value):
	if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
		raise ValueError("Invalid base64url value.")
	try:
		return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
	except (ValueError, base64.binascii.Error) as decode_err:
		raise ValueError("Invalid base64url value.") from decode_err

##############################################################################################
def get_vapid_private_key():
	from cryptography.hazmat.primitives.asymmetric import ec

	with vapid_key_lock:
		if os.path.exists(VAPID_PRIVATE_KEY_FILE):
			with open(VAPID_PRIVATE_KEY_FILE, "r", encoding="ascii") as key_file:
				private_value = int.from_bytes(base64url_decode(key_file.read().strip()), "big")
			return ec.derive_private_key(private_value, ec.SECP256R1())

		private_key = ec.generate_private_key(ec.SECP256R1())
		private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
		os.makedirs(os.path.dirname(VAPID_PRIVATE_KEY_FILE), exist_ok=True)
		fd, temporary_path = tempfile.mkstemp(prefix=".media-downloader-vapid-", dir=os.path.dirname(VAPID_PRIVATE_KEY_FILE))
		try:
			os.fchmod(fd, 0o600)
			with os.fdopen(fd, "w", encoding="ascii") as key_file:
				key_file.write(base64url_encode(private_value))
				key_file.flush()
				os.fsync(key_file.fileno())
			try:
				os.link(temporary_path, VAPID_PRIVATE_KEY_FILE)
			except FileExistsError:
				pass
		finally:
			try:
				os.unlink(temporary_path)
			except FileNotFoundError:
				pass

		with open(VAPID_PRIVATE_KEY_FILE, "r", encoding="ascii") as key_file:
			stored_value = int.from_bytes(base64url_decode(key_file.read().strip()), "big")
		return ec.derive_private_key(stored_value, ec.SECP256R1())

##############################################################################################
def get_vapid_public_key():
	from cryptography.hazmat.primitives import serialization

	public_key = get_vapid_private_key().public_key()
	return base64url_encode(public_key.public_bytes(
		serialization.Encoding.X962,
		serialization.PublicFormat.UncompressedPoint
	))

##############################################################################################
def hkdf_expand(prk, info, length):
	output = b""
	previous = b""
	counter = 1
	while len(output) < length:
		previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
		output += previous
		counter += 1
	return output[:length]

##############################################################################################
def create_vapid_token(private_key, endpoint, subject):
	from cryptography.hazmat.primitives import hashes
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

	if not is_valid_vapid_subject(subject):
		raise ValueError("VAPID subject must be a mailto: address or HTTPS URL.")

	parsed_endpoint = urllib.parse.urlsplit(endpoint)
	audience = f"{parsed_endpoint.scheme}://{parsed_endpoint.hostname}"
	now = int(time.time())
	header = base64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
	claims = base64url_encode(json.dumps({
		"aud": audience,
		"exp": now + 12 * 60 * 60,
		"sub": subject
	}, separators=(",", ":")).encode())
	signing_input = f"{header}.{claims}".encode("ascii")
	der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
	r_value, s_value = decode_dss_signature(der_signature)
	signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
	return f"{header}.{claims}.{base64url_encode(signature)}"

##############################################################################################
def is_valid_vapid_subject(subject):
	if not isinstance(subject, str) or len(subject) > 512 or "'" in subject or any(ord(char) < 0x21 for char in subject):
		return False
	if subject.startswith("mailto:"):
		return re.fullmatch(r"mailto:[^@\s]+@[^@\s]+", subject) is not None
	if subject.startswith("https://"):
		try:
			parsed_subject = urllib.parse.urlsplit(subject)
			return bool(parsed_subject.hostname) and not parsed_subject.username and not parsed_subject.password
		except ValueError:
			return False
	return False

##############################################################################################
def encrypt_web_push_payload(subscription, payload):
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM
	from cryptography.hazmat.primitives import serialization

	keys = subscription.get("keys", {})
	public_key_encoded = keys.get("p256dh", "")
	auth_secret_encoded = keys.get("auth", "")
	user_public_bytes = base64url_decode(public_key_encoded)
	auth_secret = base64url_decode(auth_secret_encoded)
	if len(user_public_bytes) != 65 or len(auth_secret) != 16:
		raise ValueError("Invalid push subscription encryption keys.")

	user_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), user_public_bytes)
	server_private_key = ec.generate_private_key(ec.SECP256R1())
	server_public_bytes = server_private_key.public_key().public_bytes(
		serialization.Encoding.X962,
		serialization.PublicFormat.UncompressedPoint
	)
	shared_secret = server_private_key.exchange(ec.ECDH(), user_public_key)
	key_prk = hmac.new(auth_secret, shared_secret, hashlib.sha256).digest()
	key_info = b"WebPush: info\x00" + user_public_bytes + server_public_bytes
	input_keying_material = hkdf_expand(key_prk, key_info, 32)

	salt = os.urandom(16)
	prk = hmac.new(salt, input_keying_material, hashlib.sha256).digest()
	content_encryption_key = hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
	nonce = hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
	ciphertext = AESGCM(content_encryption_key).encrypt(nonce, payload + b"\x02", None)
	return salt + (4096).to_bytes(4, "big") + bytes([len(server_public_bytes)]) + server_public_bytes + ciphertext

##############################################################################################
def validate_push_subscription(subscription):
	if not isinstance(subscription, dict):
		raise ValueError("Expected a push subscription object.")
	endpoint = subscription.get("endpoint")
	if not isinstance(endpoint, str) or len(endpoint) > 4096:
		raise ValueError("Push subscription endpoint is invalid.")
	parsed_endpoint = urllib.parse.urlsplit(endpoint or "")
	host = (parsed_endpoint.hostname or "").lower()
	allowed_host = (
		host == "fcm.googleapis.com"
		or host == "push.services.mozilla.com"
		or host.endswith(".push.services.mozilla.com")
		or host == "web.push.apple.com"
		or host.endswith(".push.apple.com")
		or host.endswith(".notify.windows.com")
	)
	if (
		parsed_endpoint.scheme != "https"
		or not allowed_host
		or parsed_endpoint.username
		or parsed_endpoint.password
		or parsed_endpoint.port not in (None, 443)
		or parsed_endpoint.fragment
	):
		raise ValueError("Unsupported push service endpoint.")

	keys = subscription.get("keys")
	if not isinstance(keys, dict):
		raise ValueError("Push subscription keys are missing.")
	public_key = keys.get("p256dh", "")
	auth_secret = keys.get("auth", "")
	if len(base64url_decode(public_key)) != 65 or len(base64url_decode(auth_secret)) != 16:
		raise ValueError("Push subscription keys are invalid.")
	return {"endpoint": endpoint, "keys": {"p256dh": public_key, "auth": auth_secret}}

##############################################################################################
def read_push_subscriptions():
	try:
		with open(PUSH_SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as subscriptions_file:
			subscriptions = json.load(subscriptions_file)
	except FileNotFoundError:
		return []
	if not isinstance(subscriptions, list):
		raise ValueError("Push subscription store must contain a list.")
	return [validate_push_subscription(subscription) for subscription in subscriptions]

##############################################################################################
def write_push_subscriptions(subscriptions):
	directory = os.path.dirname(PUSH_SUBSCRIPTIONS_FILE)
	os.makedirs(directory, exist_ok=True)
	fd, temporary_path = tempfile.mkstemp(prefix=".media-downloader-push-", dir=directory)
	try:
		os.fchmod(fd, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as subscriptions_file:
			json.dump(subscriptions, subscriptions_file)
			subscriptions_file.flush()
			os.fsync(subscriptions_file.fileno())
		os.replace(temporary_path, PUSH_SUBSCRIPTIONS_FILE)
	except Exception:
		try:
			os.unlink(temporary_path)
		except FileNotFoundError:
			pass
		raise

##############################################################################################
def save_push_subscription(subscription):
	validated = validate_push_subscription(subscription)
	with push_subscriptions_lock:
		subscriptions = read_push_subscriptions()
		subscriptions = [item for item in subscriptions if item["endpoint"] != validated["endpoint"]]
		subscriptions.append(validated)
		write_push_subscriptions(subscriptions)

##############################################################################################
def remove_push_subscription(endpoint):
	with push_subscriptions_lock:
		subscriptions = read_push_subscriptions()
		filtered = [item for item in subscriptions if item["endpoint"] != endpoint]
		if len(filtered) != len(subscriptions):
			write_push_subscriptions(filtered)

##############################################################################################
class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, new_url):
		return None

##############################################################################################
def send_web_push_notification(title, message, url="/queue"):
	try:
		from cryptography.hazmat.primitives import serialization

		config = read_uci_config()
		subject = config["global"].get("vapid_subject", "mailto:admin@example.com").strip()
		private_key = get_vapid_private_key()
		public_key = private_key.public_key().public_bytes(
			serialization.Encoding.X962,
			serialization.PublicFormat.UncompressedPoint
		)
		with push_subscriptions_lock:
			subscriptions = read_push_subscriptions()
		if not subscriptions:
			return False

		def limit_utf8(value, max_bytes):
			return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")

		payload = json.dumps({
			"title": limit_utf8(title, 256),
			"body": limit_utf8(message, 2048),
			"icon": "/app-icon.png",
			"url": url
		}, separators=(",", ":")).encode("utf-8")
		expired_endpoints = []
		success_count = 0
		opener = urllib.request.build_opener(NoRedirectHandler())
		for subscription in subscriptions:
			endpoint = subscription["endpoint"]
			try:
				request = urllib.request.Request(
					endpoint,
					data=encrypt_web_push_payload(subscription, payload),
					headers={
						"Authorization": f"vapid t={create_vapid_token(private_key, endpoint, subject)}, k={base64url_encode(public_key)}",
						"Content-Encoding": "aes128gcm",
						"Content-Type": "application/octet-stream",
						"TTL": "86400",
						"Urgency": "normal"
					},
					method="POST"
				)
				with opener.open(request, timeout=10) as response:
					if 200 <= response.status < 300:
						success_count += 1
			except urllib.error.HTTPError as push_err:
				if push_err.code in (404, 410):
					expired_endpoints.append(endpoint)
				else:
					log_message(f"[Web Push Warning] Delivery failed with HTTP {push_err.code}.")
			except Exception as push_err:
				log_message(f"[Web Push Warning] Delivery failed: {push_err}")

		for endpoint in expired_endpoints:
			remove_push_subscription(endpoint)
		return success_count > 0
	except Exception as push_err:
		log_message(f"[Web Push Warning] Notification could not be sent: {push_err}")
		return False

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
			f.write("    option cache_dir '/mnt/sda1/Downloads/.media-downloader-cache'\n")
			f.write("    option auth_enabled '0'\n")
			f.write("    option auth_username 'admin'\n")
			f.write("    option auth_password 'admin'\n")
			f.write("    option default_series_view 'gallery'\n\n")

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
			"cache_dir": "",
			"movies_dir": "/mnt/hdd/Movies",
			"auth_enabled": "0",
			"auth_username": "admin",
			"auth_password": "admin",
			"allowed_domains": "thenkiri.com,dramakey.com",
			"ntfy_topic": "",
			"ntfy_enabled": "1",
			"vapid_subject": "mailto:admin@example.com",
			"theme_mode": "auto",
			"default_series_view": "gallery"
		},
		"series": {},
		"watchlist": {}
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
					config_dict["series"][current_section_name] = {
						"downloaded_episodes": [],
						"last_update": "0",
						"status": "ongoing",
						"poster": "",
						"poster_cache_file": ""
					}
				elif current_section_type == "watchlist":
					config_dict["watchlist"][current_section_name] = {
						"name": "",
						"source": "thenkiri.com",
						"match_candidates": []
					}
				continue
			opt_match = re.match(r"^option\s+(\w+)\s+'([^']+)'", line)
			if opt_match and current_section_type:
				key, val = opt_match.group(1), opt_match.group(2)
				if current_section_type == "global":
					config_dict["global"][key] = val
				elif current_section_type == "watchlist" and current_section_name:
					if key == "match_candidates":
						try:
							decoded = base64.urlsafe_b64decode(val + "=" * (-len(val) % 4))
							matches = json.loads(decoded.decode("utf-8"))
							if not isinstance(matches, list) or any(
								not isinstance(match, dict)
								or not isinstance(match.get("title"), str)
								or not isinstance(match.get("url"), str)
								for match in matches
							):
								raise ValueError("Invalid Watchlist match candidate data.")
							config_dict["watchlist"][current_section_name][key] = matches
						except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
							log_message(f"[Watchlist] Ignoring invalid saved match choices for '{current_section_name}': {err}")
					else:
						config_dict["watchlist"][current_section_name][key] = val
				elif current_section_type == "series" and current_section_name:
					if key == "downloaded_episodes":
						config_dict["series"][current_section_name][key] = [x.strip() for x in val.split(',') if x.strip()]
					else:
						config_dict["series"][current_section_name][key] = val

	if not config_dict["global"].get("cache_dir"):
		base_dir = config_dict["global"].get("base_dir") or "/mnt/sda1/Downloads"
		config_dict["global"]["cache_dir"] = os.path.join(base_dir, ".media-downloader-cache")

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
		f.write(f"    option cache_dir '{config_data['global'].get('cache_dir', '')}'\n")
		f.write(f"    option movies_dir '{config_data['global'].get('movies_dir', '/mnt/hdd/Movies')}'\n")
		f.write(f"    option auth_enabled '{config_data['global'].get('auth_enabled', '0')}'\n")
		f.write(f"    option auth_username '{config_data['global'].get('auth_username', 'admin')}'\n")
		f.write(f"    option auth_password '{config_data['global'].get('auth_password', 'admin')}'\n")
		f.write(f"    option allowed_domains '{config_data['global'].get('allowed_domains', 'thenkiri.com,dramakey.com')}'\n")
		f.write(f"    option ntfy_topic '{config_data['global'].get('ntfy_topic', '')}'\n")
		f.write(f"    option ntfy_enabled '{config_data['global'].get('ntfy_enabled', '1')}'\n")
		f.write(f"    option vapid_subject '{config_data['global'].get('vapid_subject', 'mailto:admin@example.com')}'\n")
		f.write(f"    option theme_mode '{config_data['global'].get('theme_mode', 'auto')}'\n")
		f.write(f"    option default_series_view '{config_data['global'].get('default_series_view', 'gallery')}'\n\n")

		for s_id, s_data in config_data["series"].items():
			f.write(f"config series '{s_id}'\n")
			f.write(f"    option name '{s_data.get('name', '')}'\n")
			f.write(f"    option url '{s_data.get('url', '')}'\n")
			f.write(f"    option path '{s_data.get('path', '')}'\n")
			f.write(f"    option status '{s_data.get('status', 'ongoing')}'\n")
			f.write(f"    option last_update '{s_data.get('last_update', '0')}'\n")
			f.write(f"    option type '{s_data.get('type', 'series')}'\n")
			if s_data.get('poster'):
				f.write(f"    option poster '{s_data['poster']}'\n")
			if s_data.get('poster_cache_file'):
				f.write(f"    option poster_cache_file '{s_data['poster_cache_file']}'\n")
			eps = ",".join(s_data.get('downloaded_episodes', []))
			f.write(f"    option downloaded_episodes '{eps}'\n\n")

		for w_id, w_data in config_data.get("watchlist", {}).items():
			f.write(f"config watchlist '{w_id}'\n")
			f.write(f"    option name '{w_data.get('name', '')}'\n")
			f.write(f"    option source '{w_data.get('source', 'thenkiri.com')}'\n")
			f.write(f"    option type '{w_data.get('type', 'series')}'\n")
			matches = w_data.get("match_candidates", [])
			if matches:
				encoded_matches = base64.urlsafe_b64encode(
					json.dumps(matches, separators=(",", ":")).encode("utf-8")
				).decode("ascii").rstrip("=")
				f.write(f"    option match_candidates '{encoded_matches}'\n")
			f.write("\n")

##############################################################################################
def scan_existing_episodes():
	"""Scans download folders and Season subfolders to synchronize the historical database,

	safely purging corrupted stubs under 10MB to trigger self-healing redownloads.
	"""
	import os, re
	config = read_uci_config()
	if not config.get("series"):
		return {"success": True, "changed": False}

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
			for root, dirs, files in os.walk(folder_path):
				for filename in files:
					filename_lower = filename.lower()
					if not filename_lower.endswith(('.mkv', '.mp4', '.avi', '.ts')):
						continue

					full_file_path = os.path.join(root, filename)

					# 🪓 THE FILESYSTEM CLEANUP MATRIX CORE
					try:
						file_size = os.path.getsize(full_file_path)
						if file_size < CORRUPT_THRESHOLD_BYTES:
							log_message(f"[{s_id}] Self-Healing Active: Found broken stub ({filename} - {file_size / 1048576:.2f}MB). Purging asset...")
							os.remove(full_file_path)

							# Wipe matching orphaned metadata text logs if they exist
							base_name, _ = os.path.splitext(filename)
							nfo_path = os.path.join(root, f"{base_name}.nfo")
							if os.path.exists(nfo_path):
								os.remove(nfo_path)
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
		task = None
		try:
			# Blocks indefinitely until an item becomes available in the queue
			task = DOWNLOAD_QUEUE.get()
			if ABORT_SYNC_FLAG:
				DOWNLOAD_QUEUE.task_done()
				release_download_task(task)
				task = None
				continue

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

			media_type = task.get("media_type", "series")
			success = download_url_file(download_url, target_folder, series_name, ep_num, media_type, task.get("download_headers"))

			if success:
				try:
					live_config = read_uci_config()
					if s_id in live_config.get("series", {}):
						live_downloaded = set(live_config["series"][s_id].get('downloaded_episodes', []))
						live_downloaded.add(ep_num)

						live_config["series"][s_id]['downloaded_episodes'] = sorted(list(live_downloaded), key=int)
						live_config["series"][s_id]['last_update'] = str(int(time.time()))

						if media_type == "movie":
							live_config["series"][s_id]['status'] = "complete"

						write_uci_config(live_config)
						log_message(f"[Queue Engine] Successfully committed Episode {ep_num} database tracking updates for '{series_name}'.")

						if media_type == "movie":
							send_push_notification(
								"Movie Ready",
								f"{series_name} finished downloading.",
								tags="clapper,white_check_mark"
							)
							send_web_push_notification("Movie Ready", f"{series_name} finished downloading.")
						else:
							send_push_notification(
								"Episode Ready",
								f"{series_name} - E{int(ep_num):02d} finished downloading."
							)
							send_web_push_notification(
								"Episode Ready",
								f"{series_name} - E{int(ep_num):02d} finished downloading."
							)
				except Exception as db_err:
					log_message(f"[Queue Engine] Database record update hazard warning: {db_err}")
			else:
				log_message(f"[Queue Engine] Alert: Download pipeline failed or aborted for [{series_name}] Ep {ep_num}.")

			DOWNLOAD_QUEUE.task_done()
			release_download_task(task)
			task = None

		except Exception as fatal_worker_err:
			if task is not None:
				DOWNLOAD_QUEUE.task_done()
				release_download_task(task)
			log_message(f"[Queue Engine] Critical error encountered inside thread management core: {fatal_worker_err}")
			time.sleep(5)

##############################################################################################
def download_url_file(url, dest_folder, series_name, ep_num, media_type="series", download_headers=None):
	global ACTIVE_CURL_PROC
	process = None
	try:
		# Encode URL path/query characters that curl may otherwise parse as URL syntax.
		url_parts = urllib.parse.urlsplit(url)
		url = urllib.parse.urlunsplit((
			url_parts.scheme,
			url_parts.netloc,
			urllib.parse.quote(url_parts.path, safe="/%:@!$&'*+,;=-._~%"),
			urllib.parse.quote(url_parts.query, safe="=&?/:@!$'*,;%-._~+%"),
			url_parts.fragment
		))
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

		if media_type == "movie":
			standardized_name = f"{clean_series_name}{file_ext}"
		else:
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
				with progress_lock:
					ACTIVE_CURL_PROC = process

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
			"curl", "--globoff", "-#", "-L",
			"-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
			"-H", "Accept: */*",
			"-H", "Accept-Language: en-US,en;q=0.5",
			"--connect-timeout", "15",
			"--retry", "3",
			"-o", dest_path,
			"--write-out", "HTTP_STATUS:%{http_code} CONTENT_TYPE:%{content_type} TRANSFERRED:%{size_download}",
			url
		]
		download_args = []
		for header_name, header_value in (download_headers or {}).items():
			if header_value:
				if header_name.lower() == "referer":
					download_args.extend(["--referer", header_value])
				elif header_name.lower() == "cookie":
					download_args.extend(["--cookie", header_value])
		curl_cmd[-1:-1] = download_args

		# Spawn the process asynchronously so we can capture lines on the fly
		process = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		with progress_lock:
			ACTIVE_CURL_PROC = process

		#======================================================================
		# 📊 STEP 4: Bulletproof \r-Aware Progress Scraper
		#======================================================================
		buffer = ""
		stderr_tail = ""
		while True:
			# Read small character chunks dynamically from the stream
			chunk = process.stderr.read(10)
			if not chunk and process.poll() is not None:
				break # Exit cleanly when curl exits execution

			buffer += chunk
			stderr_tail = (stderr_tail + chunk)[-1000:]

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
		curl_summary = process.stdout.read().strip() if process.stdout else ""

		# Wipe download job out of memory allocation dictionary when done
		with progress_lock:
			if series_name in ACTIVE_DOWNLOADS:
				del ACTIVE_DOWNLOADS[series_name]

		if process.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024 * 100:
			log_message(f"Download transaction complete: saved to {dest_path}")
			return True
		else:
			log_message(f"Curl file stream collection failed with return code: {process.returncode}")
			file_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
			if curl_summary:
				log_message(f"Curl response details: {curl_summary}; saved_bytes={file_size}")
			if stderr_tail.strip():
				log_message(f"Curl stderr tail: {stderr_tail.strip()[-500:]}")
			if os.path.exists(dest_path):
				os.remove(dest_path)
			return False
	except Exception as e:
		log_message(f"File transaction write crash on {url}: {e}")
		return False
	finally:
		with progress_lock:
			if ACTIVE_CURL_PROC is process:
				ACTIVE_CURL_PROC = None

##############################################################################################
def find_all_series_on_site(w_name, w_source, desktop_headers, use_search=True):
	"""Returns a deduplicated list of all matching series links on a source site."""
	clean_target = re.sub(r'[^a-z0-9\s]', '', w_name.lower()).strip()
	if not clean_target:
		return []

	search_url = f"https://{w_source}/?s={urllib.parse.quote_plus(w_name)}" if use_search else f"https://{w_source}/"
	results = []
	seen_urls = set()

	try:
		req = urllib.request.Request(search_url, headers=desktop_headers)
		with urllib.request.urlopen(req, timeout=15) as res:
			html = res.read().decode("utf-8", errors="ignore")
		links = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)

		for href, raw_text in links:
			href_clean = href.split('#')[0].rstrip('/') + '/'
			href_lower = href_clean.lower()

			if href_lower in seen_urls:
				continue
			if any(skip in href_lower for skip in ["/category/", "/tag/", "/feed/", "/page/", "?s=", "t.me", "tiktok.com", "x.com"]):
				continue
			if w_source not in href_lower or href_lower == f"https://{w_source}/":
				continue

			text_clean = re.sub(r'<[^>]*>', '', raw_text).strip()
			text_norm = re.sub(r'[^a-z0-9\s]', '', text_clean.lower()).strip()

			# Skip empty image-wrapper anchor tags that have no readable title text
			if not text_norm or len(text_clean) < 3:
				continue

			if clean_target in text_norm:
				seen_urls.add(href_lower)
				# Clean up "Download " prefix from display title
				display_title = re.sub(r'^(?:Download|Watch)\s+', '', text_clean, flags=re.IGNORECASE).strip()
				results.append({
					"title": display_title,
					"url": href_clean,
					"source": w_source
				})
	except Exception as err:
		log_message(f"[Series Search] Failed querying {search_url}: {err}")

	return results

##############################################################################################
def find_series_matches_on_site(w_name, w_source, desktop_headers, cached_home_links=None, use_search=False):
	"""Returns all matching title links from a site's cached homepage and optional search results."""
	clean_target = re.sub(r'[^a-z0-9\s]', '', w_name.lower()).strip()
	if not clean_target:
		return []

	candidate_pages = []
	matches = []
	seen_urls = set()
	if cached_home_links and w_source in cached_home_links:
		candidate_pages.append(cached_home_links[w_source])

	if use_search:
		search_url = f"https://{w_source}/?s={urllib.parse.quote_plus(w_name)}"
		try:
			req = urllib.request.Request(search_url, headers=desktop_headers)
			with urllib.request.urlopen(req, timeout=15) as res:
				html = res.read().decode("utf-8", errors="ignore")
			search_links = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
			candidate_pages.append(search_links)
		except Exception as err:
			log_message(f"[Watchlist] Search request failed for '{w_name}' on {w_source}: {err}")

	for links in candidate_pages:
		for href, raw_text in links:
			href_lower = href.lower()
			if any(skip in href_lower for skip in ["/category/", "/tag/", "/feed/", "/page/", "?s=", "t.me", "tiktok.com", "x.com"]):
				continue
			if w_source not in href_lower or href_lower.rstrip("/") == f"https://{w_source}":
				continue

			text_clean = re.sub(r'<[^>]*>', '', raw_text)
			text_norm = re.sub(r'[^a-z0-9\s]', '', text_clean.lower()).strip()

			if clean_target in text_norm:
				normalized_url = href.split('#')[0].rstrip("/") + "/"
				if normalized_url.lower() in seen_urls:
					continue
				seen_urls.add(normalized_url.lower())
				display_title = re.sub(r'^(?:Download|Watch)\s+', '', text_clean, flags=re.IGNORECASE).strip()
				matches.append({
					"title": display_title or w_name,
					"url": normalized_url,
					"source": w_source
				})

	return matches

##############################################################################################
def check_watchlist_promotions(desktop_headers, target_w_id=None, use_search=False):
	"""Checks watchlisted titles against source sites, promotes matches to active Feeds,
	removes them from Watchlist, and sends a push alert. Returns True if any item was promoted.
	"""
	config = read_uci_config()
	watchlist = config.get("watchlist", {})
	if not watchlist:
		return False

	items_to_check = [target_w_id] if (target_w_id and target_w_id in watchlist) else list(watchlist.keys())
	if not items_to_check:
		return False

	# Fetch each needed homepage at most once (skip homepage fetch if doing a targeted single-item /?s= probe)
	homepage_links = {}
	if not (target_w_id and use_search):
		sources_needed = set(watchlist[k].get("source", "thenkiri.com").lower() for k in items_to_check)
		for src in sources_needed:
			home_url = f"https://{src}/"
			try:
				req = urllib.request.Request(home_url, headers=desktop_headers)
				with urllib.request.urlopen(req, timeout=15) as res:
					html = res.read().decode("utf-8", errors="ignore")
				homepage_links[src] = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
			except Exception as err:
				log_message(f"[Watchlist] Failed to fetch {home_url}: {err}")

	promoted_any = False
	config_changed = False
	base_dir = config["global"].get("base_dir", "/mnt/hdd/KDramas").rstrip("/")

	for w_id in items_to_check:
		w_data = watchlist[w_id]
		w_name = w_data.get("name", "").strip()
		w_source = w_data.get("source", "thenkiri.com").lower()
		if not w_name:
			continue

		matches = find_series_matches_on_site(
			w_name,
			w_source,
			desktop_headers,
			cached_home_links=homepage_links,
			use_search=use_search
		)

		if len(matches) > 1:
			if w_data.get("match_candidates") != matches:
				w_data["match_candidates"] = matches
				config_changed = True
				log_message(f"[Watchlist] Found {len(matches)} matches for '{w_name}' on {w_source}; waiting for the user to choose one.")
				notification_title = "Watchlist Choice Required"
				notification_message = (
					f"'{w_name}' has {len(matches)} matching items on {w_source}. "
					"Open the Watchlist and choose one to promote to Feeds."
				)
				send_push_notification(notification_title, notification_message, tags="eyes,warning")
				send_web_push_notification(notification_title, notification_message, url="/watchlist")
			continue

		if w_data.get("match_candidates"):
			log_message(f"[Watchlist] '{w_name}' has multiple saved matches and is waiting for the user to choose one.")
			continue

		if len(matches) == 1:
			matched_url = matches[0]["url"]
			w_type = w_data.get("type", "series")
			movies_dir = config["global"].get("movies_dir", "/mnt/hdd/Movies").rstrip("/")
			safe_folder = re.sub(r'[^a-zA-Z0-9\s.\'-]', '', w_name).strip()

			if w_type == "movie":
				target_path = f"{movies_dir}/{safe_folder}"
			else:
				season_match = re.search(r'\b(?:S|Season\s*)0?([2-9]|[1-9]\d)\b', w_name, re.IGNORECASE)
				if season_match:
					base_show = re.sub(r'\s*(?:S|Season\s*)\d+.*$', '', safe_folder, flags=re.IGNORECASE).strip()
					target_path = f"{base_dir}/{base_show}/Season {season_match.group(1).zfill(2)}"
				else:
					target_path = f"{base_dir}/{safe_folder}"

			config["series"][w_id] = {
				"name": w_name,
				"url": matched_url,
				"path": target_path,
				"status": "ongoing",
				"type": w_type,
				"last_update": "0",
				"downloaded_episodes": []
			}
			del config["watchlist"][w_id]
			promoted_any = True
			log_message(f"[Watchlist] Match found on {w_source} for '{w_name}'! Promoted to Feeds ({matched_url}).")

			send_push_notification(
				"Watchlist Promoted",
				f"'{w_name}' appeared on {w_source} and was promoted to Feeds!",
				tags="eyes,rocket"
			)
			send_web_push_notification(
				"Watchlist Promoted",
				f"'{w_name}' appeared on {w_source} and was promoted to Feeds!"
			)

	if promoted_any or config_changed:
		write_uci_config(config)

	return promoted_any

##############################################################################################
SYNC_LOCK = threading.Lock()

def extract_series_poster(html, page_url):
	"""Returns the page's Open Graph image URL, if one is available."""
	match = re.search(
		r'<meta\b(?=[^>]*\bproperty=["\']og:image["\'])(?=[^>]*\bcontent=["\']([^"\']+)["\'])[^>]*>',
		html,
		re.IGNORECASE
	)
	if not match:
		match = re.search(
			r'<img\b(?=[^>]*\bclass=["\'][^"\']*\bwp-post-image\b[^"\']*["\'])(?=[^>]*\b(?:data-src|src)=["\']([^"\']+)["\'])[^>]*>',
			html,
			re.IGNORECASE
		)
	if not match:
		return ""

	poster_url = urllib.parse.urljoin(page_url, unescape(match.group(1).strip()))
	if urllib.parse.urlparse(poster_url).scheme not in ("http", "https"):
		return ""
	return urllib.parse.quote(poster_url, safe=":/?&=%#-._~!$()*+,;@")

POSTER_EXTENSIONS = {
	"image/avif": ".avif",
	"image/gif": ".gif",
	"image/jpeg": ".jpg",
	"image/pjpeg": ".jpg",
	"image/png": ".png",
	"image/webp": ".webp"
}
POSTER_MIME_TYPES = {
	".avif": "image/avif",
	".gif": "image/gif",
	".jpg": "image/jpeg",
	".png": "image/png",
	".webp": "image/webp"
}
MAX_POSTER_BYTES = 8 * 1024 * 1024

def get_cache_directory(config):
	cache_dir = config["global"].get("cache_dir", "").strip()
	if not cache_dir:
		base_dir = config["global"].get("base_dir") or "/mnt/sda1/Downloads"
		cache_dir = os.path.join(base_dir, ".media-downloader-cache")
	return os.path.abspath(os.path.expanduser(cache_dir))

def get_series_poster_cache_path(series_id, cache_dir, cache_filename):
	if not cache_filename:
		return ""
	file_hash = hashlib.sha256(series_id.encode("utf-8")).hexdigest()
	if not re.fullmatch(rf"{file_hash}\.(?:avif|gif|jpg|png|webp)", cache_filename):
		return ""
	return os.path.join(cache_dir, cache_filename)

def cache_series_poster(poster_url, cache_dir, series_id, desktop_headers):
	"""Downloads one image into the configured poster cache and returns its safe filename."""
	parsed_url = urllib.parse.urlparse(poster_url)
	if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
		log_message(f"[Poster Cache] Refusing invalid image URL for '{series_id}'.")
		return ""

	temp_path = None
	try:
		os.makedirs(cache_dir, exist_ok=True)
		headers = dict(desktop_headers)
		headers["Accept"] = "image/avif,image/webp,image/*,*/*;q=0.8"
		req = urllib.request.Request(poster_url, headers=headers)
		with urllib.request.urlopen(req, timeout=15) as res:
			content_type = res.headers.get_content_type().lower()
			extension = POSTER_EXTENSIONS.get(content_type)
			if not extension:
				log_message(f"[Poster Cache] Skipping unsupported image type '{content_type}' for '{series_id}'.")
				return ""

			content_length = res.headers.get("Content-Length")
			if content_length and int(content_length) > MAX_POSTER_BYTES:
				log_message(f"[Poster Cache] Image for '{series_id}' exceeds the 8 MB size limit.")
				return ""
			image_data = res.read(MAX_POSTER_BYTES + 1)

		if not image_data or len(image_data) > MAX_POSTER_BYTES:
			log_message(f"[Poster Cache] Image for '{series_id}' is empty or exceeds the 8 MB size limit.")
			return ""

		file_hash = hashlib.sha256(series_id.encode("utf-8")).hexdigest()
		cache_filename = f"{file_hash}{extension}"
		cache_path = os.path.join(cache_dir, cache_filename)
		with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=".poster-", delete=False) as cache_file:
			temp_path = cache_file.name
			cache_file.write(image_data)
		os.replace(temp_path, cache_path)
		temp_path = None
		return cache_filename
	except Exception as err:
		log_message(f"[Poster Cache] Failed caching image for '{series_id}': {err}")
		return ""
	finally:
		if temp_path and os.path.exists(temp_path):
			try:
				os.remove(temp_path)
			except OSError as cleanup_err:
				log_message(f"[Poster Cache] Failed removing temporary image for '{series_id}': {cleanup_err}")

def persist_series_poster(series_id, series_url, poster_url, cache_filename):
	live_config = read_uci_config()
	live_series = live_config["series"].get(series_id)
	if not live_series or live_series.get("url") != series_url:
		return

	changed = False
	if poster_url and live_series.get("poster") != poster_url:
		live_series["poster"] = poster_url
		changed = True
	if cache_filename and live_series.get("poster_cache_file") != cache_filename:
		live_series["poster_cache_file"] = cache_filename
		changed = True
	if changed:
		write_uci_config(live_config)

def normalize_allowed_domain(domain):
	domain = str(domain).strip().lower()
	if not domain:
		return ""
	if "://" not in domain:
		domain = "//" + domain
	try:
		parsed_domain = urllib.parse.urlsplit(domain)
		if parsed_domain.username or parsed_domain.password:
			return ""
		hostname = (parsed_domain.hostname or "").rstrip(".")
	except ValueError:
		return ""
	return re.sub(r'^www\.', '', hostname)

def extract_html_forms(html):
	"""Parse form actions and named fields with the standard library only."""
	forms = []
	attr_pattern = r'''([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))'''
	for form_match in re.finditer(r'<form\b([^>]*)>(.*?)</form\s*>', html, re.IGNORECASE | re.DOTALL):
		form_attrs = {}
		for match in re.finditer(attr_pattern, form_match.group(1)):
			form_attrs[match.group(1).lower()] = unescape(next(v for v in match.groups()[1:] if v is not None))
		fields = {}
		for input_match in re.finditer(r'<input\b([^>]*)>', form_match.group(2), re.IGNORECASE | re.DOTALL):
			attrs = {}
			for match in re.finditer(attr_pattern, input_match.group(1)):
				attrs[match.group(1).lower()] = unescape(next((v for v in match.groups()[1:] if v is not None), ""))
			if attrs.get("name"):
				fields[attrs["name"]] = attrs.get("value", "")
		forms.append({"action": form_attrs.get("action", ""), "fields": fields})
	return forms

def extract_media_url(html):
	"""Find a direct video URL in a file-host response page."""
	for candidate in re.findall(r'''https?://[^\s"'<>]+''', unescape(html)):
		candidate = candidate.replace('\\/', '/')
		if re.search(r'''\.(?:mkv|mp4|avi)(?:[?#][^\s"'<>]*)?$''', candidate, re.IGNORECASE):
			return candidate.rstrip('.,);')
	return None

def find_xfiles_form(forms, expected_op=""):
	for form in forms:
		if form["fields"].get("op", "").lower() == expected_op:
			return form
	for form in forms:
		if form["fields"].get("id"):
			return form
	return forms[0] if forms else None

def xfiles_countdown_seconds(html):
	patterns = (
		r'''(?:countdown|countdown_str)[^0-9]{0,50}(\d{1,2})''',
		r'''(?:please\s+)?wait[^0-9]{0,30}(\d{1,2})\s*(?:seconds?|secs?)''',
	)
	for pattern in patterns:
		match = re.search(pattern, html, re.IGNORECASE)
		if match:
			return min(30, max(1, int(match.group(1))))
	return 5

def resolve_wetafiles_url(target_url, desktop_headers):
	"""Submit the two-stage free-download forms used by WetaFiles."""
	cookie_jar = http.cookiejar.CookieJar()
	opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
	def resolved_download(url):
		cookie_request = urllib.request.Request(url)
		cookie_jar.add_cookie_header(cookie_request)
		return {
			"url": url,
			"referer": target_url,
			"cookie": cookie_request.get_header("Cookie", "")
		}

	request = urllib.request.Request(target_url, headers=desktop_headers)
	with opener.open(request, timeout=30) as response:
		page_url = response.geturl()
		page_html = response.read().decode('utf-8', errors='ignore')
	forms = extract_html_forms(page_html)
	if not forms:
		media_url = extract_media_url(page_html)
		return resolved_download(media_url) if media_url else None

	for stage in range(2):
		if not forms:
			media_url = extract_media_url(page_html)
			return resolved_download(media_url) if media_url else None
		expected_op = "download1" if stage == 0 else "download2"
		form = find_xfiles_form(forms, expected_op)
		fields = form["fields"]
		if not fields:
			break
		fields.pop("method_premium", None)
		if "method_free" in fields and not fields["method_free"]:
			fields["method_free"] = "Free Download"
		action_url = urllib.parse.urljoin(page_url, form["action"] or page_url)
		post_request = urllib.request.Request(
			action_url,
			data=urllib.parse.urlencode(fields).encode('utf-8'),
			headers={
				'User-Agent': desktop_headers['User-Agent'],
				'Content-Type': 'application/x-www-form-urlencoded',
				'Referer': page_url
			}
		)
		with opener.open(post_request, timeout=30) as response:
			page_url = response.geturl()
			page_html = response.read().decode('utf-8', errors='ignore')
		forms = extract_html_forms(page_html)
		if stage == 0 and find_xfiles_form(forms, "download2"):
			wait_seconds = xfiles_countdown_seconds(page_html)
			log_message(f"[WetaFiles] Waiting {wait_seconds}s for the download link countdown.")
			time.sleep(wait_seconds)
			continue
		media_url = extract_media_url(page_html)
		if media_url:
			return resolved_download(media_url)
	return None

def is_allowed_source_url(url, allowed_domains):
	try:
		parsed_url = urllib.parse.urlparse(url)
		hostname = (parsed_url.hostname or "").lower().rstrip(".")
		port = parsed_url.port
	except ValueError:
		return False

	if parsed_url.scheme not in ("http", "https") or not hostname or parsed_url.username or parsed_url.password:
		return False
	if port is not None and port != (80 if parsed_url.scheme == "http" else 443):
		return False

	hostname = normalize_allowed_domain(hostname)
	allowed_hosts = [normalize_allowed_domain(domain) for domain in allowed_domains]
	return bool(allowed_domains) and any(
		hostname == domain or hostname.endswith("." + domain)
		for domain in allowed_hosts if domain
	)

class AllowedSourceRedirectHandler(urllib.request.HTTPRedirectHandler):
	def __init__(self, allowed_domains):
		super().__init__()
		self.allowed_domains = allowed_domains

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		redirect_url = urllib.parse.urljoin(req.full_url, newurl)
		if not is_allowed_source_url(redirect_url, self.allowed_domains):
			raise urllib.error.HTTPError(
				req.full_url,
				code,
				"Redirect to a non-allowed source domain was blocked.",
				headers,
				fp
			)
		return super().redirect_request(req, fp, code, msg, headers, newurl)

def run_downloader_cycle():
	if not SYNC_LOCK.acquire(blocking=False):
		log_message("[Sync Cycle] Sync cycle already in progress. Skipping duplicate trigger.")
		return

	try:
		global ABORT_SYNC_FLAG
		ABORT_SYNC_FLAG = False

		# Generate a short, unique runtime signature string based on the exact clock ticks
		cycle_token = f"CYCLE-SIG-{int(time.time())}"

		log_message(f"Starting scheduled web crawling sequence cycle... [{cycle_token}]")
		scan_existing_episodes()

		desktop_headers = {
			'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
			'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
			'Accept-Language': 'en-US,en;q=0.5'
		}

		check_watchlist_promotions(desktop_headers)
		config = read_uci_config()
		cache_dir = get_cache_directory(config)

		for s_id, s_data in config["series"].items():
			if ABORT_SYNC_FLAG:
				log_message("[Sync Cycle] Abort signal received. Halting remaining crawler pass.")
				break

			url = s_data.get('url', '')
			dest_path = s_data.get('path', '')
			if not url:
				continue

			poster_url = s_data.get('poster', '')
			cache_filename = s_data.get('poster_cache_file', '')
			cached_poster_path = get_series_poster_cache_path(s_id, cache_dir, cache_filename)
			has_cached_poster = bool(cached_poster_path and os.path.isfile(cached_poster_path))

			if s_data.get('status') == 'complete' and has_cached_poster:
				continue

			if s_data.get('status') == 'complete' and poster_url:
				cache_filename = cache_series_poster(poster_url, cache_dir, s_id, desktop_headers)
				if cache_filename:
					persist_series_poster(s_id, url, poster_url, cache_filename)
					s_data['poster_cache_file'] = cache_filename
				continue

			try:
				req = urllib.request.Request(url, headers=desktop_headers)
				with urllib.request.urlopen(req) as res:
					html = res.read().decode('utf-8', errors='ignore')

				poster_url = poster_url or extract_series_poster(html, url)
				if poster_url:
					if not has_cached_poster:
						cache_filename = cache_series_poster(poster_url, cache_dir, s_id, desktop_headers)
					persist_series_poster(s_id, url, poster_url, cache_filename)
					s_data['poster'] = poster_url
					if cache_filename:
						s_data['poster_cache_file'] = cache_filename

				if s_data.get('status') == 'complete' or not dest_path:
					continue

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

				for target_url, link_text in links_found:
					if ABORT_SYNC_FLAG:
						log_message("[Sync Cycle] Abort signal received. Halting remaining crawler pass.")
						break

					clean_text = re.sub(r'<[^>]*>', '', link_text).strip()
					clean_text_lower = clean_text.lower()
					url_lower = target_url.lower()

					if any(x in url_lower for x in ["gmpg.org", "wordpress", "telegram.me", "t.me", "tiktok.com", "twitter.com", "/feed/", "#comment"]):
						continue

					is_file_locker = "downloadwella" in url_lower or "downloawella" in url_lower or "wetafiles.com" in url_lower or url_lower.endswith(('.mkv', '.mp4', '.avi'))
					if not is_file_locker and clean_series_slug and (clean_series_slug not in url_lower):
						continue

					has_episode_marker = re.search(r'ep|episode|s\d+|down', clean_text_lower)
					has_episode_url_marker = re.search(r's\d+e\d+|(?<![a-z0-9])(?:episode|ep|e)[-_.\s]*\d+', url_lower)
					if not has_episode_marker and not has_episode_url_marker:
						continue

					is_movie = (s_data.get("type") == "movie")

					if not is_movie and not has_episode_marker and not has_episode_url_marker:
						continue

					ep_numbers = [n for n in re.findall(r'(?:ep|e|episode)[-_\s]*(\d+)', clean_text_lower) if int(n) <= 150]
					if not ep_numbers:
						ep_numbers = [n for n in re.findall(r's\d+e(\d+)', url_lower) if int(n) <= 150]
					if not ep_numbers:
						ep_numbers = [n for n in re.findall(r'(?<![a-z0-9])(?:episode|ep|e)[-_.\s]*(\d+)(?!\d)', url_lower) if int(n) <= 150]
					if not ep_numbers:
						ep_numbers = [n for n in re.findall(r'\b\d+\b', clean_text) if 1 <= int(n) <= 150]

					if not ep_numbers:
						if is_movie and (is_file_locker or "down" in clean_text_lower):
							ep_numbers = ["1"]
						else:
							continue

					for ep_num_raw in ep_numbers:
						if ABORT_SYNC_FLAG:
							break
						ep_num = str(int(ep_num_raw))

						live_config = read_uci_config()
						live_series_data = live_config["series"].get(s_id, {})
						live_downloaded = set(live_series_data.get('downloaded_episodes', []))

						if ep_num in live_downloaded:
							continue

						log_message(f"[{s_id}] Direct download link found for Ep {ep_num}: {target_url}")

						final_download_url = None
						final_download_headers = {}

						# Resolve WetaFiles' free-download form flow.
						if "wetafiles.com" in url_lower:
							try:
								resolved_download = resolve_wetafiles_url(target_url, desktop_headers)
								if resolved_download:
									final_download_url = resolved_download.get("url")
									final_download_headers = {
										"referer": resolved_download.get("referer", target_url),
										"cookie": resolved_download.get("cookie", "")
									}
							except Exception as locker_err:
								log_message(f"[{s_id}] Failed WetaFiles download generation pass: {locker_err}")

						# 🎯 THE DOWNLOADWELLA BYPASS VALVE
						elif "downloadwella.com" in url_lower or "downloawella.com" in url_lower:
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
							if ABORT_SYNC_FLAG:
								log_message(f"[{s_id}] Abort flag active. Dropping resolved Ep {ep_num} instead of queuing.")
								break

							series_title = live_series_data.get('name', s_id)
							sanitized_title = re.sub(r'[^a-zA-Z0-9\s.-]', '', series_title).strip()

							# Extract season number from the resolved file URL, target URL, or series slug
							season_match = re.search(r'\bs(\d{1,2})(?:e\d+|\b)', f"{final_download_url} {target_url} {clean_series_slug}", re.IGNORECASE)
							season_num = season_match.group(1).zfill(2) if season_match else "01"

							# Route Season 02+ into a 'Season XX' subfolder if dest_path doesn't already end with one
							target_dir = dest_path
							if int(season_num) >= 2 and not re.search(r'Season\s*\d+$', dest_path.rstrip('/'), re.IGNORECASE):
								target_dir = os.path.join(dest_path, f"Season {season_num}")

							filename = f"{sanitized_title} - S{season_num}E{ep_num.zfill(2)}.mkv"
							full_target_file_path = os.path.join(target_dir, filename)

							download_task = {
								"series_id": s_id,
								"episode": ep_num,
								"url": final_download_url,
								"download_headers": final_download_headers,
								"path": full_target_file_path,
								"series_name": series_title,
								"media_type": "movie" if is_movie else "series"
							}
							if enqueue_download_task(download_task):
								log_message(f"[{s_id}] True binary stream path resolved! Dispatching to background queue...")
							else:
								log_message(f"[{s_id}] Skipping Ep {ep_num}: already queued or downloading.")
						else:
							log_message(f"[{s_id}] Failed to automatically extract true video tokens for Ep {ep_num}.")

			except Exception as e:
				log_message(f"[{s_id}] Main engine execution exception crash: {e}")

		log_message(f"Scheduled sync parsing operation loop finalized. [{cycle_token}]")
	finally:
		SYNC_LOCK.release()

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
			expires_at = ACTIVE_SESSIONS.get(session_id)
			if expires_at:
				if expires_at > time.time():
					return True
				ACTIVE_SESSIONS.pop(session_id, None)
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
		clean_path = self.path.split('?')[0].rstrip('/') or '/series'
		if clean_path in ('/series', '/queue', '/logs', '/settings', '/watchlist'):
			try:
				self.send_response(200)
				self.send_header('Content-Type', 'text/html')
				self.end_headers()
				with open(HTML_FILE, 'rb') as f:
					self.wfile.write(f.read())
			except Exception as e:
				self.send_error(500, f"Dashboard extraction fault: {e}")
			return
		#======================================================================
		elif self.path.rstrip('/') == '/feeds':
			self.send_response(301)
			self.send_header('Location', '/series')
			self.end_headers()
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
		elif self.path.startswith('/api/posters/'):
			s_id = urllib.parse.urlsplit(self.path).path.rsplit('/', 1)[-1]
			if not re.fullmatch(r'^[a-z0-9_-]+$', s_id):
				self.send_response(404)
				self.end_headers()
				return

			config = read_uci_config()
			series_data = config["series"].get(s_id, {})
			cache_path = get_series_poster_cache_path(
				s_id,
				get_cache_directory(config),
				series_data.get("poster_cache_file", "")
			)
			if not cache_path or not os.path.isfile(cache_path):
				self.send_response(404)
				self.end_headers()
				return

			content_type = POSTER_MIME_TYPES.get(os.path.splitext(cache_path)[1].lower())
			if not content_type:
				self.send_response(404)
				self.end_headers()
				return

			with open(cache_path, "rb") as poster_file:
				poster_data = poster_file.read()
			self.send_response(200)
			self.send_header('Content-Type', content_type)
			self.send_header('Content-Length', str(len(poster_data)))
			self.send_header('Cache-Control', 'private, max-age=86400')
			self.send_header('X-Content-Type-Options', 'nosniff')
			self.end_headers()
			self.wfile.write(poster_data)
			return
		#======================================================================
		elif self.path == '/api/series':
			config = read_uci_config()
			cache_dir = get_cache_directory(config)
			for s_id, series_data in config["series"].items():
				cache_path = get_series_poster_cache_path(
					s_id,
					cache_dir,
					series_data.get("poster_cache_file", "")
				)
				series_data["poster"] = f"/api/posters/{s_id}" if cache_path and os.path.isfile(cache_path) else ""
				series_data.pop("poster_cache_file", None)
			config["global"].pop("vapid_private_key", None)

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps(config).encode('utf-8'))
			return
		#======================================================================
		elif self.path == '/api/push/vapid-public-key':
			try:
				public_key = get_vapid_public_key()
			except Exception as key_err:
				log_message(f"[Web Push Error] Could not load VAPID key: {key_err}")
				self.send_response(500)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Web Push is unavailable on the server."}).encode('utf-8'))
				return

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"publicKey": public_key}).encode('utf-8'))
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
					ACTIVE_SESSIONS.pop(session_id, None)

			# 🧼 Set-Cookie to a past date to instruct the browser to scrub it instantly, then bounce to home login page
			self.send_response(303)
			self.send_header('Set-Cookie', 'session_id=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Strict')
			self.send_header('Location', '/')
			self.end_headers()
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/queue':
			with DOWNLOAD_QUEUE.mutex:
				queued_items = [
					{
						"series_id": item.get("series_id", ""),
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
				"queued": queued_items,
				"searching": SYNC_LOCK.locked()
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
				expires_at = time.time() + SESSION_MAX_AGE
				with session_lock:
					ACTIVE_SESSIONS[new_session_id] = expires_at

				self.send_response(303)
				self.send_header('Set-Cookie', f'session_id={new_session_id}; Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Strict')
				self.send_header('Location', '/')
				self.end_headers()
				return
			else:
				self.serve_login_page("Invalid username or password credentials.")
				return
		#======================================================================
		elif not self.is_authenticated():
			self.send_response(401)
			self.end_headers()
			return
		#======================================================================
		elif self.path.rstrip('/') in ('/api/push/subscribe', '/api/push/unsubscribe'):
			content_length = int(self.headers.get('Content-Length', 0))
			try:
				data = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
			except (UnicodeDecodeError, json.JSONDecodeError):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON request body."}).encode('utf-8'))
				return

			if not isinstance(data, dict):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Expected a JSON object."}).encode('utf-8'))
				return

			try:
				if self.path.rstrip('/') == '/api/push/subscribe':
					save_push_subscription(data.get("subscription"))
				else:
					endpoint = data.get("endpoint")
					if not isinstance(endpoint, str):
						raise ValueError("Push subscription endpoint is required.")
					validate_push_subscription({
						"endpoint": endpoint,
						"keys": {
							"p256dh": data.get("p256dh", ""),
							"auth": data.get("auth", "")
						}
					})
					remove_push_subscription(endpoint)
			except ValueError as push_err:
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": str(push_err)}).encode('utf-8'))
				return
			except Exception as push_err:
				log_message(f"[Web Push Error] Could not update subscription: {push_err}")
				self.send_response(500)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Could not update push subscription."}).encode('utf-8'))
				return

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/series/poster-preview':
			content_length = int(self.headers.get('Content-Length', 0))
			try:
				data = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
			except (UnicodeDecodeError, json.JSONDecodeError):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON request body."}).encode('utf-8'))
				return

			if not isinstance(data, dict):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Expected a JSON object."}).encode('utf-8'))
				return

			source_url = str(data.get("url", "")).strip()
			config = read_uci_config()
			allowed_domains = [
				normalized for normalized in (
					normalize_allowed_domain(domain)
					for domain in config["global"].get("allowed_domains", "thenkiri.com,dramakey.com").split(",")
				) if normalized
			]
			if not is_allowed_source_url(source_url, allowed_domains):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "error",
					"message": "Poster previews are limited to HTTP(S) pages on allowed source domains."
				}).encode('utf-8'))
				return

			headers = {
				'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
				'Accept-Language': 'en-US,en;q=0.5'
			}
			opener = urllib.request.build_opener(AllowedSourceRedirectHandler(allowed_domains))
			try:
				request = urllib.request.Request(source_url, headers=headers)
				with opener.open(request, timeout=10) as response:
					page_url = response.geturl()
					html = response.read(2 * 1024 * 1024).decode('utf-8', errors='ignore')
				poster_url = extract_series_poster(html, page_url)
			except (urllib.error.URLError, TimeoutError, OSError) as preview_err:
				log_message(f"[Poster Preview] Failed retrieving {source_url}: {preview_err}")
				self.send_response(502)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "error",
					"message": "Could not retrieve the source page for its poster preview."
				}).encode('utf-8'))
				return

			if not poster_url:
				self.send_response(404)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "not_found",
					"message": "No poster image was found on the source page."
				}).encode('utf-8'))
				return

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({
				"status": "success",
				"poster_url": poster_url
			}).encode('utf-8'))
			return
		#======================================================================
		elif self.path == '/api/logs/clear':
			with log_lock:
				LOG_BUFFER.clear()
				LOG_BUFFER.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] System Activity Log cleared by administrator.")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/series/search':
			content_length = int(self.headers.get('Content-Length', 0))
			data = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
			query_title = (data.get("name") or "").strip()

			if not query_title:
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Series name is required."}).encode('utf-8'))
				return

			config = read_uci_config()
			allowed_raw = config["global"].get("allowed_domains", "thenkiri.com,dramakey.com")
			allowed_list = [d.strip().lower() for d in allowed_raw.split(",") if d.strip()]

			headers = {
				'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
				'Accept-Language': 'en-US,en;q=0.5'
			}

			target_source = (data.get("source") or "").strip().lower()
			domains_to_search = [target_source] if (target_source and target_source in allowed_list) else allowed_list

			all_matches = []
			for domain in domains_to_search:
				all_matches.extend(find_all_series_on_site(query_title, domain, headers, use_search=True))

			if all_matches:
				log_message(f"[Series Search] Found {len(all_matches)} match(es) for '{query_title}'.")
				self.send_response(200)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "success",
					"matches": all_matches
				}).encode('utf-8'))
			else:
				self.send_response(404)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "not_found",
					"message": f"No match found for '{query_title}' on {', '.join(allowed_list)}."
				}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.startswith('/api/series/'):
			s_id = self.path.split('/')[-1]
			content_length = int(self.headers['Content-Length'])
			data = json.loads(self.rfile.read(content_length).decode('utf-8'))
			config = read_uci_config()

			if s_id == '_config':
				if "base_dir" in data:
					config["global"]["base_dir"] = data["base_dir"]
				if "cache_dir" in data:
					config["global"]["cache_dir"] = str(data["cache_dir"]).strip()
				if "movies_dir" in data:
					config["global"]["movies_dir"] = data["movies_dir"].strip()
				if "auth_enabled" in data:
					config["global"]["auth_enabled"] = str(data["auth_enabled"])
				if "auth_username" in data:
					config["global"]["auth_username"] = data["auth_username"]
				if "auth_password" in data:
					config["global"]["auth_password"] = data["auth_password"]
				if "allowed_domains" in data:
					# Normalize comma-separated domains (strip http://, www., and trailing slashes)
					raw_domains = [d.strip().lower() for d in data["allowed_domains"].split(",") if d.strip()]
					clean_domains = [
						normalized for normalized in (
							normalize_allowed_domain(domain) for domain in raw_domains
						) if normalized
					]
					config["global"]["allowed_domains"] = ",".join(clean_domains)
				if "ntfy_topic" in data:
					config["global"]["ntfy_topic"] = data["ntfy_topic"].strip()
				if "ntfy_enabled" in data:
					config["global"]["ntfy_enabled"] = "1" if str(data["ntfy_enabled"]) in ("1", "true", "True") else "0"
				if "vapid_subject" in data:
					subject = str(data["vapid_subject"]).strip()
					if not is_valid_vapid_subject(subject):
						self.send_response(400)
						self.send_header('Content-Type', 'application/json')
						self.end_headers()
						self.wfile.write(json.dumps({"status": "error", "message": "VAPID contact must be a mailto: address or HTTPS URL."}).encode('utf-8'))
						return
					config["global"]["vapid_subject"] = subject
				if "theme_mode" in data:
					mode_val = str(data["theme_mode"]).strip().lower()
					if mode_val in ("auto", "light", "dark"):
						config["global"]["theme_mode"] = mode_val
				if "default_series_view" in data:
					view_val = str(data["default_series_view"]).strip().lower()
					if view_val in ("gallery", "list"):
						config["global"]["default_series_view"] = view_val

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

			# Capture current/historical state before applying updates to check for unchecked episodes
			existing_eps = set(config["series"][s_id].get("downloaded_episodes", []))
			series_folder_path = data.get("path", config["series"][s_id].get("path", ""))
			series_friendly_name = data.get("name", config["series"][s_id].get("name", s_id))

			# Safely map user text parameter updates
			submitted_status = data.get("status", config["series"][s_id].get("status", "ongoing"))
			if submitted_status not in ("ongoing", "complete"):
				submitted_status = "ongoing"

			media_type = data.get("type", config["series"][s_id].get("type", "series"))
			if media_type not in ("series", "movie"):
				media_type = "series"

			config["series"][s_id].update({
				"name": series_friendly_name,
				"url": data.get("url"),
				"path": series_folder_path,
				"status": submitted_status,
				"type": media_type
			})

			# 📋 THE PHYSICAL FILE PURGE SYSTEM
			if "downloaded_episodes" in data and not data.get("skip_prune"):
				# Convert submitted numbers into standardized integers
				submitted_eps = set(str(int(x)) for x in data.get("downloaded_episodes", []))

				# 🎯 ISOLATE THE PRUNED TARGETS: What was in the database, but is now missing from the checkboxes?
				pruned_eps = existing_eps - submitted_eps

				if pruned_eps:
					config["series"][s_id]["status"] = "ongoing"

				if pruned_eps and os.path.exists(series_folder_path):
					import shutil
					for ep_to_delete in pruned_eps:
						log_message(f"[{s_id}] Pruning trigger activated. Evicting Episode {ep_to_delete} from storage drive...")

						s_str = f"E{int(ep_to_delete):02d}"

						try:
							for root, dirs, files in os.walk(series_folder_path):
								# Check subdirectories first (such as .trickplay folders)
								for dirname in list(dirs):
									dir_lower = dirname.lower()
									is_target_dir = (s_str.lower() in dir_lower) or \
													(f"ep{ep_to_delete}" in dir_lower.replace(" ", "")) or \
													(f"episode{ep_to_delete}" in dir_lower.replace(" ", ""))

									if is_target_dir:
										full_dir_path = os.path.join(root, dirname)
										if os.path.exists(full_dir_path):
											shutil.rmtree(full_dir_path)
											dirs.remove(dirname)
											log_message(f"[{s_id}] Successfully wiped physical directory cache: {dirname}")

								# Check individual files (.mkv, .nfo, etc.)
								for filename in files:
									filename_lower = filename.lower()
									is_target_file = (s_str.lower() in filename_lower) or \
													(f"ep{ep_to_delete}" in filename_lower.replace(" ", "")) or \
													(f"episode{ep_to_delete}" in filename_lower.replace(" ", ""))

									if is_target_file:
										full_file_path = os.path.join(root, filename)
										if os.path.exists(full_file_path):
											os.remove(full_file_path)
											log_message(f"[{s_id}] Successfully deleted physical file asset: {filename}")
						except Exception as disk_err:
							log_message(f"[{s_id}] File eviction system filesystem alert: {disk_err}")

			series_url = (data.get("url") or "").strip()
			allowed_raw = config["global"].get("allowed_domains", "thenkiri.com,dramakey.com")
			allowed_list = [d.strip().lower() for d in allowed_raw.split(",") if d.strip()]

			parsed_host = normalize_allowed_domain(series_url)

			if allowed_list and not is_allowed_source_url(series_url, allowed_list):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({
					"status": "error",
					"message": f"Domain '{parsed_host}' is not in your allowed sites list ({', '.join(allowed_list)})."
				}).encode('utf-8'))
				return

			poster_url = str(data.get("poster_url") or "").strip()
			poster_cached = None
			if poster_url:
				if is_allowed_source_url(poster_url, allowed_list):
					config["series"][s_id]["poster"] = poster_url
					cache_headers = {
						'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
					}
					cache_filename = cache_series_poster(poster_url, get_cache_directory(config), s_id, cache_headers)
					poster_cached = bool(cache_filename)
					if cache_filename:
						config["series"][s_id]["poster_cache_file"] = cache_filename
				else:
					log_message(f"[Poster Cache] Refusing unallowed poster URL for '{s_id}'.")
					poster_cached = False

			write_uci_config(config)
			log_message(f"Saved tracking profile changes for section: {series_friendly_name}")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({
				"status": "success",
				"poster_cached": poster_cached
			}).encode('utf-8'))
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
		elif self.path.startswith('/api/watchlist/'):
			w_id = self.path.split('/')[-1]
			if not re.match(r'^[a-z0-9_-]+$', w_id):
				self.send_response(400)
				self.end_headers()
				return

			content_length = int(self.headers['Content-Length'])
			data = json.loads(self.rfile.read(content_length).decode('utf-8'))
			config = read_uci_config()

			allowed_raw = config["global"].get("allowed_domains", "thenkiri.com,dramakey.com")
			allowed_list = [d.strip().lower() for d in allowed_raw.split(",") if d.strip()]

			source = data.get("source", allowed_list[0] if allowed_list else "thenkiri.com").strip().lower()
			if allowed_list and source not in allowed_list:
				source = allowed_list[0]

			media_type = data.get("type", "series")
			if media_type not in ("series", "movie"):
				media_type = "series"

			config.setdefault("watchlist", {})[w_id] = {
				"name": data.get("name", w_id).strip(),
				"source": source,
				"type": media_type
			}
			write_uci_config(config)
			log_message(f"[Watchlist] Added '{data.get('name', w_id)}' watching {source}.")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/queue/clear':
			cleared_count = clear_pending_download_tasks()

			log_message(f"[Queue Engine] User cleared {cleared_count} pending item(s) from the download queue.")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success", "cleared": cleared_count}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/queue/remove':
			content_length = int(self.headers.get('Content-Length', 0))
			try:
				data = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
			except (UnicodeDecodeError, json.JSONDecodeError):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Invalid JSON request body."}).encode('utf-8'))
				return

			if not isinstance(data, dict):
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Expected a JSON object."}).encode('utf-8'))
				return

			series_id = str(data.get("series_id", "")).strip()
			episode = str(data.get("episode", "")).strip()
			if not series_id or not episode:
				self.send_response(400)
				self.send_header('Content-Type', 'application/json')
				self.end_headers()
				self.wfile.write(json.dumps({"status": "error", "message": "Series and episode are required."}).encode('utf-8'))
				return

			removed_count = remove_queued_download_task(series_id, episode)
			if removed_count:
				log_message(f"[Queue Engine] User removed {series_id} Episode {episode} from the pending queue.")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success", "removed": removed_count > 0}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/queue/abort':
			global ABORT_SYNC_FLAG
			ABORT_SYNC_FLAG = True

			cleared_count = clear_pending_download_tasks()

			killed_active = False
			with progress_lock:
				if ACTIVE_CURL_PROC is not None and ACTIVE_CURL_PROC.poll() is None:
					try:
						ACTIVE_CURL_PROC.terminate()
						killed_active = True
					except Exception as kill_err:
						log_message(f"[Queue Engine] Error terminating active curl process: {kill_err}")

				ACTIVE_DOWNLOADS.clear()

			log_message(f"[Queue Engine] Emergency abort triggered! Terminated active download: {killed_active}, cleared {cleared_count} queued item(s).")

			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({
				"status": "success",
				"killed_active": killed_active,
				"cleared": cleared_count
			}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/notify/test':
			success = send_push_notification("Test Notification", "Media Downloader push notifications are working!")
			self.send_response(200 if success else 400)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success" if success else "error"}).encode('utf-8'))
			return
		#======================================================================
		elif self.path.rstrip('/') == '/api/push/test':
			success = send_web_push_notification("Test Notification", "Media Downloader browser push notifications are working!")
			self.send_response(200 if success else 400)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success" if success else "error"}).encode('utf-8'))
			return
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
		elif self.path.startswith('/api/watchlist/'):
			w_id = self.path.split('/')[-1]
			config = read_uci_config()
			if w_id in config.get("watchlist", {}):
				removed_name = config["watchlist"][w_id].get("name", w_id)
				del config["watchlist"][w_id]
				write_uci_config(config)
				log_message(f"[Watchlist] Removed '{removed_name}' from watchlist.")
			self.send_response(200)
			self.send_header('Content-Type', 'application/json')
			self.end_headers()
			self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
			return
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
