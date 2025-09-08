#!/usr/bin/env python3
"""
Bridge X-Touch ↔ OBS avec GUI Tkinter.
Nécessite : pip install mido python-rtmidi obsws-python
OBS >= 28 avec WebSocket activé (port 4455)
"""

import tkinter as tk
from tkinter import ttk
import threading, time, logging, sys
import mido
import obsws_python as obs

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
MIDI_NAME_SUBSTR = "X-TOUCH"
OBS_HOST = "192.168.1.10"
OBS_PORT = 4455
OBS_PASSWORD = "5lTGhDUiwGeKYcVx"
POLL_INTERVAL = 1.0  # Increased interval, since MIDI->OBS is now real-time
MIDI_CHANNEL = 1
# faders CC (à adapter selon ce que ta X-Touch émet)
FADER_CCS = [0, 1, 2, 3, 4, 5, 6, 7]

# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

def midi_to_linear(v): return max(0.0, min(1.0, v/127.0))
def linear_to_midi(v): return int(round(max(0.0, min(1.0, v))*127))

def find_midi_ports(substr):
    ins, outs = mido.get_input_names(), mido.get_output_names()
    # Prefer output port that contains 'X-Touch' but NOT 'MIDIOUT2'
    x_touch_ports = [n for n in outs if substr.upper() in n.upper()]
    filtered_ports = [n for n in x_touch_ports if 'MIDIOUT2' not in n.upper()]
    outport = filtered_ports[0] if filtered_ports else (x_touch_ports[0] if x_touch_ports else None)
    inport = next((n for n in ins if substr.upper() in n.upper()), None)
    logging.info(f"[PORTS] X-Touch output ports found: {x_touch_ports}")
    logging.info(f"[PORTS] Selected output port: {outport}")
    return (inport, outport)

# ------------------------------------------------------------
class ObsBridge:
    def __init__(self):
        self.req = None
    def connect(self):
        self.req = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        self.req.get_version()  # test
        logging.info("Connecté OBS WebSocket")
    def get_audio_inputs_for_current_scene(self):
        # Fetch all inputs and filter for audio sources
        all_inputs = self.req.get_input_list().inputs
        logging.info(f"[DEBUG] Full OBS input list: {all_inputs}")
        audio_kinds = [
            "wasapi_input_capture", "wasapi_output_capture", "wasapi_process_output_capture",
            "dshow_input", "ffmpeg_source", "pulse_input_capture", "pulse_output_capture"
        ]
        audio_inputs = [inp["inputName"] for inp in all_inputs if inp["inputKind"] in audio_kinds]
        logging.info(f"Audio inputs found (global): {audio_inputs}")
        return audio_inputs
    def get_input_volume(self, name):
        try:
            resp = self.req.get_input_volume(name)
            logging.info(f"[DEBUG] get_input_volume response for '{name}': {resp}")
            logging.info(f"[DEBUG] dir(resp): {dir(resp)}")
            if hasattr(resp, '__dict__'):
                logging.info(f"[DEBUG] resp.__dict__: {resp.__dict__}")
            # Use input_volume_mul if available
            if hasattr(resp, 'input_volume_mul'):
                return float(resp.input_volume_mul)
            elif hasattr(resp, 'input_volume'):
                return float(resp.input_volume)
            elif hasattr(resp, 'inputVolume'):
                return float(resp.inputVolume)
            else:
                logging.warning(f"[DEBUG] get_input_volume: No input_volume_mul, input_volume or inputVolume attribute for '{name}'")
                return None
        except Exception as e:
            logging.warning(f"[DEBUG] get_input_volume failed for '{name}': {e}")
            return None
    def set_input_volume(self, name, val):
        try: self.req.set_input_volume(name, val)
        except Exception as e: logging.warning("set volume %s: %s", name, e)

# ------------------------------------------------------------
class MidiObsThread(threading.Thread):
    def __init__(self, midi_in, midi_out, obs_bridge, log_callback):
        super().__init__(daemon=True)
        self.midi_in_name, self.midi_out_name = midi_in, midi_out
        self.obs = obs_bridge
        self.running = False
        self.log = log_callback
        self.last_vals = {}

    def send_fader_position(self, fader_idx, value):
        # value: float 0.0-1.0, send as pitchwheel on the correct channel for X-Touch fader motor
        pitch_val = int(round(-8192 + value * 16383))
        pitch_val = max(-8192, min(8191, pitch_val))
        msg = mido.Message("pitchwheel", channel=fader_idx, pitch=pitch_val)
        self.outport.send(msg)
        logging.info(f"[SEND] {msg}")
        logging.info(f"[OBS->MIDI] Send to X-Touch: pitchwheel channel={fader_idx} value={pitch_val}")

    def send_mcu_handshake(self):
        # Try sending SysEx with and without F0/F7 for compatibility
        try:
            # Standard Mackie MCU handshake SysEx: F0 00 00 66 14 10 01 F7
            sysex = [0x00, 0x00, 0x66, 0x14, 0x10, 0x01]
            self.outport.send(mido.Message('sysex', data=sysex))
            logging.info("[MCU] Sent handshake SysEx to X-Touch (no F0/F7)")
        except Exception as e:
            logging.error(f"[MCU] Error sending handshake (no F0/F7): {e}")
        try:
            # Some devices require F0/F7 included
            sysex_full = [0xF0, 0x00, 0x00, 0x66, 0x14, 0x10, 0x01, 0xF7]
            self.outport.send(mido.Message('sysex', data=sysex_full[1:-1]))
            logging.info("[MCU] Sent handshake SysEx to X-Touch (with F0/F7 removed for mido)")
        except Exception as e:
            logging.error(f"[MCU] Error sending handshake (with F0/F7): {e}")

    def try_all_output_ports(self, pitch_val, channel):
        # Try sending a pitchwheel message to all X-Touch output ports
        for port_name in mido.get_output_names():
            if 'x-touch' in port_name.lower():
                try:
                    with mido.open_output(port_name) as test_out:
                        test_out.send(mido.Message("pitchwheel", channel=channel, pitch=pitch_val))
                        logging.info(f"[TEST] Sent pitchwheel to {port_name} channel={channel} pitch={pitch_val}")
                except Exception as e:
                    logging.error(f"[TEST] Error sending to {port_name}: {e}")

    def test_fader_loop(self):
        # Test: move fader 0 up and down for 5 seconds
        import math
        for t in range(50):
            v = 0.5 + 0.5 * math.sin(t/5.0 * math.pi)  # oscillate between 0 and 1
            self.send_fader_position(0, v)
            time.sleep(0.1)
        logging.info("[TEST] Fader test loop done.")

    def run(self):
        # Log available MIDI output ports for troubleshooting
        logging.info(f"Available MIDI output ports: {mido.get_output_names()}")
        self.obs.connect()
        # auto-map selon scène
        inputs = self.obs.get_audio_inputs_for_current_scene()
        self.fader_map = {i: inputs[i] for i in range(min(len(inputs), len(FADER_CCS)))}
        self.log(f"Faders mappés : {self.fader_map}")
        try:
            self.inport = mido.open_input(self.midi_in_name)
            self.outport = mido.open_output(self.midi_out_name)
            logging.info(f"[MIDI] Opened IN: {self.midi_in_name} OUT: {self.midi_out_name}")
        except Exception as e:
            logging.error(f"[MIDI] Error opening ports: {e}")
            self.log(f"Erreur ouverture port MIDI: {e}")
            return
        self.running = True
        # Send MCU handshake
        self.send_mcu_handshake()
        time.sleep(0.2)
        # TEST: move fader 0 up and down for 5 seconds (commented out for real-time operation)
        # self.test_fader_loop()
        # Init: synchronize X-Touch faders to OBS values
        for cc, name in self.fader_map.items():
            val = self.obs.get_input_volume(name)
            logging.info(f"[DEBUG] OBS volume for fader {cc} ({name}): {val}")
            if val is not None:
                try:
                    self.send_fader_position(cc, val)
                    logging.info(f"[INIT] Send to X-Touch: fader {cc} value {val} for {name}")
                except Exception as e:
                    logging.error(f"[MIDI] Error sending fader position: {e}")
                self.last_vals[cc] = val
                time.sleep(0.05)
        # Extra: after a short delay, refresh all fader positions to ensure sync
        time.sleep(0.5)
        for cc, name in self.fader_map.items():
            val = self.obs.get_input_volume(name)
            logging.info(f"[DEBUG] OBS volume for fader {cc} ({name}): {val}")
            if val is not None:
                try:
                    self.send_fader_position(cc, val)
                    logging.info(f"[REFRESH] Send to X-Touch: fader {cc} value {val} for {name}")
                except Exception as e:
                    logging.error(f"[MIDI] Error sending fader position: {e}")
                self.last_vals[cc] = val
                time.sleep(0.05)
        self.fader_touched = {cc: False for cc in self.fader_map}
        while self.running:
            # traiter messages MIDI entrants
            for msg in self.inport.iter_pending():
                logging.info(f"[MIDI IN] {msg}")
                # Detect fader touch/release (note_on for note 104, velocity>0 = touch, velocity==0 = release)
                if msg.type == "note_on" and msg.note == 104:
                    if msg.velocity > 0:
                        self.fader_touched[0] = True
                        logging.info("[TOUCH] Fader 0 touched")
                    else:
                        self.fader_touched[0] = False
                        logging.info("[TOUCH] Fader 0 released")
                # Handle pitchwheel for faders
                if msg.type == "pitchwheel" and msg.channel in self.fader_map:
                    lin = (msg.pitch + 8192) / 16383.0
                    logging.info(f"[MIDI->OBS] Set {self.fader_map[msg.channel]} to {lin:.3f}")
                    # Send to OBS immediately for real-time update
                    self.obs.set_input_volume(self.fader_map[msg.channel], lin)
                    self.last_vals[msg.channel] = lin
                # Optionally, still handle control_change for other controls
                if msg.type == "control_change" and msg.control in self.fader_map and msg.channel == msg.control:
                    lin = midi_to_linear(msg.value)
                    logging.info(f"[MIDI->OBS] Set {self.fader_map[msg.control]} to {lin:.3f}")
                    # Send to OBS immediately for real-time update
                    self.obs.set_input_volume(self.fader_map[msg.control], lin)
                    self.last_vals[msg.control] = lin
            # polling OBS -> envoyer aux moteurs (all faders, all channels)
            for cc, name in self.fader_map.items():
                val = self.obs.get_input_volume(name)
                logging.info(f"[DEBUG] OBS volume for fader {cc} ({name}): {val}")
                if not self.fader_touched.get(cc, False):
                    if val is not None:
                        try:
                            self.send_fader_position(cc, val)
                            logging.info(f"[OBS->MIDI] Send to X-Touch: fader {cc} value {val} for {name}")
                        except Exception as e:
                            logging.error(f"[MIDI] Error sending fader position: {e}")
                        self.last_vals[cc] = val
                        time.sleep(0.01)
            time.sleep(POLL_INTERVAL)

    def stop(self):
        self.running = False
        try: self.inport.close(); self.outport.close()
        except: pass

# ------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("X-Touch ↔ OBS Bridge")
        ttk.Label(root, text="Bridge MIDI → OBS").pack()
        self.log_box = tk.Text(root, width=70, height=15)
        self.log_box.pack()
        self.btn = ttk.Button(root, text="Démarrer", command=self.toggle)
        self.btn.pack(pady=5)
        self.thread = None

    def log(self, txt):
        self.log_box.insert(tk.END, txt + "\n")
        self.log_box.see(tk.END)

    def toggle(self):
        if self.thread and self.thread.running:
            self.thread.stop()
            self.thread = None
            self.btn.config(text="Démarrer")
            self.log("Arrêt du bridge.")
        else:
            inport, outport = find_midi_ports(MIDI_NAME_SUBSTR)
            if not inport or not outport:
                self.log(f"Port MIDI '{MIDI_NAME_SUBSTR}' introuvable")
                return
            bridge = ObsBridge()
            self.thread = MidiObsThread(inport, outport, bridge, self.log)
            self.thread.start()
            self.btn.config(text="Arrêter")
            self.log(f"Bridge lancé. MIDI IN: {inport} / OUT: {outport}")

# ------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()