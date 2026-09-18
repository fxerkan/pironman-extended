#!/usr/bin/env python3
"""pironman-extend - one service that extends SunFounder Pironman 5 with the bits
the stock firmware doesn't do, exposed under a single versioned HTTP API:

  * case-fan control  : PWM/on-off speed curve, hysteresis, per-fan status + RPM,
                        and an independently-drivable fan indicator LED (GPIO5/D2)
                        with follow/on/off/warn modes.
  * temperature RGB   : colours the 4 onboard WS2812 LEDs by CPU temperature
                        (green -> blue -> yellow -> orange -> red, purple breathing
                        alarm above warn_temp), brightness ramping with temp. It
                        does this *through pironman's own HTTP API* so the stock
                        Grafana RGB panels keep working untouched.
  * metrics           : pushes per-fan metrics to the same InfluxDB Grafana reads.

Designed to grow: add a peripheral, add a couple of /api/v1/... routes, done.
Meant to be shareable with the Pironman community.

API (JSON, all under /api/v1):
  GET  /health
  GET  /status                 -> {cpu_temp, fans:[...], rgb:{...}}
  GET  /fans        GET /fans/<name>
  POST /fans/<name>            {mode?: auto|manual|off, percent?, led_mode?: follow|on|off|warn}
  GET  /rgb
  POST /rgb                    {enabled?: bool}

Reached from Grafana via the datasource proxy (same-origin, so it works over both
http://rpifx.local:3003 and https://grafana.fxerkan.com with no CORS / mixed-content):
  POST /api/datasources/proxy/uid/pironman_extend/api/v1/fans/case_fans
"""
import json, os, time, threading, http.server, socketserver, urllib.request, urllib.parse, signal, sys
import lgpio

CONFIG = os.environ.get("PIRONMAN_EXTEND_CONFIG", "/home/fxerkan/PROJECTs/pironman-extend/config.json")
STATE = os.environ.get("PIRONMAN_EXTEND_STATE", "/home/fxerkan/PROJECTs/pironman-extend/state.json")

DEFAULTS = {
    "fan_interval": 5,          # fan curve / metrics tick (s)
    "rgb_interval": 8,          # rgb temp tick (s)
    "led_blink_period": 0.4,    # fan-LED warn blink half-period (s)
    "http_port": 34010,
    "bind": "127.0.0.1",        # Grafana proxies to localhost; not exposed publicly
    "influx_url": "http://localhost:8086",
    "influx_db": "pironman5-max",
    "influx_measurement": "case_fan",
    "temp_path": "/sys/class/thermal/thermal_zone0/temp",
    "curve": [[45, 0], [55, 40], [65, 70], [75, 100]],
    "fans": [],
    # --- RGB-by-temperature ---
    "rgb_enabled": True,
    "rgb_api_base": "http://127.0.0.1:34001/api/v1.0",
    "rgb_anchors": [[40, "#00ff00"], [50, "#0000ff"], [55, "#ffff00"],
                    [60, "#ff8000"], [65, "#ff0000"]],
    "rgb_warn_temp": 65, "rgb_warn_color": "#b000ff", "rgb_warn_speed": 90,
    "rgb_temp_lo": 40, "rgb_temp_hi": 65, "rgb_bri_lo": 20, "rgb_bri_hi": 100,
    "rgb_color_step": 8, "rgb_bri_step": 5, "rgb_temp_deadband": 1.0,
}


# ---- helpers ----------------------------------------------------------------
def cpu_temp(path):
    try:
        with open(path) as f:
            return int(f.read()) / 1000.0
    except Exception:
        return 0.0


def interp(curve, t):
    pts = sorted(curve)
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 != t0 else p0
    return pts[-1][1]


def onoff_target(running, temp, on_temp, off_temp):
    if running and temp <= off_temp:
        return 0
    if not running and temp >= on_temp:
        return 100
    return 100 if running else 0


def open_chip():
    last = None
    for c in (4, 0):
        try:
            return lgpio.gpiochip_open(c)
        except Exception as e:
            last = e
    raise last


def hex_to_rgb(h):
    h = h.strip().lstrip("#")
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def lerp(a, b, t):
    return a + (b - a) * t


def color_for_temp(anchors, t):
    pts = sorted(anchors)
    if t <= pts[0][0]:
        return hex_to_rgb(pts[0][1])
    if t >= pts[-1][0]:
        return hex_to_rgb(pts[-1][1])
    for (t0, c0), (t1, c1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            f = (t - t0) / (t1 - t0) if t1 != t0 else 0
            a, b = hex_to_rgb(c0), hex_to_rgb(c1)
            return [lerp(a[i], b[i], f) for i in range(3)]
    return hex_to_rgb(pts[-1][1])


def quantize(v, step):
    return int(round(v / step) * step)


# ---- hardware PWM (4-wire fans) ---------------------------------------------
class SysfsPWM:
    def __init__(self, chip, channel, freq):
        self.base = f"/sys/class/pwm/pwmchip{chip}/pwm{channel}"
        self.exp = f"/sys/class/pwm/pwmchip{chip}/export"
        self.channel = channel
        self.period = int(1e9 / freq)
        if not os.path.exists(self.base):
            with open(self.exp, "w") as f:
                f.write(str(channel))
            time.sleep(0.1)
        self._w("period", self.period)
        self._w("duty_cycle", 0)
        self._w("enable", 1)

    def _w(self, attr, val):
        with open(f"{self.base}/{attr}", "w") as f:
            f.write(str(val))

    def set(self, pct):
        self._w("duty_cycle", int(self.period * pct / 100))

    def close(self):
        try:
            self._w("duty_cycle", 0)
            self._w("enable", 0)
        except Exception:
            pass


# ---- fan + indicator LED ----------------------------------------------------
class Fan:
    def __init__(self, h, cfg, log):
        self.h = h
        self.log = log
        self.name = cfg["name"]
        self.model = cfg.get("model", "unknown")
        self.pin = cfg.get("pwm_pin", cfg.get("power_pin"))
        self.led_pin = cfg.get("led_pin")
        # Fan indicator LED (GPIO5/D2) is a single on/off line, NOT addressable RGB,
        # so it can't show colour. It IS a separate GPIO from fan power though, so it
        # is driven independently. led_mode: follow | on | off | warn (blink on overtemp).
        self.led_mode = cfg.get("led_mode", "follow")
        self.led_warn_temp = cfg.get("led_warn_temp", 65)
        self.tach_pin = cfg.get("tach_pin")
        self.pulses_per_rev = cfg.get("pulses_per_rev", 2)
        self.min_start = cfg.get("min_start_percent", 35)
        self.mode = cfg.get("mode", "auto")
        self.manual_percent = cfg.get("manual_percent", 60)
        self.curve = cfg.get("curve")
        self.applied = None
        self._tach_count = 0
        self._rpm = None
        self._lock = threading.Lock()

        self.pwm_enabled = cfg.get("pwm", "pwm_chip" in cfg)
        self.on_temp = cfg.get("on_temp", 52)
        self.off_temp = cfg.get("off_temp", 46)
        self.hw = None
        if "pwm_chip" in cfg:
            self.pwm_freq = cfg.get("pwm_freq", 25000)
            self.hw = SysfsPWM(cfg["pwm_chip"], cfg["pwm_channel"], self.pwm_freq)
        else:
            self.pwm_freq = min(cfg.get("pwm_freq", 10000), 10000)
            lgpio.gpio_claim_output(h, self.pin, 0)
        if self.led_pin is not None:
            lgpio.gpio_claim_output(h, self.led_pin, 0)
        if self.tach_pin is not None:
            lgpio.gpio_claim_alert(h, self.tach_pin, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
            self._cb = lgpio.callback(h, self.tach_pin, lgpio.FALLING_EDGE, self._on_tach)

    def _on_tach(self, chip, gpio, level, tick):
        with self._lock:
            self._tach_count += 1

    def sample_rpm(self, dt):
        if self.tach_pin is None:
            self._rpm = None
            return
        with self._lock:
            c, self._tach_count = self._tach_count, 0
        self._rpm = int(round(c / self.pulses_per_rev / dt * 60)) if dt > 0 else 0

    def _pwm(self, pct):
        if self.hw is not None:
            self.hw.set(pct)
        elif self.pwm_enabled:
            lgpio.tx_pwm(self.h, self.pin, self.pwm_freq, pct)
        else:
            lgpio.gpio_write(self.h, self.pin, 1 if pct > 0 else 0)

    def _set_duty(self, pct):
        pct = max(0, min(100, int(round(pct))))
        if not self.pwm_enabled and self.hw is None:
            pct = 100 if pct > 0 else 0
        if self.pwm_enabled and pct > 0 and (self.applied in (None, 0)) and pct < self.min_start:
            self._pwm(100)
            time.sleep(0.4)
        self._pwm(pct)
        self.applied = pct
        # LED is driven by the daemon's led loop (led_mode / warn), not here.

    def led_state(self, temp, blink_phase):
        if self.led_pin is None:
            return None
        if self.led_mode == "on":
            return 1
        if self.led_mode == "off":
            return 0
        if self.led_mode == "warn" and temp >= self.led_warn_temp:
            return blink_phase
        return 1 if self.applied else 0

    def set_led(self, level):
        if self.led_pin is not None:
            lgpio.gpio_write(self.h, self.led_pin, 1 if level else 0)

    def control(self, temp, global_curve):
        if self.mode == "off":
            target = 0
        elif self.mode == "manual":
            target = self.manual_percent
        elif self.pwm_enabled or self.hw is not None:
            target = interp(self.curve or global_curve, temp)
        else:
            target = onoff_target(bool(self.applied), temp, self.on_temp, self.off_temp)
        self._set_duty(target)
        return self.applied

    def status(self):
        return {
            "name": self.name, "model": self.model, "mode": self.mode,
            "control_pin": self.pin, "speed_percent": self.applied,
            "manual_percent": self.manual_percent, "rpm": self._rpm,
            "running": bool(self.applied), "led_mode": self.led_mode,
        }

    def close(self):
        try:
            self._pwm(0)
            if self.hw is not None:
                self.hw.close()
            if self.led_pin is not None:
                lgpio.gpio_write(self.h, self.led_pin, 0)
        except Exception:
            pass


# ---- RGB by temperature (drives the strip via pironman's HTTP API) ----------
class RgbController:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.enabled = self._load_enabled(cfg["rgb_enabled"])
        self.zone = "idle"
        self.last = {}
        self.pushed_temp = None
        self.lock = threading.Lock()

    def _load_enabled(self, default):
        try:
            with open(STATE) as f:
                return bool(json.load(f).get("rgb_enabled", default))
        except Exception:
            return default

    def _save_enabled(self):
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            with open(STATE, "w") as f:
                json.dump({"rgb_enabled": self.enabled}, f)
        except Exception as e:
            self.log(f"[pironman-extend] state save failed: {e}")

    def set_enabled(self, val):
        with self.lock:
            if val and not self.enabled:
                self.last = {}
                self.pushed_temp = None
            self.enabled = bool(val)
            self._save_enabled()

    def _post(self, path, body):
        url = f"{self.cfg['rgb_api_base']}/{path}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()

    def _push(self, target):
        endpoints = {
            "enable": ("set-rgb-enable", "enable"), "color": ("set-rgb-color", "color"),
            "brightness": ("set-rgb-brightness", "brightness"),
            "style": ("set-rgb-style", "style"), "speed": ("set-rgb-speed", "speed"),
        }
        for key in ["enable", "style", "color", "brightness", "speed"]:
            if key in target and target[key] != self.last.get(key):
                path, field = endpoints[key]
                try:
                    self._post(path, {field: target[key]})
                    self.last[key] = target[key]
                except Exception as e:
                    self.log(f"[pironman-extend] rgb push {key} failed: {e}")
                    return False
        return True

    def compute(self, t):
        c = self.cfg
        span = c["rgb_temp_hi"] - c["rgb_temp_lo"]
        bri_f = (t - c["rgb_temp_lo"]) / span if span else 1
        bri_f = max(0.0, min(1.0, bri_f))
        brightness = quantize(lerp(c["rgb_bri_lo"], c["rgb_bri_hi"], bri_f), c["rgb_bri_step"])
        if t > c["rgb_warn_temp"]:
            self.zone = "alarm"
            return {"enable": True, "style": "breathing", "color": c["rgb_warn_color"],
                    "brightness": max(brightness, c["rgb_bri_hi"]), "speed": c["rgb_warn_speed"]}
        rgb = [quantize(x, c["rgb_color_step"]) for x in color_for_temp(c["rgb_anchors"], t)]
        self.zone = self._zone_name(t)
        return {"enable": True, "style": "solid", "color": rgb_to_hex(rgb),
                "brightness": brightness, "speed": 0}

    def _zone_name(self, t):
        if t < 50:
            return "cool"
        if t < 55:
            return "warm"
        if t < 60:
            return "hot"
        return "very_hot"

    def tick(self, temp):
        with self.lock:
            if not self.enabled:
                return
            warn = self.cfg["rgb_warn_temp"]
            crossing = self.pushed_temp is not None and (
                (temp > warn) != (self.pushed_temp > warn))
            if (self.pushed_temp is not None and not crossing
                    and abs(temp - self.pushed_temp) < self.cfg["rgb_temp_deadband"]):
                return
            if self._push(self.compute(temp)):
                self.pushed_temp = temp

    def status(self, temp):
        return {"enabled": self.enabled, "zone": self.zone, "current": self.last,
                "warn_temp": self.cfg["rgb_warn_temp"]}


# ---- daemon -----------------------------------------------------------------
class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = print
        self.h = open_chip()
        self.fans = [Fan(self.h, f, self.log) for f in cfg["fans"]]
        self.by_name = {f.name: f for f in self.fans}
        self.rgb = RgbController(cfg, self.log)
        self.temp = 0.0
        self.running = True

    def influx_write(self):
        m = self.cfg["influx_measurement"]
        esc = lambda s: s.replace("\\", "").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")
        lines = []
        for f in self.fans:
            tags = f"fan={esc(f.name)},model={esc(f.model)}"
            fields = [f"speed_percent={f.applied or 0}i", f"target_percent={f.applied or 0}i",
                      f"state={1 if f.applied else 0}i"]
            if f._rpm is not None:
                fields.append(f"rpm={f._rpm}i")
            lines.append(f"{m},{tags} {','.join(fields)}")
        if not lines:
            return
        url = f"{self.cfg['influx_url']}/write?" + urllib.parse.urlencode({"db": self.cfg["influx_db"]})
        try:
            req = urllib.request.Request(url, data="\n".join(lines).encode(), method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:
            self.log(f"[pironman-extend] influx write failed: {e}")

    def fan_loop(self):
        interval = self.cfg["fan_interval"]
        last = time.monotonic()
        while self.running:
            now = time.monotonic()
            dt, last = now - last, now
            self.temp = cpu_temp(self.cfg["temp_path"])
            for f in self.fans:
                f.sample_rpm(dt)
                f.control(self.temp, self.cfg["curve"])
            self.influx_write()
            time.sleep(interval)

    def rgb_loop(self):
        interval = self.cfg["rgb_interval"]
        while self.running:
            try:
                self.rgb.tick(self.temp or cpu_temp(self.cfg["temp_path"]))
            except Exception as e:
                self.log(f"[pironman-extend] rgb tick error: {e}")
            time.sleep(interval)

    def led_loop(self):
        period = self.cfg["led_blink_period"]
        phase = 0
        while self.running:
            phase ^= 1
            for f in self.fans:
                level = f.led_state(self.temp, phase)
                if level is not None:
                    f.set_led(level)
            time.sleep(period)

    def status(self):
        return {"cpu_temp": round(self.temp, 1),
                "fans": [f.status() for f in self.fans],
                "rgb": self.rgb.status(self.temp)}

    def close(self):
        self.running = False
        for f in self.fans:
            f.close()
        lgpio.gpiochip_close(self.h)


# ---- HTTP API ---------------------------------------------------------------
API = "/api/v1"


def make_handler(daemon):
    class H(http.server.BaseHTTPRequestHandler):
        def _cors(self):
            # Grafana's canvas button uses backend_srv, which sends credentials +
            # X-Grafana-* headers. So we must echo a SPECIFIC origin (not *), allow
            # credentials, and allow whatever headers the browser preflights.
            origin = self.headers.get("Origin", "*")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

        def _send(self, code, obj):
            body = json.dumps(obj, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def do_OPTIONS(self):
            self.send_response(200)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # Echo the requested headers (can't use "*" together with credentials).
            req_h = self.headers.get("Access-Control-Request-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Headers", req_h)
            # Chrome Private Network Access: an http page calling a LAN IP preflights
            # Access-Control-Request-Private-Network; without this it's "Failed to fetch".
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            p = self.path.rstrip("/")
            if p in (f"{API}/health", "/health"):
                return self._send(200, {"status": "ok"})
            if p == f"{API}/status":
                return self._send(200, daemon.status())
            if p == f"{API}/rgb":
                return self._send(200, daemon.rgb.status(daemon.temp))
            if p in (f"{API}/fans", f"{API}/fans".rstrip("/")):
                return self._send(200, {"cpu_temp": daemon.temp,
                                        "fans": [f.status() for f in daemon.fans]})
            if p.startswith(f"{API}/fans/"):
                name = urllib.parse.unquote(p.split(f"{API}/fans/", 1)[1])
                f = daemon.by_name.get(name)
                return self._send(200, f.status()) if f else self._send(404, {"error": "no such fan"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            p = self.path.rstrip("/")
            try:
                body = self._body()
            except Exception:
                return self._send(400, {"error": "bad json"})
            if p == f"{API}/rgb":
                if "enabled" in body:
                    daemon.rgb.set_enabled(body["enabled"])
                return self._send(200, daemon.rgb.status(daemon.temp))
            if p.startswith(f"{API}/fans/"):
                name = urllib.parse.unquote(p.split(f"{API}/fans/", 1)[1])
                f = daemon.by_name.get(name)
                if not f:
                    return self._send(404, {"error": "no such fan"})
                if "mode" in body:
                    if body["mode"] not in ("auto", "manual", "off"):
                        return self._send(400, {"error": "mode must be auto|manual|off"})
                    f.mode = body["mode"]
                if "percent" in body:
                    f.manual_percent = max(0, min(100, int(body["percent"])))
                    if f.mode == "auto":
                        f.mode = "manual"
                if "led_mode" in body:
                    if body["led_mode"] not in ("follow", "on", "off", "warn"):
                        return self._send(400, {"error": "led_mode must be follow|on|off|warn"})
                    f.led_mode = body["led_mode"]
                f.control(daemon.temp, daemon.cfg["curve"])
                return self._send(200, f.status())
            self._send(404, {"error": "not found"})
    return H


class ThreadingHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        print(f"[pironman-extend] no config at {CONFIG}, using defaults", flush=True)
    d = Daemon(cfg)
    srv = ThreadingHTTP((cfg["bind"], cfg["http_port"]), make_handler(d))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if any(f.led_pin is not None for f in d.fans):
        threading.Thread(target=d.led_loop, daemon=True).start()
    threading.Thread(target=d.rgb_loop, daemon=True).start()
    print(f"[pironman-extend] up: {len(d.fans)} fan(s), rgb_enabled={d.rgb.enabled}, "
          f"API on {cfg['bind']}:{cfg['http_port']}", flush=True)

    def stop(*_):
        d.close()
        srv.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        d.fan_loop()
    finally:
        d.close()


def selftest():
    a = DEFAULTS["rgb_anchors"]
    assert rgb_to_hex(color_for_temp(a, 30)) == "#00ff00"
    assert rgb_to_hex(color_for_temp(a, 50)) == "#0000ff"
    assert rgb_to_hex(color_for_temp(a, 55)) == "#ffff00"
    assert rgb_to_hex(color_for_temp(a, 65)) == "#ff0000"
    assert rgb_to_hex(color_for_temp(a, 80)) == "#ff0000"
    r = RgbController(DEFAULTS, print)
    assert r.compute(70)["style"] == "breathing"
    assert r.compute(42)["style"] == "solid"
    assert r.compute(40)["brightness"] <= r.compute(64)["brightness"]
    assert onoff_target(False, 55, 52, 46) == 100 and onoff_target(True, 47, 52, 46) == 100
    assert onoff_target(True, 45, 52, 46) == 0
    assert quantize(133, 8) == 136
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
