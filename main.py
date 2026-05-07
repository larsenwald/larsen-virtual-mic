import webview
import sounddevice as sd
import numpy as np
import threading
import json

class AudioEngine:
    def __init__(self):
        self.stream = None
        self.lock = threading.Lock()
        self.fx_chain = []       # [{type, params}]
        self.pre_rms  = 0.0      # RMS before FX
        self.post_rms = 0.0      # RMS after FX

    # ── devices ──────────────────────────────────────────────────────────
    def query_devices(self):
        devices = sd.query_devices()
        inputs, outputs = [], []
        for i, d in enumerate(devices):
            entry = {'id': i, 'name': d['name']}
            if d['max_input_channels'] > 0:
                inputs.append(entry)
            if d['max_output_channels'] > 0:
                outputs.append(entry)
        return {'inputs': inputs, 'outputs': outputs}

    # ── stream ────────────────────────────────────────────────────────────
    def start(self, input_id, output_id):
        self.stop()
        RATE, CHUNK = 44100, 512

        def callback(indata, outdata, frames, time_info, status):
            mono = indata[:, 0].astype(np.float32)

            # pre-FX level
            self.pre_rms = float(np.sqrt(np.mean(mono ** 2)))

            # process chain
            out = mono.copy()
            with self.lock:
                chain = list(self.fx_chain)
            for fx in chain:
                t = fx.get('type')
                p = fx.get('params', {})
                if t == 'gain':
                    db = float(p.get('db', 0.0))
                    out *= 10 ** (db / 20.0)

            out = np.clip(out, -1.0, 1.0)
            self.post_rms = float(np.sqrt(np.mean(out ** 2)))

            # write to all output channels
            for ch in range(outdata.shape[1]):
                outdata[:, ch] = out

        try:
            self.stream = sd.Stream(
                device=(int(input_id), int(output_id)),
                samplerate=RATE,
                blocksize=CHUNK,
                channels=(1, 2),
                dtype='float32',
                callback=callback,
                latency='low',
            )
            self.stream.start()
            return True
        except Exception as exc:
            self.stream = None
            return str(exc)

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.pre_rms = 0.0
        self.post_rms = 0.0

    # ── fx chain ──────────────────────────────────────────────────────────
    def set_fx_chain(self, chain):
        with self.lock:
            self.fx_chain = chain

    # ── metering ──────────────────────────────────────────────────────────
    def get_peak(self):
        return {'pre': self.pre_rms, 'post': self.post_rms}


# ── singleton ─────────────────────────────────────────────────────────────
engine = AudioEngine()


# ── pywebview JS API ──────────────────────────────────────────────────────
class API:
    def get_devices(self):
        return json.dumps(engine.query_devices())

    def start_stream(self, input_id, output_id):
        result = engine.start(input_id, output_id)
        if result is True:
            return json.dumps({'ok': True})
        return json.dumps({'ok': False, 'error': result})

    def stop_stream(self):
        engine.stop()
        return json.dumps({'ok': True})

    def set_fx_chain(self, chain_json):
        chain = json.loads(chain_json)
        engine.set_fx_chain(chain)
        return json.dumps({'ok': True})

    def get_peak(self):
        return json.dumps(engine.get_peak())


if __name__ == '__main__':
    api_instance = API()
    window = webview.create_window(
        'LarsenVirtualMic',
        'index.html',
        js_api=api_instance,
        width=1024,
        height=700,
        resizable=True,
        min_size=(800, 560),
        background_color='#0a0a0b',
    )
    webview.start(debug=False)