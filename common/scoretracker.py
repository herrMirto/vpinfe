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
import requests
import platform
from datetime import datetime
from PIL import ImageGrab
from screeninfo import get_monitors
from pynput import keyboard

logger = logging.getLogger(__name__)


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
        print("=== ScoreTracker.start() called ===")
        logger.info("=== ScoreTracker.start() called ===")

        if not self.is_enabled():
            print("Leaderboard tracking is disabled")
            logger.info("Leaderboard tracking is disabled")
            return

        print("Leaderboard tracking is ENABLED")

        self.running = True

        # Start WebSocket connection
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()

        # Start hotkey listener only if in manual mode
        config = self.get_config()
        if config['send_mode'] == 'manual':
            logger.info("Starting hotkey listener (manual mode)")
            self.hotkey_thread = threading.Thread(target=self._run_hotkey_listener, daemon=True)
            self.hotkey_thread.start()
        else:
            logger.info("Automatic mode enabled: Hotkey listener skipped")

        logger.info("ScoreTracker started")

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

        logger.info("ScoreTracker stopped")

    def _run_websocket(self):
        """Run WebSocket connection in background thread."""
        import websocket

        config = self.get_config()
        url = f"ws://{config['score_server_host']}:{config['score_server_port']}"

        while self.running:
            try:
                logger.info(f"Connecting to score server at {url}...")
                self.ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close
                )
                self.ws.run_forever()

                if self.running:
                    logger.warning("WebSocket connection closed. Reconnecting in 10 seconds...")
                    import time
                    time.sleep(10)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                if self.running:
                    import time
                    time.sleep(10)

    def _on_ws_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
        except:
            return

        rom_name = data.get('rom', 'unknown_rom')
        msg_type = data.get('type', '')

        if msg_type in ['table_loaded', 'game_start']:
            self.game_session_data[rom_name] = {}
            logger.info(f"Game started: {rom_name}")
            return

        if msg_type == 'game_end':
            if rom_name in self.game_session_data and self.game_session_data[rom_name]:
                logger.info(f"Game ended: {rom_name}")
                # Find the highest score from all players
                best_score = 0
                for player_id, p_data in self.game_session_data[rom_name].items():
                    try:
                        raw_score = p_data.get('score', 0)
                        score = int(str(raw_score).replace(',', '').replace('.', '').lstrip('0') or 0)
                        if score > best_score:
                            best_score = score
                    except:
                        pass

                if best_score > 0:
                    # Store as last score for screenshot submission
                    self.last_score = {
                        'rom_name': rom_name,
                        'score': best_score,
                        'timestamp': datetime.now()
                    }
                    logger.info(f"Last score updated: {rom_name} - {best_score:,}")

                    # Check for automatic submission
                    config = self.get_config()
                    if config['send_mode'] == 'automatic':
                        logger.info("Automatic mode: Triggering submission in 2 seconds...")
                        
                        def auto_submit():
                            import time
                            # Small delay to ensure any end-game screen/animations settle
                            time.sleep(2) 
                            self.submit_score_with_screenshot()
                            
                        threading.Thread(target=auto_submit, daemon=True).start()

                del self.game_session_data[rom_name]
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
                    logger.error(f"Error parsing player data: {e}")

    def _on_ws_error(self, ws, error):
        """Handle WebSocket errors."""
        logger.error(f"WebSocket error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")

    def _run_hotkey_listener(self):
        """Run hotkey listener in background thread."""
        print("=== _run_hotkey_listener started ===")
        
        # Define the hotkey combination based on OS
        system = platform.system()
        if system == 'Darwin':  # macOS
            COMBO = {keyboard.Key.cmd, keyboard.Key.shift, keyboard.KeyCode.from_char('s')}
            hotkey_label = "Cmd+Shift+S"
        else:  # Linux, Windows, and others
            COMBO = {keyboard.Key.ctrl, keyboard.Key.shift, keyboard.KeyCode.from_char('s')}
            hotkey_label = "Ctrl+Shift+S"
        
        logger.info(f"Starting hotkey listener ({hotkey_label} for screenshot submission)...")
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
        print("=== HOTKEY PRESSED! Cmd+Shift+S ===")
        logger.info("Screenshot hotkey triggered (Cmd+Shift+S)")
        # Run submission in a separate thread to not block the hotkey listener
        threading.Thread(target=self.submit_score_with_screenshot, daemon=True).start()

    def submit_score_with_screenshot(self):
        """Capture screenshot and submit score + screenshot to API."""
        logger.info("=== submit_score_with_screenshot called ===")
        logger.info(f"Last score: {self.last_score}")

        if not self.last_score['rom_name']:
            logger.warning("No score available to submit")
            self.on_notification("Error", "No score available!\nPlay a game first.")
            return

        config = self.get_config()
        logger.info(f"Config: api_url={config['api_url']}, api_key={'*' * len(config['api_key']) if config['api_key'] else 'None'}")

        if not config['api_url'] or not config['api_key']:
            logger.error("API URL or API Key not configured")
            self.on_notification("Error", "Leaderboard not configured!")
            return

        try:
            # Capture screenshot from appropriate screen
            logger.info("Capturing screenshot...")
            screenshot = self._capture_screenshot()
            if not screenshot:
                logger.error("Screenshot capture returned None")
                self.on_notification("Error", "Failed to capture screenshot")
                return

            logger.info(f"Screenshot captured: {screenshot.size}")

            # Convert to bytes
            buffer = io.BytesIO()
            screenshot.save(buffer, format='PNG')
            buffer.seek(0)
            logger.info(f"Screenshot buffer size: {len(buffer.getvalue())} bytes")

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

            logger.info(f"Submitting score to {endpoint}...")
            logger.info(f"Data: romName={data['romName']}, score={data['score']}")

            response = requests.post(endpoint, files=files, data=data, timeout=30)
            logger.info(f"Response status: {response.status_code}")
            response.raise_for_status()

            result = response.json()
            logger.info(f"Response JSON: {result}")

            if result.get('success'):
                score_formatted = f"{self.last_score['score']:,}"
                table_name = result.get('tableName', self.last_score['rom_name'])
                print(f"=== SUCCESS! Score submitted: {table_name} - {score_formatted} ===")
                logger.info(f"=== Score submitted successfully: {score_formatted} ===")
                self.on_notification(
                    "Score Submitted!",
                    f"Table: {table_name}\nScore: {score_formatted}"
                )
                # Clear last score after successful submission
                self.last_score = {'rom_name': None, 'score': None, 'timestamp': None}
            else:
                raise Exception(result.get('error', 'Unknown error'))

        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            self.on_notification("Error", f"Failed to submit:\n{str(e)[:50]}")
        except Exception as e:
            logger.error(f"Screenshot submission failed: {e}")
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
                logger.info(f"Capturing DMD screen (id={screen_id})")
            elif bgscreenid:
                screen_id = int(bgscreenid)
                logger.info(f"Capturing BG screen (id={screen_id})")
            else:
                logger.info("Capturing primary screen")
                # Capture primary screen (no bbox = full primary)
                return ImageGrab.grab()

            # Capture specific monitor
            if screen_id is not None and screen_id < len(monitors):
                mon = monitors[screen_id]
                bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)
                logger.info(f"Screenshot bbox: {bbox}")
                return ImageGrab.grab(bbox=bbox)

            # Fallback to primary
            return ImageGrab.grab()

        except Exception as e:
            logger.error(f"Screenshot capture failed: {e}")
            return None

    def get_last_score(self):
        """Get the last tracked score."""
        return self.last_score.copy()

    def has_pending_score(self):
        """Check if there's a pending score to submit."""
        return self.last_score['rom_name'] is not None
