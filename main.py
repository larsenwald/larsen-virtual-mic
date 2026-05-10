import webview
import sounddevice as sd
import numpy as np
import threading
import json
import subprocess
import sys
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    MEIPASS_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    MEIPASS_DIR = BASE_DIR
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DRIVERS_DIR = os.path.join(BASE_DIR, "driver")
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "LarsenwaldVM")
CONFIG_PATH = os.path.join(APPDATA_DIR, "config.json")

# ─────────────────────────────────────────────────────────────
#  Config persistence
# ─────────────────────────────────────────────────────────────

def save_config():
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        config = {
            "mic": engine.selected_mic_name,
            "plugins": [p.to_dict() for p in engine.sorted_plugins()],
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"[CONFIG] saved to {CONFIG_PATH}")
    except Exception as e:
        print(f"[CONFIG] save error: {e}")

def load_config() -> dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            print(f"[CONFIG] loaded from {CONFIG_PATH}")
            return config
    except Exception as e:
        print(f"[CONFIG] load error: {e}")
    return {}

# ─────────────────────────────────────────────────────────────
#  Plugin system
# ─────────────────────────────────────────────────────────────

@dataclass
class Plugin:
    id: str
    type: str        # "gain"
    x: float         # canvas x — determines signal chain order
    y: float
    params: dict = field(default_factory=dict)

    def process(self, audio: np.ndarray) -> np.ndarray:
        if self.type == "gain":
            db = float(self.params.get("gain_db", 0.0))
            db = max(-40.0, min(40.0, db))
            linear = 10 ** (db / 20.0)
            return np.clip(audio * linear, -1.0, 1.0)
        return audio

    def to_dict(self):
        return asdict(self)


# ─────────────────────────────────────────────────────────────
#  Audio engine
# ─────────────────────────────────────────────────────────────

class AudioEngine:
    def __init__(self):
        self.plugins: list[Plugin] = []
        self.input_device: Optional[int] = None
        self.output_device: Optional[int] = None
        self.stream = None
        self.lock = threading.Lock()
        self.running = False
        self.samplerate = 48000
        self.blocksize = 512
        self.selected_mic_name: Optional[str] = None

    def sorted_plugins(self) -> list[Plugin]:
        return sorted(self.plugins, key=lambda p: p.x)

    def audio_callback(self, indata, outdata, frames, time_info, status):
        with self.lock:
            audio = indata[:, 0].copy().astype(np.float32)
            for plugin in self.sorted_plugins():
                audio = plugin.process(audio)
            outdata[:, 0] = audio
            if outdata.shape[1] > 1:
                outdata[:, 1] = audio

    def start(self) -> bool:
        self.stop()
        if self.input_device is None or self.output_device is None:
            return False
        try:
            in_info   = sd.query_devices(self.input_device)
            out_info  = sd.query_devices(self.output_device)
            in_rate   = int(in_info.get("default_samplerate",  44100))
            out_rate  = int(out_info.get("default_samplerate", 48000))
            in_ch     = min(1, int(in_info.get("max_input_channels",  1)))
            out_ch    = min(2, int(out_info.get("max_output_channels", 2)))

            print(f"[AudioEngine] input  device: idx={self.input_device} name={in_info['name']!r} rate={in_rate} ch={in_ch} hostapi={in_info['hostapi']}")
            print(f"[AudioEngine] output device: idx={self.output_device} name={out_info['name']!r} rate={out_rate} ch={out_ch} hostapi={out_info['hostapi']}")

            # Shared buffer between the two streams
            self._buf = np.zeros(self.blocksize * 4, dtype=np.float32)
            self._buf_lock = threading.Lock()

            def input_callback(indata, frames, time_info, status):
                audio = indata[:, 0].copy()
                with self.lock:
                    for plugin in self.sorted_plugins():
                        audio = plugin.process(audio)
                with self._buf_lock:
                    n = min(len(audio), len(self._buf))
                    self._buf = np.roll(self._buf, -n)
                    self._buf[-n:] = audio[:n]

            def output_callback(outdata, frames, time_info, status):
                with self._buf_lock:
                    out = self._buf[-frames:].copy()
                outdata[:, 0] = out
                if outdata.shape[1] > 1:
                    outdata[:, 1] = out

            self._in_stream = sd.InputStream(
                device=self.input_device,
                samplerate=in_rate,
                blocksize=self.blocksize,
                channels=in_ch,
                dtype=np.float32,
                callback=input_callback,
                latency="low",
            )
            self._out_stream = sd.OutputStream(
                device=self.output_device,
                samplerate=out_rate,
                blocksize=self.blocksize,
                channels=out_ch,
                dtype=np.float32,
                callback=output_callback,
                latency="low",
            )
            self._in_stream.start()
            self._out_stream.start()
            self.running = True
            print(f"[AudioEngine] started — in@{in_rate}Hz out@{out_rate}Hz")
            return True
        except Exception as e:
            print(f"[AudioEngine] start error: {e}")
            self.running = False
            return False

    def stop(self):
        for attr in ("_in_stream", "_out_stream", "stream"):
            s = getattr(self, attr, None)
            if s:
                try:
                    s.stop()
                    s.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.stream = None
        self.running = False

    def find_larsen_output(self) -> Optional[int]:
        """Find VB-Audio output via live Core Audio enumeration, then match to sd index."""
        try:
            import warnings
            from pycaw.pycaw import AudioUtilities
            from pycaw.constants import DEVICE_STATE, EDataFlow
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                render_devs = AudioUtilities.GetAllDevices(
                    data_flow=EDataFlow.eRender.value,
                    device_state=DEVICE_STATE.ACTIVE.value
                )
            print(f"[FIND_OUTPUT] live render devices: {[d.FriendlyName for d in render_devs]}")
            for d in render_devs:
                name = d.FriendlyName.lower()
                if "larsenwald vm" in name or "cable input" in name:
                    idx = self._match_sd_index(d.FriendlyName, is_output=True)
                    print(f"[FIND_OUTPUT] matched '{d.FriendlyName}' → sd_index={idx}")
                    return idx
        except Exception as e:
            print(f"[FIND_OUTPUT] pycaw error, falling back: {e}")
        # Fallback to plain sounddevice
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next(
            (i for i, h in enumerate(hostapis) if "wasapi" in h["name"].lower()), None
        )
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0 and d["hostapi"] == wasapi_index:
                name = d["name"].lower()
                if "larsenwald vm" in name or "cable input" in name:
                    return i
        return None

    def _match_sd_index(self, friendly_name: str, is_output: bool, retry: bool = True) -> Optional[int]:
        """Match a Core Audio friendly name to a sounddevice index."""
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next(
            (i for i, h in enumerate(hostapis) if "wasapi" in h["name"].lower()), None
        )
        ch_key = "max_output_channels" if is_output else "max_input_channels"
        fn_lower = friendly_name.lower()
        for i, d in enumerate(devices):
            if d[ch_key] > 0 and d["hostapi"] == wasapi_index:
                sd_name = d["name"].lower()
                if sd_name in fn_lower or fn_lower in sd_name:
                    return i
        if retry:
            print(f"[MATCH] '{friendly_name}' not found, reloading PortAudio DLL...")
            try:
                sd._terminate()
                sd._ffi.dlclose(sd._lib)
                sd._lib = sd._ffi.dlopen(sd._libname)
                sd._initialize()
            except Exception as e:
                print(f"[MATCH] DLL reload error: {e}")
            return self._match_sd_index(friendly_name, is_output, retry=False)
        return None

    def get_mic_list(self) -> list[dict]:
        """List capture devices via live Core Audio enumeration."""
        try:
            import warnings
            from pycaw.pycaw import AudioUtilities
            from pycaw.constants import DEVICE_STATE, EDataFlow
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                capture_devs = AudioUtilities.GetAllDevices(
                    data_flow=EDataFlow.eCapture.value,
                    device_state=DEVICE_STATE.ACTIVE.value
                )
            print(f"[MIC_LIST] live capture devices: {[d.FriendlyName for d in capture_devs]}")
            mics = []
            for d in capture_devs:
                name = d.FriendlyName.lower()
                skip = any(k in name for k in ["vb-audio", "cable output", "larsenwald", "virtual mic"])
                if not skip:
                    idx = self._match_sd_index(d.FriendlyName, is_output=False)
                    mics.append({"index": idx, "name": d.FriendlyName})
            return mics
        except Exception as e:
            print(f"[MIC_LIST] pycaw error, falling back: {e}")
        # Fallback
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next(
            (i for i, h in enumerate(hostapis) if "wasapi" in h["name"].lower()), None
        )
        mics = []
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and d["hostapi"] == wasapi_index:
                name = d["name"].lower()
                skip = any(k in name for k in ["vb-audio", "cable output", "larsenwald", "virtual mic"])
                if not skip:
                    mics.append({"index": i, "name": d["name"]})
        return mics


# ─────────────────────────────────────────────────────────────
#  Setup / health checks
# ─────────────────────────────────────────────────────────────

def run_powershell(script_path: str, args: list[str] = []) -> tuple[bool, str]:
    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script_path] + args
    print(f"[PS] running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, creationflags=0x08000000)
    output = result.stdout + result.stderr
    print(f"[PS] exit code: {result.returncode}")
    print(f"[PS] output:\n{output.strip()}")
    return result.returncode == 0, output

def run_configure_as_system() -> tuple[bool, str]:
    """Run configure_audio.ps1 as SYSTEM using the scheduled task trick."""
    script_path = os.path.join(SCRIPTS_DIR, "configure_audio.ps1")
    tmp = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "larsen_configure.ps1")
    print(f"[CONFIGURE] script path: {script_path}")
    print(f"[CONFIGURE] wrapper tmp: {tmp}")

    wrapper = f"""
$a = New-ScheduledTaskAction -Execute "powershell" -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"{script_path}`""
Register-ScheduledTask -TaskName "LarsenConfigure" -Action $a -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest) -Force | Out-Null
Start-ScheduledTask -TaskName "LarsenConfigure"

# Poll until task finishes instead of fixed sleep
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {{
    $state = (Get-ScheduledTask -TaskName "LarsenConfigure").State
    if ($state -eq "Ready") {{ break }}
    Start-Sleep -Milliseconds 500
}}

Unregister-ScheduledTask -TaskName "LarsenConfigure" -Confirm:$false
Write-Output "WRAPPER_DONE"
"""
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(wrapper)

    print(f"[CONFIGURE] starting wrapper...")
    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", tmp],
        capture_output=True, text=True, creationflags=0x08000000
    )
    try:
        os.remove(tmp)
    except Exception:
        pass

    output = result.stdout + result.stderr
    print(f"[CONFIGURE] wrapper exit code: {result.returncode}")
    print(f"[CONFIGURE] wrapper output:\n{output.strip()}")
    success = "WRAPPER_DONE" in output
    print(f"[CONFIGURE] success: {success}")
    return success, output

def check_vbaudio_state() -> dict:
    """
    Returns:
      {
        "installed": bool,   # VB-Audio driver present at all
        "names_ours": bool,  # 'before' field contains 'Larsenwald' (we own it)
        "names_correct": bool # both before+inside match exactly what we set
      }

    Decision tree:
      not installed                          → fresh install consent
      installed + not names_ours             → hijack warning → reinstall + configure
      installed + names_ours + not correct   → silent reconfigure (names reverted after reboot)
      installed + names_ours + correct       → pass through
    """
    print("[CHECK] scanning registry for VB-Audio state...")

    EXPECTED = {
        "render":  {"before": "Larsenwald VM", "inside": "Don't Touch"},
        "capture": {"before": "Larsenwald",    "inside": "Virtual Mic"},
    }

    try:
        import winreg
        result = {"installed": False, "names_ours": False, "names_correct": False}

        def scan_hive(hive_path):
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path)
            count = winreg.QueryInfoKey(key)[0]
            found_vbaudio = False
            found_ours    = False
            found_correct = False
            hive_type = "render" if "Render" in hive_path else "capture"
            exp = EXPECTED[hive_type]

            for i in range(count):
                sub_name = winreg.EnumKey(key, i)
                try:
                    props_key = winreg.OpenKey(key, f"{sub_name}\\Properties")
                    before, inside = "", ""
                    try:
                        before, _ = winreg.QueryValueEx(props_key, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                    except Exception:
                        pass
                    try:
                        inside, _ = winreg.QueryValueEx(props_key, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                    except Exception:
                        pass
                    winreg.CloseKey(props_key)

                    print(f"[CHECK]   device: before='{before}' inside='{inside}'")

                    if "vb-audio" in inside.lower() or "cable" in before.lower() or "cable" in inside.lower() or "larsenwald" in before.lower():
                        found_vbaudio = True
                    if "larsenwald" in before.lower():
                        found_ours = True
                        # Check if both fields match exactly
                        if before == exp["before"] and inside == exp["inside"]:
                            found_correct = True
                except Exception as e:
                    print(f"[CHECK]   error reading device: {e}")
            winreg.CloseKey(key)
            return found_vbaudio, found_ours, found_correct

        render_vb,  render_ours,  render_correct  = scan_hive(r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render")
        capture_vb, capture_ours, capture_correct = scan_hive(r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture")

        result["installed"]     = render_vb or capture_vb
        result["names_ours"]    = render_ours and capture_ours
        result["names_correct"] = render_correct and capture_correct
        print(f"[CHECK] result: {result}")
        return result

    except Exception as e:
        print(f"[CHECK] error: {e}")
        return {"installed": False, "names_ours": False, "names_correct": False}


# ─────────────────────────────────────────────────────────────
#  JS API (exposed to frontend via webview)
# ─────────────────────────────────────────────────────────────

engine = AudioEngine()

class API:
    # ── Setup flow ──────────────────────────────────────────

    def quit_app(self):
        engine.stop()
        if tray_icon:
            tray_icon.stop()
        window.destroy()
        os._exit(0)

    def get_start_minimized(self):
        return START_MINIMIZED

    def get_config(self):
        return load_config()

    def get_vbaudio_state(self):
        """Called on launch. Returns install state so frontend can decide which screen to show."""
        return check_vbaudio_state()

    def run_reconfigure(self):
        """Silently reconfigure devices (names reverted after reboot). No reinstall."""
        print("[RECONFIG] running configure as SYSTEM...")
        ok, out = run_configure_as_system()
        if not ok:
            return {"success": False, "error": f"Reconfigure failed: {out}"}
        print("[RECONFIG] done")
        return {"success": True}

    def run_setup(self):
        """Install VB-Audio + configure devices. Called after user consent."""
        # Save current default playback device before we touch anything
        default_device_id = None
        try:
            from pycaw.pycaw import AudioUtilities
            default_device_id = AudioUtilities.GetSpeakers().id
            print(f"[SETUP] saved default playback device: {default_device_id}")
        except Exception as e:
            print(f"[SETUP] could not save default device: {e}")

        print("[SETUP] starting install...")
        install_script = os.path.join(SCRIPTS_DIR, "install_vbaudio.ps1")
        ok, out = run_powershell(install_script, ["-DriversPath", DRIVERS_DIR])
        if not ok:
            print("[SETUP] install FAILED")
            return {"success": False, "error": f"Install failed: {out}"}

        print("[SETUP] install done, sleeping 3s...")
        time.sleep(3)

        print("[SETUP] running configure as SYSTEM...")
        ok, out = run_configure_as_system()
        if not ok:
            print("[SETUP] configure FAILED")
            return {"success": False, "error": f"Configure failed: {out}"}

        print("[SETUP] configure done, sleeping 3s...")
        time.sleep(3)

        # Restore default playback device
        if default_device_id:
            try:
                from pycaw.pycaw import AudioUtilities
                AudioUtilities.SetDefaultDevice(default_device_id)
                print(f"[SETUP] restored default playback device: {default_device_id}")
            except Exception as e:
                print(f"[SETUP] could not restore default device: {e}")

        print("[SETUP] setup complete")
        return {"success": True}

    def wait_for_devices(self):
        """Poll registry until our renamed devices appear. Times out after 40s."""
        print("[WAIT] waiting for devices to appear in registry...")
        time.sleep(2)
        deadline = time.time() + 40
        while time.time() < deadline:
            state = check_vbaudio_state()
            print(f"[WAIT] state: {state}")
            if state["installed"] and state["names_correct"]:
                print("[WAIT] devices ready")
                return {"ready": True}
            time.sleep(1)
        print("[WAIT] timed out")
        return {"ready": False}

    # ── Mic selection & engine control ──────────────────────

    def get_mics(self):
        return engine.get_mic_list()

    def set_mic(self, device_name: str):
        engine.stop()
        # Resolve output first — this may trigger a DLL reload on fresh install
        engine.output_device = engine.find_larsen_output()
        if engine.output_device is None:
            return {"success": False, "error": "VB-Audio output not found"}
        # Re-fetch mic list after potential DLL reload (indices may have shifted)
        mics = engine.get_mic_list()
        matched = next((m for m in mics if m["name"] == device_name), None)
        if matched is None:
            print(f"[SET_MIC] '{device_name}' not found in mic list: {[m['name'] for m in mics]}")
            return {"success": False, "error": f"Mic '{device_name}' not found"}
        engine.input_device = matched["index"]
        engine.selected_mic_name = matched["name"]
        print(f"[SET_MIC] using input={engine.input_device} ({matched['name']}) output={engine.output_device}")
        ok = engine.start()
        if ok:
            save_config()
        return {"success": ok, "running": engine.running}

    def get_engine_status(self):
        return {
            "running": engine.running,
            "input_device": engine.input_device,
            "output_device": engine.output_device,
        }

    # ── Plugin management ────────────────────────────────────

    def get_plugins(self):
        return [p.to_dict() for p in engine.sorted_plugins()]

    def add_plugin(self, plugin_type: str, x: float, y: float):
        defaults = { "gain": {"gain_db": 0.0} }
        p = Plugin(
            id=str(uuid.uuid4()),
            type=plugin_type,
            x=float(x), y=float(y),
            params=defaults.get(plugin_type, {}),
        )
        with engine.lock:
            engine.plugins.append(p)
        save_config()
        return p.to_dict()

    def add_plugin_restore(self, plugin_type: str, x: float, y: float, plugin_id: str, params: dict):
        """Restore a plugin from saved config — preserves original ID and params."""
        p = Plugin(id=plugin_id, type=plugin_type, x=float(x), y=float(y), params=params)
        with engine.lock:
            engine.plugins.append(p)
        return p.to_dict()

    def update_plugin_position(self, plugin_id: str, x: float, y: float):
        with engine.lock:
            for p in engine.plugins:
                if p.id == plugin_id:
                    p.x = float(x)
                    p.y = float(y)
                    save_config()
                    return {"success": True}
        return {"success": False}

    def update_plugin_param(self, plugin_id: str, param: str, value):
        with engine.lock:
            for p in engine.plugins:
                if p.id == plugin_id:
                    p.params[param] = value
                    save_config()
                    return {"success": True}
        return {"success": False}

    def remove_plugin(self, plugin_id: str):
        with engine.lock:
            before = len(engine.plugins)
            engine.plugins = [p for p in engine.plugins if p.id != plugin_id]
        save_config()
        return {"success": len(engine.plugins) < before}

    def get_plugin_chain_order(self):
        """Returns plugin IDs in signal chain order (left to right by x)."""
        return [p.id for p in engine.sorted_plugins()]


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────

def create_tray_icon():
    """Create a simple tray icon image — a filled circle in accent color."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Accent color #7c6af7
    draw.ellipse([4, 4, size - 4, size - 4], fill=(124, 106, 247, 255))
    return img

if __name__ == "__main__":
    import pystray
    from PIL import Image

    START_MINIMIZED = '--minimized' in sys.argv

    api = API()
    index_path = os.path.join(MEIPASS_DIR, "index.html")
    window = webview.create_window(
        title="Larsenwald Virtual Mic",
        url=f"file://{index_path}",
        js_api=api,
        width=1100,
        height=720,
        min_size=(900, 600),
        resizable=True,
        background_color="#0d0d0f",
    )

    tray_icon = None

    def show_window():
        window.show()
        window.restore()

    def quit_app():
        if tray_icon:
            tray_icon.stop()
        engine.stop()
        window.destroy()
        os._exit(0)

    def on_closing():
        """Intercept window close — hide instead of closing."""
        window.hide()
        return False

    def setup_tray(icon):
        icon.visible = True

    tray_icon = pystray.Icon(
        name="LarsenwaldVM",
        icon=create_tray_icon(),
        title="Larsenwald Virtual Mic",
        menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: show_window(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: quit_app()),
        )
    )

    window.events.closing += on_closing

    def on_shown():
        """Called when window is ready. If --minimized and setup is clean, hide immediately."""
        if not START_MINIMIZED:
            return
        state = check_vbaudio_state()
        # Only hide if we don't need user interaction
        if state["installed"] and (state["names_correct"] or state["names_ours"]):
            window.hide()

    window.events.shown += on_shown

    # Run tray in background thread
    tray_thread = threading.Thread(target=tray_icon.run, args=(setup_tray,), daemon=True)
    tray_thread.start()

    webview.start(debug=False)
    engine.stop()