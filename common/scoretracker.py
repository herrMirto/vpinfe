"""
Score Tracker Module for VPinFE

Handles:
- WebSocket connection to score server
- Score tracking during gameplay
- Screenshot capture on hotkey
- Score + Screenshot submission to leaderboard API
"""

import threading
import json
import logging
import io
import time
import requests
import platform
from datetime import datetime
from PIL import ImageGrab
from screeninfo import get_monitors
from pynput import keyboard

logger = logging.getLogger(__name__)


def _ts():
    """Return a timestamp string matching VPX log format: 2026-02-10 18:58:43.893"""
    now = datetime.now()
    return now.strftime('%Y-%m-%d %H:%M:%S.') + f'{now.microsecond // 1000:03d}'


def _log(level, msg):
    """Print a log line with VPX-style timestamp."""
    print(f"{_ts()} {level}  [ScoreTracker] {msg}")


class ScoreTracker:
    """Tracks scores from WebSocket and handles screenshot submission."""

    def __init__(self, ini_config, on_notification=None):
        """
        Initialize the ScoreTracker.

        Args:
            ini_config: IniConfig instance with leaderboard settings
            on_notification: Callback function(title, message) for notifications
        """
        self.ini_config = ini_config
        self.on_notification = on_notification or (lambda t, m: None)

        # WebSocket state
        self.ws = None
        self.ws_thread = None
        self.running = False

        # Game session tracking
        self.game_session_data = {}
        self.last_score = {
            'rom_name': None,
            'score': None,
            'timestamp': None
        }

        # Hotkey listener
        self.hotkey_thread = None
        self.hotkey_listener = None

        # Debounce: track last processed game_end per ROM to prevent duplicates
        self._last_game_end = {}  # rom_name -> timestamp

        # Connection timestamp: ignore messages older than when we connected
        self._ws_connected_at = None  # datetime (UTC)

    def is_enabled(self):
        """Check if leaderboard tracking is enabled."""
        if not self.ini_config.config.has_section('Leaderboard'):
            return False
        return self.ini_config.config.get('Leaderboard', 'enabled', fallback='0') == '1'

    def get_config(self):
        """Get leaderboard configuration."""
        config = self.ini_config.config
        if not config.has_section('Leaderboard'):
            return {
                'enabled': False,
                'api_url': '',
                'api_key': '',
                'machine_id': '',
                'score_server_host': 'localhost',
                'score_server_port': '3131',
            }
        return {
            'enabled': config.get('Leaderboard', 'enabled', fallback='0') == '1',
            'api_url': config.get('Leaderboard', 'api_url', fallback=''),
            'api_key': config.get('Leaderboard', 'api_key', fallback=''),
            'machine_id': config.get('Leaderboard', 'machine_id', fallback=''),
            'score_server_host': config.get('Leaderboard', 'score_server_host', fallback='localhost'),
            'score_server_port': config.get('Leaderboard', 'score_server_port', fallback='3131'),
            'send_mode': config.get('Leaderboard', 'send_mode', fallback='manual').lower(),
        }

    def start(self):
        """Start the score tracker (WebSocket + hotkey listener)."""
        _log("INFO", "ScoreTracker.start() called")

        if not self.is_enabled():
            _log("INFO", "Leaderboard tracking is disabled")
            return

        _log("INFO", "Leaderboard tracking is ENABLED")

        self.running = True

        # Start WebSocket connection
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()

        # Start hotkey listener only if in manual mode
        config = self.get_config()
        if config['send_mode'] == 'manual':
            _log("INFO", "Starting hotkey listener (manual mode)")
            self.hotkey_thread = threading.Thread(target=self._run_hotkey_listener, daemon=True)
            self.hotkey_thread.start()
        else:
            _log("INFO", "Automatic mode enabled: Hotkey listener skipped")

        _log("INFO", "ScoreTracker started")

    def stop(self):
        """Stop the score tracker."""
        self.running = False

        if self.ws:
            try:
                self.ws.close()
            except:
                pass

        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except:
                pass

        _log("INFO", "ScoreTracker stopped")

    def _run_websocket(self):
        """Run WebSocket connection in background thread."""
        import websocket

        config = self.get_config()
        url = f"ws://{config['score_server_host']}:{config['score_server_port']}"

        while self.running:
            try:
                _log("INFO", f"Connecting to score server at {url}...")
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close
                )
                self.ws.run_forever()

                if self.running:
                    _log("WARN", "WebSocket connection closed. Reconnecting in 10 seconds...")
                    time.sleep(10)
            except Exception as e:
                _log("ERROR", f"WebSocket error: {e}")
                if self.running:
                    time.sleep(10)

    def _on_ws_open(self, ws):
        """Handle WebSocket connection opened."""
        self._ws_connected_at = datetime.utcnow()
        _log("INFO", f"WebSocket connected (will ignore messages timestamped before {self._ws_connected_at.strftime('%Y-%m-%dT%H:%M:%S')}Z)")

    def _on_ws_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
        except:
            return

        # Ignore stale messages that were queued before we connected
        msg_timestamp = data.get('timestamp', '')
        if msg_timestamp and self._ws_connected_at:
            try:
                # Parse ISO timestamp from score-server (e.g. "2026-02-11T08:43:44.982Z")
                msg_time = datetime.strptime(msg_timestamp.replace('Z', ''), '%Y-%m-%dT%H:%M:%S.%f')
                if msg_time < self._ws_connected_at:
                    msg_type = data.get('type', '')
                    _log("INFO", f"Ignoring stale {msg_type} message (timestamp={msg_timestamp}, connected at {self._ws_connected_at.strftime('%Y-%m-%dT%H:%M:%S')}Z)")
                    return
            except ValueError:
                pass  # If timestamp parsing fails, process the message normally

        rom_name = data.get('rom', 'unknown_rom')
        msg_type = data.get('type', '')

        if msg_type in ['table_loaded', 'game_start']:
            self.game_session_data[rom_name] = {}
            # Clear debounce on new game start so next game_end is accepted
            self._last_game_end.pop(rom_name, None)
            _log("INFO", f"Game started: {rom_name}")
            return

        if msg_type == 'game_end':
            reason = data.get('reason', '')

            # Ignore plugin_unload events — the game was already ended properly
            if reason == 'plugin_unload':
                _log("INFO", f"Ignoring game_end (plugin_unload) for: {rom_name}")
                self.game_session_data.pop(rom_name, None)
                return

            # Debounce: ignore duplicate game_end for the same ROM within 10 seconds
            now = time.time()
            last = self._last_game_end.get(rom_name, 0)
            if now - last < 10:
                _log("WARN", f"Ignoring duplicate game_end for {rom_name} (received {now - last:.1f}s after previous)")
                return
            self._last_game_end[rom_name] = now

            _log("INFO", f"Game ended: {rom_name} (reason={reason})")

            # Find the highest score from all players
            best_score = 0

            # Prefer scores from the game_end payload (sent by score-server)
            end_scores = data.get('scores', [])
            if end_scores:
                _log("INFO", f"Using scores from game_end payload ({len(end_scores)} players)")
                for p_data in end_scores:
                    try:
                        raw_score = p_data.get('score', 0)
                        score = int(str(raw_score).replace(',', '').replace('.', '').lstrip('0') or 0)
                        if score > best_score:
                            best_score = score
                    except:
                        pass
            # Fallback: use accumulated session data (backward compatibility)
            elif rom_name in self.game_session_data and self.game_session_data[rom_name]:
                _log("INFO", f"No scores in game_end payload, using accumulated session data")
                for player_id, p_data in self.game_session_data[rom_name].items():
                    try:
                        raw_score = p_data.get('score', 0)
                        score = int(str(raw_score).replace(',', '').replace('.', '').lstrip('0') or 0)
                        if score > best_score:
                            best_score = score
                    except:
                        pass
            else:
                _log("WARN", f"game_end received for {rom_name} but no scores available (not in payload, not in session)")

            if best_score > 0:
                # Store as last score for screenshot submission
                self.last_score = {
                    'rom_name': rom_name,
                    'score': best_score,
                    'timestamp': datetime.now()
                }
                _log("INFO", f"Last score updated: {rom_name} - {best_score:,}")

                # Check for automatic submission
                config = self.get_config()
                if config['send_mode'] == 'automatic':
                    _log("INFO", "Automatic mode: Triggering submission in 2 seconds...")

                    def auto_submit():
                        # Small delay to ensure any end-game screen/animations settle
                        time.sleep(2)
                        self.submit_score_with_screenshot()

                    threading.Thread(target=auto_submit, daemon=True).start()

            # Clean up session
            self.game_session_data.pop(rom_name, None)
            return

        if msg_type == 'current_scores':
            if rom_name not in self.game_session_data:
                self.game_session_data[rom_name] = {}

            for p_data in data.get('scores', []):
                try:
                    p_label = str(p_data.get('player', ''))
                    p_score = p_data.get('score', 0)
                    p_id = p_label.replace("Player", "").strip() if "Player" in p_label else p_label

                    self.game_session_data[rom_name][p_id] = {
                        'score': p_score,
                        'ball': data.get('current_ball')
                    }
                except Exception as e:
                    _log("ERROR", f"Error parsing player data: {e}")

    def _on_ws_error(self, ws, error):
        """Handle WebSocket errors."""
        _log("ERROR", f"WebSocket error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        _log("INFO", f"WebSocket connection closed. Reconnecting in 10 seconds...")

    def _run_hotkey_listener(self):
        """Run hotkey listener in background thread."""
        # Define the hotkey combination based on OS
        system = platform.system()
        if system == 'Darwin':  # macOS
            COMBO = {keyboard.Key.cmd, keyboard.Key.shift, keyboard.KeyCode.from_char('s')}
            hotkey_label = "Cmd+Shift+S"
        else:  # Linux, Windows, and others
            COMBO = {keyboard.Key.ctrl, keyboard.Key.shift, keyboard.KeyCode.from_char('s')}
            hotkey_label = "Ctrl+Shift+S"

        _log("INFO", f"Starting hotkey listener ({hotkey_label} for screenshot submission)...")
        current_keys = set()

        def on_press(key):
            current_keys.add(key)
            if COMBO.issubset(current_keys):
                self._on_screenshot_hotkey()

        def on_release(key):
            current_keys.discard(key)

        self.hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.hotkey_listener.start()
        self.hotkey_listener.join()

    def _on_screenshot_hotkey(self):
        """Handle screenshot hotkey press."""
        _log("INFO", "Screenshot hotkey triggered")
        # Run submission in a separate thread to not block the hotkey listener
        threading.Thread(target=self.submit_score_with_screenshot, daemon=True).start()

    def submit_score_with_screenshot(self):
        """Capture screenshot and submit score + screenshot to API."""
        _log("INFO", f"submit_score_with_screenshot called, last_score={self.last_score}")

        if not self.last_score['rom_name']:
            _log("WARN", "No score available to submit")
            self.on_notification("Error", "No score available!\nPlay a game first.")
            return

        config = self.get_config()

        if not config['api_url'] or not config['api_key']:
            _log("ERROR", "API URL or API Key not configured")
            self.on_notification("Error", "Leaderboard not configured!")
            return

        try:
            # Capture screenshot from appropriate screen
            _log("INFO", "Capturing screenshot...")
            screenshot = self._capture_screenshot()
            if not screenshot:
                _log("ERROR", "Screenshot capture returned None")
                self.on_notification("Error", "Failed to capture screenshot")
                return

            _log("INFO", f"Screenshot captured: {screenshot.size}")

            # Convert to bytes
            buffer = io.BytesIO()
            screenshot.save(buffer, format='PNG')
            buffer.seek(0)

            # Prepare multipart form data
            files = {
                'screenshot': ('screenshot.png', buffer, 'image/png')
            }
            data = {
                'apiKey': config['api_key'],
                'machineID': config['machine_id'],
                'romName': self.last_score['rom_name'],
                'score': str(self.last_score['score']),
            }

            # Submit to API
            api_url = config['api_url'].rstrip('/')
            endpoint = f"{api_url}/api/submit-score-with-screenshot"

            _log("INFO", f"Submitting score to {endpoint} - romName={data['romName']}, score={data['score']}")

            response = requests.post(endpoint, files=files, data=data, timeout=30)
            response.raise_for_status()

            result = response.json()
            _log("INFO", f"Response: status={response.status_code}, result={result}")

            if result.get('success'):
                score_formatted = f"{self.last_score['score']:,}"
                table_name = result.get('tableName', self.last_score['rom_name'])
                _log("INFO", f"Score submitted successfully: {table_name} - {score_formatted}")
                self.on_notification(
                    "Score Submitted!",
                    f"Table: {table_name}\nScore: {score_formatted}"
                )
                # Clear last score after successful submission
                self.last_score = {'rom_name': None, 'score': None, 'timestamp': None}
            else:
                raise Exception(result.get('error', 'Unknown error'))

        except requests.exceptions.RequestException as e:
            _log("ERROR", f"API request failed: {e}")
            self.on_notification("Error", f"Failed to submit:\n{str(e)[:50]}")
        except Exception as e:
            _log("ERROR", f"Screenshot submission failed: {e}")
            import traceback
            traceback.print_exc()
            self.on_notification("Error", f"Submission failed:\n{str(e)[:50]}")

    def _capture_screenshot(self):
        """
        Capture screenshot from the appropriate screen.

        Priority:
        1. If dmdscreenid is set, capture that screen
        2. Else if bgscreenid is set, capture that screen
        3. Else capture primary screen
        """
        try:
            monitors = get_monitors()
            config = self.ini_config.config

            # Determine which screen to capture
            screen_id = None

            dmdscreenid = config.get('Displays', 'dmdscreenid', fallback='').strip()
            bgscreenid = config.get('Displays', 'bgscreenid', fallback='').strip()

            if dmdscreenid:
                screen_id = int(dmdscreenid)
                _log("INFO", f"Capturing DMD screen (id={screen_id})")
            elif bgscreenid:
                screen_id = int(bgscreenid)
                _log("INFO", f"Capturing BG screen (id={screen_id})")
            else:
                _log("INFO", "Capturing primary screen")
                return ImageGrab.grab()

            # Capture specific monitor
            if screen_id is not None and screen_id < len(monitors):
                mon = monitors[screen_id]
                bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)
                _log("INFO", f"Screenshot bbox: {bbox}")
                return ImageGrab.grab(bbox=bbox)

            # Fallback to primary
            return ImageGrab.grab()

        except Exception as e:
            _log("ERROR", f"Screenshot capture failed: {e}")
            return None

    def get_last_score(self):
        """Get the last tracked score."""
        return self.last_score.copy()

    def has_pending_score(self):
        """Check if there's a pending score to submit."""
        return self.last_score['rom_name'] is not None
