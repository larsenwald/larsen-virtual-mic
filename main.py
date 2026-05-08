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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DRIVERS_DIR = os.path.join(BASE_DIR, "driver")

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
            out_info = sd.query_devices(self.output_device)
            out_ch = min(2, int(out_info.get("max_output_channels", 1)))
            self.stream = sd.Stream(
                device=(self.input_device, self.output_device),
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                channels=(1, out_ch),
                dtype=np.float32,
                callback=self.audio_callback,
                latency="low",
            )
            self.stream.start()
            self.running = True
            return True
        except Exception as e:
            print(f"[AudioEngine] start error: {e}")
            self.running = False
            return False

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.running = False

    def find_larsen_output(self) -> Optional[int]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_index = next(
            (i for i, h in enumerate(hostapis) if "wasapi" in h["name"].lower()),
            None
        )
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0 and d["hostapi"] == wasapi_index:
                name = d["name"].lower()
                if "larsenwald vm" in name or "cable input" in name:
                    return i
        return None

    def get_mic_list(self) -> list[dict]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()

        # Find the WASAPI host API index
        wasapi_index = next(
            (i for i, h in enumerate(hostapis) if "wasapi" in h["name"].lower()),
            None
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
    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_path] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    return result.returncode == 0, output

def run_configure_as_system() -> tuple[bool, str]:
    """Run configure_audio.ps1 as SYSTEM using the scheduled task trick."""
    script_path = os.path.join(SCRIPTS_DIR, "configure_audio.ps1")
    tmp = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "larsen_configure.ps1")

    wrapper = f"""
$a = New-ScheduledTaskAction -Execute "powershell" -Argument "-ExecutionPolicy Bypass -File `"{script_path}`""
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

    result = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", tmp],
        capture_output=True, text=True
    )
    try:
        os.remove(tmp)
    except Exception:
        pass

    output = result.stdout + result.stderr
    return "WRAPPER_DONE" in output, output

def check_vbaudio_state() -> dict:
    """
    Returns:
      { "installed": bool, "ours": bool }
    installed = VB-Audio driver is present at all
    ours      = devices are already renamed to our names (Larsen VM / Larsen Virtual Mic)
    """
    try:
        import winreg
        result = {"installed": False, "ours": False}

        def scan_hive(hive_path):
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive_path)
            count = winreg.QueryInfoKey(key)[0]
            found_vbaudio = False
            found_ours = False
            for i in range(count):
                sub_name = winreg.EnumKey(key, i)
                try:
                    props_key = winreg.OpenKey(key, f"{sub_name}\\Properties")
                    # Read friendly name fields
                    before = ""
                    inside = ""
                    try:
                        before, _ = winreg.QueryValueEx(props_key, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                    except Exception:
                        pass
                    try:
                        inside, _ = winreg.QueryValueEx(props_key, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                    except Exception:
                        pass
                    winreg.CloseKey(props_key)

                    if "vb-audio" in inside.lower() or "cable" in before.lower() or "cable" in inside.lower() or "larsenwald" in before.lower():
                        found_vbaudio = True
                    if "larsenwald" in before.lower():
                        found_ours = True
                except Exception:
                    pass
            winreg.CloseKey(key)
            return found_vbaudio, found_ours

        render_vb, render_ours = scan_hive(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
        )
        capture_vb, capture_ours = scan_hive(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
        )

        result["installed"] = render_vb or capture_vb
        result["ours"] = render_ours and capture_ours
        return result

    except Exception as e:
        print(f"[check_vbaudio_state] error: {e}")
        return {"installed": False, "ours": False}


# ─────────────────────────────────────────────────────────────
#  JS API (exposed to frontend via webview)
# ─────────────────────────────────────────────────────────────

engine = AudioEngine()

class API:
    # ── Setup flow ──────────────────────────────────────────

    def get_vbaudio_state(self):
        """Called on launch. Returns install state so frontend can decide which screen to show."""
        return check_vbaudio_state()

    def run_setup(self):
        """Install VB-Audio + configure devices. Called after user consent."""
        # Step 1: Install
        install_script = os.path.join(SCRIPTS_DIR, "install_vbaudio.ps1")
        ok, out = run_powershell(install_script, ["-DriversPath", DRIVERS_DIR])
        if not ok:
            return {"success": False, "error": f"Install failed: {out}"}

        # Step 2: Wait a moment for devices to register
        time.sleep(3)

        # Step 3: Configure (rename + disable) as SYSTEM
        ok, out = run_configure_as_system()
        if not ok:
            return {"success": False, "error": f"Configure failed: {out}"}

        # Step 4: Wait for audio service to settle
        time.sleep(3)

        return {"success": True}

    def wait_for_devices(self):
        """Poll until our renamed devices appear in sounddevice. Times out after 40s."""
        time.sleep(3)  # give Audiosrv a moment to fully restart first
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                out = engine.find_larsen_output()
                mics = engine.get_mic_list()
                if out is not None and len(mics) > 0:
                    return {"ready": True}
            except Exception:
                pass
            time.sleep(1)
        return {"ready": False}

    # ── Mic selection & engine control ──────────────────────

    def get_mics(self):
        return engine.get_mic_list()

    def set_mic(self, device_index: int):
        engine.stop()
        engine.input_device = int(device_index)
        engine.output_device = engine.find_larsen_output()
        if engine.output_device is None:
            return {"success": False, "error": "VB-Audio output not found"}
        ok = engine.start()
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
        defaults = {
            "gain": {"gain_db": 0.0},
        }
        p = Plugin(
            id=str(uuid.uuid4()),
            type=plugin_type,
            x=float(x),
            y=float(y),
            params=defaults.get(plugin_type, {}),
        )
        with engine.lock:
            engine.plugins.append(p)
        return p.to_dict()

    def update_plugin_position(self, plugin_id: str, x: float, y: float):
        with engine.lock:
            for p in engine.plugins:
                if p.id == plugin_id:
                    p.x = float(x)
                    p.y = float(y)
                    return {"success": True}
        return {"success": False}

    def update_plugin_param(self, plugin_id: str, param: str, value):
        with engine.lock:
            for p in engine.plugins:
                if p.id == plugin_id:
                    p.params[param] = value
                    return {"success": True}
        return {"success": False}

    def remove_plugin(self, plugin_id: str):
        with engine.lock:
            before = len(engine.plugins)
            engine.plugins = [p for p in engine.plugins if p.id != plugin_id]
            return {"success": len(engine.plugins) < before}

    def get_plugin_chain_order(self):
        """Returns plugin IDs in signal chain order (left to right by x)."""
        return [p.id for p in engine.sorted_plugins()]


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    api = API()
    index_path = os.path.join(BASE_DIR, "index.html")
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
    webview.start(debug=False)
    engine.stop()