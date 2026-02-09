#!/usr/bin/env python3

import webview
from pathlib import Path
from screeninfo import get_monitors
from frontend.customhttpserver import CustomHTTPServer
from frontend.api import API
import threading
from common.iniconfig import IniConfig
from common.scoretracker import ScoreTracker
import sys
import os
from clioptions import parseArgs
from managerui.managerui import start_manager_ui, stop_manager_ui
from nicegui import app as nicegui_app
from platformdirs import user_config_dir
from common.themes import ThemeRegistry

#debug
import sys
from common.vpxcollections import VPXCollections
from common.tableparser import TableParser

# Get the base path
base_path = os.path.dirname(os.path.abspath(__file__))

nicegui_app.add_static_files('/static', os.path.join(base_path, 'managerui/static'))
html_file = Path(base_path) / "web/splash.html"
notification_file = Path(base_path) / "web/notification.html"
webview_windows = [] # [ [window_name, window, api] ]
notification_window = None

# Use platform-specific config directory
config_dir = Path(user_config_dir("vpinfe", "vpinfe"))
config_dir.mkdir(parents=True, exist_ok=True)
config_path = config_dir / "vpinfe.ini"
iniconfig = IniConfig(str(config_path))

 # The last window created will be the one in focus.  AKA the controller for all the other windows!!!! Always "table"
import sys
import webview

def loadWindows():
    global webview_windows
    global api
    global notification_window
    monitors = get_monitors()
    print(monitors)

    is_mac = sys.platform == "darwin"

    # macOS-safe window flags
    window_flags = {
        "fullscreen": not is_mac,
        "frameless": is_mac,
        "resizable": False if is_mac else True,
    }

    # --- BG SCREEN ---
    if iniconfig.config['Displays']['bgscreenid']:
        screen_id = int(iniconfig.config['Displays']['bgscreenid'])
        api = API(iniconfig)

        win = webview.create_window(
            "BG Screen",
            url=f"file://{html_file.resolve()}",
            js_api=api,
            x=monitors[screen_id].x,
            y=monitors[screen_id].y,
            width=monitors[screen_id].width,
            height=monitors[screen_id].height,
            background_color="#000000",
            fullscreen=window_flags["fullscreen"],
            frameless=window_flags["frameless"],
            resizable=window_flags["resizable"],
        )

        api.myWindow.append(win)
        webview_windows.append(['bg', win, api])
        api.webview_windows = webview_windows
        api.iniConfig = iniconfig
        api._finish_setup()

    # --- DMD SCREEN ---
    if iniconfig.config['Displays']['dmdscreenid']:
        screen_id = int(iniconfig.config['Displays']['dmdscreenid'])
        api = API(iniconfig)

        win = webview.create_window(
            "DMD Screen",
            url=f"file://{html_file.resolve()}",
            js_api=api,
            x=monitors[screen_id].x,
            y=monitors[screen_id].y,
            width=monitors[screen_id].width,
            height=monitors[screen_id].height,
            background_color="#000000",
            fullscreen=window_flags["fullscreen"],
            frameless=window_flags["frameless"],
            resizable=window_flags["resizable"],
        )

        api.myWindow.append(win)
        webview_windows.append(['dmd', win, api])
        api.webview_windows = webview_windows
        api.iniConfig = iniconfig
        api._finish_setup()

    # --- TABLE SCREEN (ALWAYS LAST) ---
    if iniconfig.config['Displays']['tablescreenid']:
        screen_id = int(iniconfig.config['Displays']['tablescreenid'])
        api = API(iniconfig)

        win = webview.create_window(
            "Table Screen",
            url=f"file://{html_file.resolve()}",
            js_api=api,
            x=monitors[screen_id].x,
            y=monitors[screen_id].y,
            width=monitors[screen_id].width,
            height=monitors[screen_id].height,
            background_color="#000000",
            fullscreen=window_flags["fullscreen"],
            frameless=True if is_mac else False,  # force frameless for table on mac
            resizable=window_flags["resizable"],
        )

        api.myWindow.append(win)
        webview_windows.append(['table', win, api])
        api.webview_windows = webview_windows
        api.iniConfig = iniconfig
        api._finish_setup()

    # Always create notification overlay
    try:
        # Use primary monitor or default to first
        primary_mon = monitors[0]
        for m in monitors:
            if m.is_primary:
                primary_mon = m
                break
        
        print(f"Creating notification overlay on monitor: {primary_mon}")
        # Calculate position for top-right corner
        # Window size: 370x160
        win_width = 370
        win_height = 160
        win_x = primary_mon.x + primary_mon.width - win_width - 20 # 20px padding from right
        win_y = primary_mon.y + 20 # 20px padding from top

        notification_window = webview.create_window(
            "Notification Overlay",
            url=f"file://{notification_file.resolve()}",
            transparent=True,
            frameless=True,
            on_top=True,
            x=int(win_x),
            y=int(win_y),
            width=win_width,
            height=win_height,
            focus=False
        )
        print(f"Notification overlay created at {win_x},{win_y} ({win_width}x{win_height})")
    except Exception as e:
        print(f"Failed to create notification overlay: {e}")


if len(sys.argv) > 0:
    parseArgs()

# Initialize theme registry and auto-install default themes
try:
    theme_registry = ThemeRegistry()
    theme_registry.load_registry()
    theme_registry.load_theme_manifests()
    theme_registry.auto_install_defaults()
except Exception as e:
    print(f"[WARN] Theme registry initialization failed: {e}")

# Initialize webview windows
loadWindows()

# Start an the HTTP server to serve the images from the "tables" directory
themes_dir = str(config_dir / "themes")
os.makedirs(themes_dir, exist_ok=True)
nicegui_app.add_static_files('/themes', themes_dir)

MOUNT_POINTS = {
        '/tables/': os.path.abspath(iniconfig.config['Settings']['tablerootdir']),
        '/web/': os.path.join(base_path, 'web'),
        '/themes/': themes_dir,
        }
http_server = CustomHTTPServer(MOUNT_POINTS)
theme_assets_port = int(iniconfig.config['Network'].get('themeassetsport', '8000'))
http_server.start_file_server(port=theme_assets_port)

# Start the NiceGUI HTTP server
manager_ui_port = int(iniconfig.config['Network'].get('manageruiport', '8001'))
start_manager_ui(port=manager_ui_port)

# Notification function using webview windows
def trigger_notification(title, message):
    """Send notification to all webview windows."""
    print(f"[NOTIFICATION] === TRIGGER START ===")
    print(f"[NOTIFICATION] Title: {title}")
    print(f"[NOTIFICATION] Message: {message}")
    print(f"[NOTIFICATION] Number of windows: {len(webview_windows)}")

    # Escape message for JavaScript string (handle newlines, quotes)
    safe_title = title.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')
    safe_message = str(message).replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')

    if notification_window:
        try:
            print("[NOTIFICATION] Sending to overlay window")
            notification_window.evaluate_js(f'showNotification("{safe_title}", "{safe_message}")')
            return
        except Exception as e:
            print(f"[NOTIFICATION] Error sending to overlay, falling back: {e}")

    # Fallback to old behavior if overlay fails
    # Send notification event to all windows
    for window_name, window, api in webview_windows:
        try:
            print(f"[NOTIFICATION] Sending to window: {window_name}")
            # Call the toast function directly on vpin object
            js_code = f"""
            (function() {{
                console.log('[NOTIFICATION JS] Starting notification: {safe_title}');
                try {{
                    // Try to create toast directly without going through receiveEvent
                    var toastContainer = document.getElementById('vpinfe-toast-container');
                    if (!toastContainer) {{
                        toastContainer = document.createElement('div');
                        toastContainer.id = 'vpinfe-toast-container';
                        // Max z-index and pointer-events: none for container to let clicks through
                        toastContainer.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 2147483647 !important; display: flex; flex-direction: column; gap: 10px; pointer-events: none;';
                        document.body.appendChild(toastContainer);
                        console.log('[NOTIFICATION JS] Created toast container');
                    }} else {{
                        // Ensure z-index is correct even if it existed
                        toastContainer.style.setProperty('z-index', '2147483647', 'important');
                        console.log('[NOTIFICATION JS] Found existing container');
                    }}

                    var toast = document.createElement('div');
                    // pointer-events: auto for the toast itself so it can be interacted with if needed (though it fades)
                    toast.style.cssText = 'background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); border: 2px solid #00d4ff; border-radius: 12px; padding: 16px 20px; min-width: 280px; max-width: 400px; box-shadow: 0 8px 32px rgba(0, 212, 255, 0.3), 0 0 20px rgba(0, 212, 255, 0.1); font-family: Segoe UI, Tahoma, Geneva, Verdana, sans-serif; color: white; opacity: 0; transform: translateX(100%); transition: all 0.4s cubic-bezier(0.68, -0.55, 0.265, 1.55); pointer-events: auto; z-index: 2147483647;';

                    var titleEl = document.createElement('div');
                    titleEl.style.cssText = 'font-size: 16px; font-weight: bold; color: #00d4ff; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px;';
                    titleEl.textContent = '{safe_title}';

                    var messageEl = document.createElement('div');
                    messageEl.style.cssText = 'font-size: 14px; color: #e0e0e0; line-height: 1.4; white-space: pre-line;';
                    messageEl.textContent = '{safe_message}';

                    toast.appendChild(titleEl);
                    toast.appendChild(messageEl);
                    toastContainer.appendChild(toast);

                    console.log('[NOTIFICATION JS] Toast element created and added');

                    // Force reflow
                    void toast.offsetWidth;

                    requestAnimationFrame(function() {{
                        toast.style.opacity = '1';
                        toast.style.transform = 'translateX(0)';
                        console.log('[NOTIFICATION JS] Toast animated in');
                    }});

                    setTimeout(function() {{
                        toast.style.opacity = '0';
                        toast.style.transform = 'translateX(100%)';
                        setTimeout(function() {{
                            toast.remove();
                            // If container is empty, maybe remove it? Or just leave it.
                            if (toastContainer.childNodes.length === 0) {{
                                // toastContainer.remove(); 
                            }}
                        }}, 400);
                    }}, 5000);

                    return 'Toast created successfully: ' + document.location.href;
                }} catch (e) {{
                    console.error('[NOTIFICATION JS] Error:', e);
                    return 'Error: ' + e.toString();
                }}
            }})();
            """
            result = window.evaluate_js(js_code)
            print(f"[NOTIFICATION] Sent to {window_name}, result: {result}")
        except Exception as e:
            print(f"[NOTIFICATION] Error sending to {window_name}: {e}")
            import traceback
            traceback.print_exc()

    print(f"[NOTIFICATION] === TRIGGER END ===")

# Start Score Tracker (WebSocket + Hotkey listener) with notification callback
score_tracker = ScoreTracker(iniconfig, on_notification=trigger_notification)
score_tracker.start()

# Make score_tracker available globally for remote.py
import managerui.pages.remote as remote_module
remote_module.score_tracker = score_tracker

# block and start webview
if sys.platform == "darwin":
    webview.start(gui="cocoa")
else:
    webview.start()

# shutdown items
score_tracker.stop()
http_server.on_closed()
nicegui_app.shutdown()
stop_manager_ui()

# Check for restart sentinel
restart_flag = config_dir / '.restart'
if restart_flag.exists():
    restart_flag.unlink()
    print("[VPinFE] Restart requested, re-launching...")
    python_exe = sys.executable
    main_script = os.path.abspath(__file__)
    os.execvp(python_exe, [python_exe, main_script])
