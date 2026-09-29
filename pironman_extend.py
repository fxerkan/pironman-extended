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
  * OLED status screen: a second 128x64 SSD1315 OLED on its own i2c bus rotates
                        CPU temp / load / SSD usage / SSD temp, and preempts with a
                        blinking inverted ALARM on a crit threshold or system
                        anomaly (under-voltage, throttle, RAM full, root read-only).
                        The API can also draw arbitrary text or an image on it.

Designed to grow: add a peripheral, add a couple of /api/v1/... routes, done.
Meant to be shareable with the Pironman community.

API (JSON, all under /api/v1):
  GET  /health
  GET  /status                 -> {cpu_temp, fans:[...], rgb:{...}, oled:{...}}
  GET  /fans        GET /fans/<name>
  POST /fans/<name>            {mode?: auto|manual|off, percent?, led_mode?: follow|on|off|warn}
  GET  /rgb
  POST /rgb                    {enabled?: bool}
  GET  /oled                   -> {enabled, showing, pages, override, alerts, metrics}
  POST /oled                   {enabled?: bool, contrast?: 0-255}
  POST /oled/text              {text|lines[], size?, align?: left|center, invert?, duration?}
  POST /oled/image             {image_b64, fit?: contain|stretch|center, invert?, threshold?, duration?}
  POST /oled/clear             -> drop pushed text/image, back to auto rotation

Reached from Grafana via the datasource proxy (same-origin, so it works over both
http://rpifx.local:3003 and https://grafana.fxerkan.com with no CORS / mixed-content):
  POST /api/datasources/proxy/uid/pironman_extend/api/v1/fans/case_fans
"""
import json, os, time, threading, http.server, socketserver, urllib.request, urllib.parse, signal, sys, glob, base64, io, subprocess, math, random
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
    # --- OLED status screen (Waveshare 128x64 SSD1315 on its own i2c-gpio bus) ---
    # Separate bus (default /dev/i2c-3) so it never clashes with Pironman's own
    # 0x3c OLED on i2c-1. See README for the i2c-gpio dtoverlay + wiring.
    "oled_enabled": True,
    "oled_i2c_port": 3,
    "oled_i2c_addr": 60,            # 0x3c (int or "0x3c")
    "oled_width": 128, "oled_height": 64,
    "oled_refresh": 2,             # render/scan tick (s) -> anomaly catch speed
    "oled_page_dwell": 60,         # seconds per metric before rotating (1-3 min => 60..180)
    "oled_alert_rot": 3,           # if many alarms, seconds between them
    "oled_alarm_override": True,   # alarms preempt a user-pushed text/image (safety)
    "oled_contrast": 255,
    # page tokens: cpu load ssd | disks (expands per drive) | ram (shown only when
    # >= warn) | clock | speed | rpifx | anim:<name> | anim:* (rotates the whole pool)
    "oled_pages": ["anim:fxerkan", "rpifx", "cpu", "load", "disks", "ssd", "ram",
                   "clock", "speed", "anim:*", "anim:*"],
    "oled_thresholds": {           # (warn, crit)
        "cpu": [70, 80], "load": [1.5, 3.0], "ram": [90, 95],
        "disk": [85, 95], "ssd": [60, 70],
    },
    "oled_font": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "oled_anim_dwell": 8,          # seconds an animation page runs
    "oled_anim_frame": 0.2,        # animation frame delay (s) -> ~5 fps
    "oled_banner": ["FXerkan"],   # marquee text for anim:fxerkan
    "oled_banner_dwell": 6,
    "oled_disk_min_gb": 16,        # ignore drives smaller than this
    "oled_speedtest_interval": 21600,   # re-run Ookla/HTTP download test every 6 h
    "oled_speedtest_url": "https://speed.cloudflare.com/__down?bytes=25000000",
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
        chip = self._resolve_chip(chip)
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

    @staticmethod
    def _resolve_chip(chip):
        # pwm_chip may be an int index or a stable DT address substring
        # (e.g. "1f00098000"). pwmchipN enumeration order can swap across reboots
        # when >1 RP1 PWM controller exists; the address never does. On Pi 5
        # GPIO18 = PWM0_CHAN2, controller 1f00098000. ponytail: address match if
        # non-numeric, plain index otherwise.
        if str(chip).isdigit():
            return int(chip)
        for p in glob.glob("/sys/class/pwm/pwmchip*"):
            if str(chip) in os.path.realpath(p):
                return int(p.rsplit("pwmchip", 1)[1])
        raise FileNotFoundError("no pwmchip matches %r" % (chip,))

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


# ---- OLED: metric readers + anomaly detection (pure, testable) ---------------
def load_avg1():
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except Exception:
        return None


def disk_used_pct(path="/"):
    try:
        import shutil
        du = shutil.disk_usage(path)
        return du.used / du.total * 100.0
    except Exception:
        return None


def mem_used_pct():
    try:
        info = {}
        for line in open("/proc/meminfo"):
            parts = line.split()
            info[parts[0].rstrip(":")] = int(parts[1])
        total, avail = info["MemTotal"], info["MemAvailable"]
        return (total - avail) / total * 100.0
    except Exception:
        return None


def find_nvme_temp_path():
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(os.path.join(h, "name")).read().strip() == "nvme":
                return os.path.join(h, "temp1_input")   # Composite
        except OSError:
            pass
    return None


def read_hwmon_c(path):
    try:
        return int(open(path).read().strip()) / 1000.0
    except Exception:
        return None


def read_throttled():
    """vcgencmd get_throttled bitmask (0 on failure)."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=3).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return 0


def root_readonly():
    try:
        for line in open("/proc/mounts"):
            p = line.split()
            if len(p) >= 4 and p[1] == "/":
                return p[3].split(",")[0] == "ro"
    except OSError:
        pass
    return False


def oled_severity(key, value, thresholds, nproc):
    """0=ok 1=warn 2=crit; load threshold is per-core."""
    if value is None:
        return 0
    v = value / nproc if key == "load" else value
    warn, crit = thresholds[key]
    return 2 if v >= crit else 1 if v >= warn else 0


def oled_build_alerts(m, throttled, ro, thresholds, nproc):
    """Active problems as (short_title, detail). System anomalies first, then crit thresholds."""
    a = []
    if throttled & 0x1: a.append(("DUSUK VOLTAJ", "5V besleme zayif"))
    if throttled & 0x4: a.append(("THROTTLE", "CPU kisiliyor"))
    if throttled & 0x2: a.append(("FREKANS KISILI", "arm capped"))
    if throttled & 0x8: a.append(("ISI LIMITI", "soft temp limit"))
    if ro:              a.append(("DISK SALT-OKUR", "root ro! kontrol et"))
    msg = {
        "cpu":  lambda v: ("CPU KRITIK",  f"{v:.0f} derece"),
        "ssd":  lambda v: ("SSD KRITIK",  f"{v:.0f} derece"),
        "disk": lambda v: ("DISK DOLDU",  f"%{v:.0f} kullanim"),
        "ram":  lambda v: ("RAM DOLDU",   f"%{v:.0f} kullanim"),
        "load": lambda v: ("YUK KRITIK",  f"{v:.2f} yuk"),
    }
    for k in ["cpu", "ssd", "disk", "ram", "load"]:
        if m.get(k) is not None and oled_severity(k, m[k], thresholds, nproc) == 2:
            a.append(msg[k](m[k]))
    return a


# ---- OLED controller (own i2c bus; rotation + anomaly alarm + API draw) ------
# ---- OLED helpers: disks, datetime, speedtest, icons -------------------------
def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            s = f"{n:.0f}{unit}" if (unit in ("B", "K", "M") or n >= 100) else f"{n:.1f}{unit}"
            return s.replace(".0", "")
        n /= 1024.0


TR_DAYS = ["Pazartesi", "Sali", "Carsamba", "Persembe", "Cuma", "Cumartesi", "Pazar"]


def list_disks(min_gb=16):
    """Physical disks >= min_gb, each with (name, total, used, pct). Skips zram/loop.
    Usage comes from the largest mounted partition, if any."""
    import shutil
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,TRAN,SIZE,MODEL,LABEL,MOUNTPOINT"],
                             capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out)
    except Exception:
        return []
    TYPE = {"nvme": "NVMe", "usb": "USB", "sata": "SATA", "mmc": "SD", "ata": "SATA"}
    FALLBACK = {"nvme": "SSD", "usb": "DISK", "sata": "SSD", "mmc": "KART", "ata": "DISK"}
    GENERIC = {"", "efi", "bootfs", "rootfs", "boot", "system-boot", "recovery"}
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name0 = dev.get("name", "")
        if name0.startswith(("zram", "loop")):
            continue
        size = int(dev.get("size") or 0)
        if size < min_gb * 1024 ** 3:
            continue
        mounts = []
        labels = []

        def walk(node):
            mp = node.get("mountpoint")
            if mp and not mp.startswith("[") and mp not in ("/boot", "/boot/firmware"):
                mounts.append(mp)
                labels.append((node.get("label") or "").strip())
            for ch in node.get("children") or []:
                walk(ch)
        walk(dev)
        used = total = None
        tag = ""
        for mp, lbl in zip(mounts, labels):
            try:
                du = shutil.disk_usage(mp)
                used, total = du.used, du.total
                # tag from partition label, else the mount's basename (root -> "SYS")
                if lbl.lower() not in GENERIC:
                    tag = lbl
                elif mp != "/":
                    tag = os.path.basename(mp.rstrip("/"))   # e.g. /media/sandisk -> sandisk
                break
            except OSError:
                pass
        tran = dev.get("tran") or ""
        base = TYPE.get(tran, (dev.get("model") or name0).strip()[:6])
        if not tag or tag.lower() in GENERIC:
            tag = FALLBACK.get(tran, "DISK")
        name = f"{base} {tag}".upper()[:16]
        pct = (used / total * 100.0) if (used and total) else None
        disks.append({"name": name, "size": size, "used": used,
                      "total": total or size, "pct": pct})
    return disks


def run_speedtest(url, timeout=40):
    """Download-speed in Mbps. Prefers Ookla `speedtest`, then `speedtest-cli`,
    then a plain HTTP download from `url` (works with no extra tools installed)."""
    # 1) Ookla CLI
    try:
        out = subprocess.run(["speedtest", "--format=json", "--accept-license", "--accept-gdpr"],
                             capture_output=True, text=True, timeout=timeout).stdout
        bw = json.loads(out)["download"]["bandwidth"]      # bytes/s
        return bw * 8 / 1e6
    except Exception:
        pass
    # 2) speedtest-cli
    try:
        out = subprocess.run(["speedtest-cli", "--json"],
                             capture_output=True, text=True, timeout=timeout).stdout
        return json.loads(out)["download"] / 1e6            # bits/s -> Mbps
    except Exception:
        pass
    # 3) HTTP fallback: time a bounded download (UA header — CDNs 403 the default)
    try:
        t0 = time.monotonic()
        got = 0
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pironman-extend)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if time.monotonic() - t0 > 12 or got > 60 * 1024 * 1024:
                    break
        dt = time.monotonic() - t0
        return (got * 8 / 1e6) / dt if dt > 0 else None
    except Exception:
        return None


# ---- OLED controller (own i2c bus; rotation + anomaly alarm + API draw) ------
class OledController:
    """Drives the second OLED. Rotates metric/info/anim pages (each with an icon),
    and on a crit threshold or system anomaly preempts with a blinking inverted
    ALARM screen. The HTTP API can push arbitrary text or an image (optional TTL)."""

    def __init__(self, cfg, log, temp_getter):
        self.cfg = cfg
        self.log = log
        self._temp = temp_getter
        self.nproc = os.cpu_count() or 4
        self.thresholds = cfg["oled_thresholds"]
        self.pages = cfg["oled_pages"]
        self.enabled = bool(cfg["oled_enabled"])
        self.override = None
        self.lock = threading.Lock()
        self.tick = 0
        self.idx = 0
        self.dwell = 0.0
        self.anim_frame = 0
        self._anim_cursor = 0        # rotates through the pool so anim:* varies
        self._anim_cur = None        # concrete anim name chosen for the current page
        self.showing = "off"
        self._cur_is_anim = False
        self._cleared = False
        self._last_render = 0.0
        self._fonts = {}
        self._disks = []
        self._disks_ts = 0.0
        self.speed_mbps = None
        self.speed_ts = None
        self._alive = True
        self.dev = None
        self.PIL = None
        self.nvme_path = find_nvme_temp_path()
        if not self.enabled:
            return
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import ssd1306
            from PIL import Image, ImageDraw, ImageFont, ImageOps
            self.PIL = (Image, ImageDraw, ImageFont, ImageOps)
            addr = cfg["oled_i2c_addr"]
            addr = int(str(addr), 0) if isinstance(addr, str) else int(addr)
            serial = i2c(port=cfg["oled_i2c_port"], address=addr)
            self.dev = ssd1306(serial, width=cfg["oled_width"], height=cfg["oled_height"])
            self.dev.persist = True
            self.dev.contrast(int(cfg["oled_contrast"]))
        except Exception as e:
            self.log(f"[pironman-extend] OLED init failed ({e}); disabling OLED")
            self.enabled = False
            self.dev = None

    # -- fonts --
    def _font(self, size):
        if size not in self._fonts:
            from PIL import ImageFont
            self._fonts[size] = ImageFont.truetype(self.cfg["oled_font"], size)
        return self._fonts[size]

    # -- metrics --
    def read_metrics(self):
        return {
            "cpu":  self._temp(),
            "load": load_avg1(),
            "disk": disk_used_pct("/"),
            "ram":  mem_used_pct(),
            "ssd":  read_hwmon_c(self.nvme_path) if self.nvme_path else None,
        }

    def disks(self):
        now = time.monotonic()
        if now - self._disks_ts > 60 or not self._disks:
            self._disks = list_disks(self.cfg["oled_disk_min_gb"])
            self._disks_ts = now
        return self._disks

    # -- speedtest background loop (started by the daemon) --
    def speedtest_loop(self):
        time.sleep(20)   # let the network settle after boot
        interval = self.cfg["oled_speedtest_interval"]
        while self._alive:
            v = run_speedtest(self.cfg["oled_speedtest_url"])
            if v:
                self.speed_mbps = v
                self.speed_ts = time.time()
                self.log(f"[pironman-extend] speedtest: {v:.0f} Mbps")
            for _ in range(int(max(60, interval))):
                if not self._alive:
                    return
                time.sleep(1)

    # -- icons (small monochrome, drawn top-left at (x,y), box size s) --
    def _icon(self, d, name, x=0, y=0, s=14):
        f = 1
        if name == "cpu":
            a, b, c, e = x + 3, y + 3, x + s - 3, y + s - 3
            d.rectangle((a, b, c, e), outline=f)
            for i in range(3):
                px = a + 1 + i * ((c - a) // 2)
                d.line((px, y, px, b), fill=f); d.line((px, e, px, y + s), fill=f)
                py = b + 1 + i * ((e - b) // 2)
                d.line((x, py, a, py), fill=f); d.line((c, py, x + s, py), fill=f)
        elif name == "temp":
            cx = x + s // 2
            d.rectangle((cx - 2, y + 2, cx + 2, y + s - 5), outline=f)
            d.ellipse((cx - 4, y + s - 7, cx + 4, y + s - 1), outline=f)
            d.rectangle((cx - 1, y + s // 2, cx + 1, y + s - 4), fill=f)
            d.ellipse((cx - 3, y + s - 6, cx + 3, y + s - 2), fill=f)
        elif name == "disk":
            d.ellipse((x + 2, y + 2, x + s - 2, y + 7), outline=f)
            d.line((x + 2, y + 4, x + 2, y + s - 4), fill=f)
            d.line((x + s - 2, y + 4, x + s - 2, y + s - 4), fill=f)
            d.ellipse((x + 2, y + s - 6, x + s - 2, y + s - 1), outline=f)
        elif name == "gauge":
            d.arc((x + 1, y + 3, x + s - 1, y + s + 3), 180, 360, fill=f)
            cx, cy = x + s // 2, y + s - 2
            d.line((cx, cy, x + s - 4, y + 5), fill=f)
        elif name == "ram":
            d.rectangle((x + 2, y + 4, x + s - 2, y + s - 4), outline=f)
            for i in range(x + 4, x + s - 2, 3):
                d.line((i, y + s - 4, i, y + s - 2), fill=f)
            d.rectangle((x + 5, y + 6, x + s - 5, y + 8), fill=f)
        elif name == "clock":
            d.ellipse((x + 2, y + 2, x + s - 2, y + s - 2), outline=f)
            cx, cy = x + s // 2, y + s // 2
            d.line((cx, cy, cx, y + 4), fill=f); d.line((cx, cy, x + s - 5, cy), fill=f)
        elif name == "down":
            cx = x + s // 2
            d.line((cx, y + 2, cx, y + s - 6), fill=f)
            d.line((cx, y + s - 6, cx - 4, y + s - 10), fill=f)
            d.line((cx, y + s - 6, cx + 4, y + s - 10), fill=f)
            d.line((x + 2, y + s - 3, x + s - 2, y + s - 3), fill=f)
        elif name == "star":
            cx, cy = x + s // 2, y + s // 2
            d.line((cx, y + 1, cx, y + s - 1), fill=f); d.line((x + 1, cy, x + s - 1, cy), fill=f)
            d.line((x + 2, y + 2, x + s - 2, y + s - 2), fill=f)
            d.line((x + 2, y + s - 2, x + s - 2, y + 2), fill=f)

    # -- render primitives (all return a 1-bit 128x64 image) --
    def _blank(self, invert=False):
        Image = self.PIL[0]
        return Image.new("1", (self.cfg["oled_width"], self.cfg["oled_height"]), 1 if invert else 0)

    def _draw(self, img):
        from PIL import ImageDraw
        return ImageDraw.Draw(img)

    def render_metric(self, spec):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        img = self._blank()
        d = self._draw(img)
        self._icon(d, spec.get("icon", "cpu"), 0, 0, 14)
        d.text((17, 1), spec["title"], font=self._font(11), fill=1)
        big, unit = spec["big"], spec.get("unit", "")
        big_size = spec.get("big_size", 28)
        fb = self._font(big_size)
        bw = d.textlength(big, font=fb)
        us = spec.get("unit_size", 11)
        fu = self._font(us)
        uw = d.textlength(unit, font=fu) if unit else 0
        by = spec.get("big_y", 15)
        if spec.get("big_align", "right") != "left":   # big value hugs the right margin by default
            # value + unit hugged against the right margin (disks want % on the right)
            ux = W - 3 - uw
            d.text((max(4, ux - bw), by), big, font=fb, fill=1)
            if unit:
                d.text((ux, by + (big_size - us)), unit, font=fu, fill=1)
        else:
            d.text((4, by), big, font=fb, fill=1)
            if unit:
                d.text((6 + bw, by + (big_size - us) - 3), unit, font=fu, fill=1)
        sub, bar = spec.get("sub"), spec.get("bar")
        if sub:
            d.text((2, 49), sub, font=self._font(10), fill=1)
        if bar is not None:
            w = int(min(bar, 100) / 100.0 * (W - 4))
            d.rectangle((2, H - 5, W - 3, H - 1), outline=1)
            if w > 0:
                d.rectangle((2, H - 5, 2 + w, H - 1), fill=1)
        return img

    def render_clock(self):
        import time as _t
        W = self.cfg["oled_width"]
        lt = _t.localtime()
        img = self._blank()
        d = self._draw(img)
        self._icon(d, "clock", 0, 0, 14)
        d.text((17, 1), "TARIH / SAAT", font=self._font(11), fill=1)
        hm = _t.strftime("%H:%M", lt)
        fb = self._font(30)
        w = d.textlength(hm, font=fb)
        d.text((int((W - w) // 2), 14), hm, font=fb, fill=1)
        ds = _t.strftime("%d.%m.%Y", lt)
        f2 = self._font(12)
        w2 = d.textlength(ds, font=f2)
        d.text((int((W - w2) // 2), 46), ds, font=f2, fill=1)
        day = TR_DAYS[lt.tm_wday]
        # weekday to the right of nothing -> put small under date already tight; overlay day at bottom-right
        return img

    def render_banner(self):
        # kept for back-compat; the rotation now uses anim:fxerkan + the rpifx page
        return self.render_anim("fxerkan", self.anim_frame)

    def render_rpifx(self):
        # This panel's top ~16px glow yellow, the rest blue. "Raspberry Pi 5" sits
        # in the yellow band, "RPIFX" fills the blue band in big letters.
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        img = self._blank()
        d = self._draw(img)
        top = "Raspberry Pi 5"
        ft = self._font(13)
        w = d.textlength(top, font=ft)
        d.text((int((W - w) // 2), 0), top, font=ft, fill=1)
        d.line((0, 16, W, 16), fill=1)
        big = "RPIFX"
        fb = self._font(40)
        wb = d.textlength(big, font=fb)
        d.text((int((W - wb) // 2), 20), big, font=fb, fill=1)
        return img

    # -- animation pool (name -> method); anim:* rotates through all of these ----
    ANIM_NAMES = ["fxerkan", "pacman", "faces", "cat", "dog", "runner", "walker",
                  "jumper", "dino", "fish", "bird", "rocket", "car", "snake",
                  "rain", "wave", "balls", "heart", "spinner", "dvd"]

    def render_anim(self, name, frame):
        img = self._blank()
        d = self._draw(img)
        fn = getattr(self, "_anim_" + name, None)
        (fn or self._anim_dots)(d, frame)
        return img

    def _label(self, d, txt, y=2):
        W = self.cfg["oled_width"]
        f = self._font(10)
        w = d.textlength(txt, font=f)
        d.text((int((W - w) // 2), y), txt, font=f, fill=1)

    def _anim_fxerkan(self, d, frame):
        # marquee: "FXerkan" scrolls right-to-left, wrapping continuously
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        txt = "FXerkan"
        f = self._font(34)
        tw = int(d.textlength(txt, font=f))
        span = tw + W
        x = W - (frame * 6) % span
        y = (H - 34) // 2 - 2
        d.text((x, y), txt, font=f, fill=1)
        d.text((x - span, y), txt, font=f, fill=1)   # trailing copy for seamless wrap

    def _anim_pacman(self, d, frame):
        cx, cy, r = 26, 34, 15
        if frame % 2 == 0:
            d.pieslice((cx - r, cy - r, cx + r, cy + r), 32, 328, fill=1)
        else:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=1)
        d.ellipse((cx - 3, cy - 10, cx + 1, cy - 6), fill=0)
        for i in range(6):
            x = 56 + ((i * 16 - frame * 7) % 80)
            d.ellipse((x - 2, cy - 2, x + 2, cy + 2), fill=1)
        self._label(d, "PAC-MAN")

    def _anim_faces(self, d, frame):
        cx, cy, r = 64, 34, 24
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=1)
        exp = frame % 4
        if exp == 2:  # wink
            d.ellipse((cx - 13, cy - 8, cx - 7, cy - 2), fill=1)
            d.line((cx + 7, cy - 5, cx + 13, cy - 5), fill=1)
        else:
            d.ellipse((cx - 13, cy - 8, cx - 7, cy - 2), fill=1)
            d.ellipse((cx + 7, cy - 8, cx + 13, cy - 2), fill=1)
        if exp in (0, 2):
            d.arc((cx - 12, cy - 2, cx + 12, cy + 16), 10, 170, fill=1)   # smile
        elif exp == 1:
            d.line((cx - 10, cy + 9, cx + 10, cy + 9), fill=1)            # neutral
        else:
            d.arc((cx - 12, cy + 6, cx + 12, cy + 24), 190, 350, fill=1)  # sad

    def _anim_dots(self, d, frame):
        W = self.cfg["oled_width"]
        for i in range(3):
            up = (frame % 3) == i
            x = W // 2 - 22 + i * 18
            y = 30 - (8 if up else 0)
            d.ellipse((x, y, x + 11, y + 11), fill=1)
        t = "YUKLENIYOR" + "." * (frame % 4)
        self._label(d, t, 48)

    def _anim_cat(self, d, frame):
        cx, cy = 64, 36
        d.ellipse((cx - 20, cy - 14, cx + 20, cy + 16), outline=1)      # head
        d.polygon((cx - 20, cy - 10, cx - 12, cy - 26, cx - 6, cy - 12), outline=1)  # ear L
        d.polygon((cx + 20, cy - 10, cx + 12, cy - 26, cx + 6, cy - 12), outline=1)  # ear R
        if frame % 4 == 0:
            d.line((cx - 12, cy - 2, cx - 6, cy - 2), fill=1)
            d.line((cx + 6, cy - 2, cx + 12, cy - 2), fill=1)
        else:
            d.ellipse((cx - 12, cy - 5, cx - 6, cy + 1), fill=1)
            d.ellipse((cx + 6, cy - 5, cx + 12, cy + 1), fill=1)
        d.line((cx, cy + 3, cx, cy + 7), fill=1)
        wag = 6 if frame % 2 else -6
        for k in (-1, 1):
            d.line((cx + k * 2, cy + 8, cx + k * 14, cy + 8 + wag // 2), fill=1)
        self._label(d, "KEDI")

    def _anim_dog(self, d, frame):
        # side-view dog trotting in place, tail + ear wag
        cx, cy = 60, 40
        d.ellipse((cx - 22, cy - 8, cx + 6, cy + 8), outline=1)          # body
        d.ellipse((cx + 2, cy - 18, cx + 20, cy), outline=1)            # head
        ear = -8 if frame % 2 else -4
        d.line((cx + 6, cy - 16, cx + 4, cy - 16 + ear), fill=1)        # ear
        d.line((cx + 18, cy - 6, cx + 24, cy - 6), fill=1)             # snout
        d.ellipse((cx + 12, cy - 12, cx + 15, cy - 9), fill=1)         # eye
        tail = -10 if frame % 2 else -16
        d.line((cx - 22, cy - 2, cx - 30, cy + tail // 2), fill=1)     # tail wag
        step = 3 if frame % 2 else -3
        for lx in (cx - 16, cx - 2):
            d.line((lx, cy + 8, lx + step, cy + 18), fill=1)          # legs
        self._label(d, "KOPEK")

    def _anim_runner(self, d, frame):
        # stick figure running across the screen
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        x = 10 + (frame * 8) % (W - 20)
        cy = 30
        d.ellipse((x - 5, cy - 14, x + 5, cy - 4), outline=1)          # head
        d.line((x, cy - 4, x, cy + 10), fill=1)                        # torso
        sw = 8 if frame % 2 else -8
        d.line((x, cy, x + sw, cy + 4), fill=1); d.line((x, cy, x - sw, cy + 2), fill=1)  # arms
        d.line((x, cy + 10, x + sw, cy + 22), fill=1); d.line((x, cy + 10, x - sw, cy + 22), fill=1)  # legs
        d.line((0, cy + 24, W, cy + 24), fill=1)                       # ground
        self._label(d, "KOSU")

    def _anim_walker(self, d, frame):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        x = 10 + (frame * 4) % (W - 20)
        cy = 30
        d.ellipse((x - 5, cy - 14, x + 5, cy - 4), outline=1)
        d.line((x, cy - 4, x, cy + 10), fill=1)
        sw = 5 if frame % 2 else -5
        d.line((x, cy + 2, x + sw, cy + 8), fill=1); d.line((x, cy + 2, x - sw, cy + 8), fill=1)
        d.line((x, cy + 10, x + sw, cy + 22), fill=1); d.line((x, cy + 10, x - sw, cy + 22), fill=1)
        d.line((0, cy + 24, W, cy + 24), fill=1)
        self._label(d, "YURUYUS")

    def _anim_jumper(self, d, frame):
        # a ball bouncing in a parabola across the screen
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        x = 8 + (frame * 6) % (W - 16)
        t = ((frame % 10) / 10.0) * 2 - 1
        y = 48 - int((1 - t * t) * 34)
        d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=1)
        d.ellipse((x - 2, y - 3, x, y - 1), fill=0)
        d.line((0, 54, W, 54), fill=1)
        self._label(d, "ZIPLAMA")

    def _anim_dino(self, d, frame):
        # chrome-style dino running, jumping over a cactus
        W = self.cfg["oled_width"]
        ground = 50
        jump = 0
        if frame % 8 in (2, 3, 4):
            jump = -14
        dx = 20
        dy = ground + jump
        d.rectangle((dx, dy - 16, dx + 10, dy), fill=1)               # body
        d.rectangle((dx + 8, dy - 22, dx + 16, dy - 12), fill=1)      # head
        d.rectangle((dx + 15, dy - 20, dx + 18, dy - 18), fill=1)     # snout
        leg = 3 if frame % 2 else 0
        d.rectangle((dx + 1, dy, dx + 4, dy + 4 - leg), fill=1)
        d.rectangle((dx + 6, dy, dx + 9, dy + leg), fill=1)
        cx = W - ((frame * 6) % (W + 20))                             # cactus scrolls in
        d.rectangle((cx, ground - 12, cx + 4, ground), fill=1)
        d.rectangle((cx - 3, ground - 8, cx, ground - 4), fill=1)
        d.rectangle((cx + 4, ground - 9, cx + 7, ground - 5), fill=1)
        d.line((0, ground, W, ground), fill=1)
        self._label(d, "DINO")

    def _anim_fish(self, d, frame):
        # fish swimming left->right with bubbles + tail flick
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        x = 8 + (frame * 5) % (W - 8)
        cy = 34
        d.ellipse((x - 12, cy - 8, x + 8, cy + 8), outline=1)         # body
        tl = 8 if frame % 2 else 5
        d.polygon((x - 12, cy, x - 20, cy - tl, x - 20, cy + tl), outline=1)  # tail
        d.ellipse((x + 1, cy - 3, x + 4, cy), fill=1)                 # eye
        for i in range(3):
            by = cy - 8 - ((frame * 2 + i * 6) % 26)
            d.ellipse((x + 6 + i * 3, by, x + 9 + i * 3, by + 3), outline=1)
        self._label(d, "BALIK")

    def _anim_bird(self, d, frame):
        # bird flapping across the sky
        W = self.cfg["oled_width"]
        x = 10 + (frame * 7) % (W - 20)
        cy = 26
        up = frame % 2 == 0
        wy = -10 if up else 6
        d.line((x - 12, cy + (0 if up else 4), x, cy), fill=1)
        d.line((x, cy, x - 6, cy + wy), fill=1)
        d.line((x, cy, x + 12, cy + (0 if up else 4)), fill=1)
        d.line((x, cy, x + 6, cy + wy), fill=1)
        d.ellipse((x - 2, cy - 3, x + 4, cy + 3), fill=1)             # body
        for sx, sy in ((100, 12), (30, 8), (70, 18)):
            d.point((sx, sy), fill=1)
        self._label(d, "KUS", 50)

    def _anim_rocket(self, d, frame):
        # rocket flying diagonally with a flickering flame + starfield
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        p = (frame * 6) % (W + 30)
        x = p - 15
        y = H - 12 - int(p * 0.3)
        d.polygon((x, y - 10, x + 6, y, x, y + 10, x - 6, y), outline=1)  # body diamond-ish
        d.polygon((x, y - 10, x + 4, y - 4, x - 4, y - 4), fill=1)        # nose
        fl = 10 if frame % 2 else 6
        d.polygon((x - 6, y - 3, x - 6 - fl, y, x - 6, y + 3), fill=1)    # flame
        for sx, sy in ((110, 10), (90, 40), (40, 20), (70, 52)):
            d.point((sx, sy), fill=1)
        self._label(d, "ROKET")

    def _anim_car(self, d, frame):
        # car driving across, wheels spin
        W = self.cfg["oled_width"]
        x = (frame * 7) % (W + 40) - 40
        cy = 40
        d.rectangle((x, cy - 6, x + 34, cy + 6), outline=1)          # chassis
        d.polygon((x + 6, cy - 6, x + 12, cy - 14, x + 24, cy - 14, x + 28, cy - 6), outline=1)  # cabin
        for wx in (x + 8, x + 26):
            d.ellipse((wx - 5, cy + 3, wx + 5, cy + 13), outline=1)
            a = (frame % 4) * 45
            d.line((wx, cy + 8, wx + int(4 * math.cos(math.radians(a))),
                    cy + 8 + int(4 * math.sin(math.radians(a)))), fill=1)
        d.line((0, cy + 13, W, cy + 13), fill=1)
        self._label(d, "ARABA")

    def _anim_snake(self, d, frame):
        # snake weaving across a grid
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        for i in range(10):
            x = (frame * 6 + i * 12) % (W + 24) - 12
            y = 34 + int(14 * math.sin((frame + i) * 0.5))
            r = 5 if i == 0 else 4
            d.ellipse((x - r, y - r, x + r, y + r), fill=1)
        self._label(d, "YILAN")

    def _anim_rain(self, d, frame):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        for i in range(14):
            x = (i * 37 + 7) % W
            y = (frame * 5 + i * 11) % H
            d.line((x, y, x - 2, y + 5), fill=1)
        d.arc((44, 4, 84, 30), 200, 340, fill=1)                     # cloud
        d.arc((54, 2, 90, 26), 190, 350, fill=1)
        self._label(d, "YAGMUR", 50)

    def _anim_wave(self, d, frame):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        prev = None
        for x in range(0, W, 2):
            y = 34 + int(16 * math.sin((x + frame * 6) * 0.12))
            if prev:
                d.line((prev[0], prev[1], x, y), fill=1)
            prev = (x, y)
        self._label(d, "DALGA")

    def _anim_balls(self, d, frame):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        for i in range(4):
            x = 20 + i * 28
            phase = (frame + i * 3)
            t = (phase % 12) / 12.0 * 2 - 1
            y = 46 - int((1 - t * t) * 30)
            r = 4 + i
            d.ellipse((x - r, y - r, x + r, y + r), fill=1)
        d.line((0, 52, W, 52), fill=1)
        self._label(d, "TOPLAR")

    def _anim_heart(self, d, frame):
        # beating heart (two lobes + triangle), pulses size
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        cx, cy = W // 2, 32
        s = 12 + (4 if frame % 2 else 0)
        d.ellipse((cx - s, cy - s // 2, cx, cy + s // 2), fill=1)
        d.ellipse((cx, cy - s // 2, cx + s, cy + s // 2), fill=1)
        d.polygon((cx - s, cy, cx + s, cy, cx, cy + s + 4), fill=1)
        self._label(d, "KALP", 52)

    def _anim_spinner(self, d, frame):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        cx, cy, r = W // 2, 32, 18
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=1)
        a = math.radians((frame * 30) % 360)
        d.line((cx, cy, cx + int(r * math.cos(a)), cy + int(r * math.sin(a))), fill=1)
        for k in range(12):
            aa = math.radians(k * 30)
            x0 = cx + int((r - 3) * math.cos(aa)); y0 = cy + int((r - 3) * math.sin(aa))
            x1 = cx + int(r * math.cos(aa)); y1 = cy + int(r * math.sin(aa))
            d.line((x0, y0, x1, y1), fill=1)
        self._label(d, "DONUYOR", 52)

    def _anim_dvd(self, d, frame):
        # the classic bouncing logo
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        bw, bh = 30, 14
        px = (frame * 5) % (2 * (W - bw))
        py = (frame * 3) % (2 * (H - bh))
        x = px if px <= W - bw else 2 * (W - bw) - px
        y = py if py <= H - bh else 2 * (H - bh) - py
        d.rectangle((x, y, x + bw, y + bh), outline=1)
        f = self._font(11)
        d.text((x + 5, y + 1), "FX", font=f, fill=1)

    def render_alert(self, alert, invert):
        W = self.cfg["oled_width"]
        title, detail = alert
        img = self._blank(invert=invert)
        d = self._draw(img)
        fg = 0 if invert else 1
        d.text((2, 0), "!!! PROBLEM !!!", font=self._font(12), fill=fg)
        fm = self._font(15)
        tw = d.textlength(title, font=fm)
        d.text((max(2, int((W - tw) // 2)), 22), title, font=fm, fill=fg)
        fs = self._font(11)
        dw = d.textlength(detail, font=fs)
        d.text((max(2, int((W - dw) // 2)), 46), detail, font=fs, fill=fg)
        return img

    def render_text(self, lines, size=None, align="center", invert=False):
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        img = self._blank(invert=invert)
        d = self._draw(img)
        fg = 0 if invert else 1
        n = max(1, len(lines))
        if size is None:
            size = max(8, min(40, (H // n) - 2))
        font = self._font(size)
        lh = H / n
        for i, line in enumerate(lines):
            w = d.textlength(line, font=font)
            x = 0 if align == "left" else max(0, int((W - w) // 2))
            y = int(i * lh + (lh - size) / 2)
            d.text((x, y), line, font=font, fill=fg)
        return img

    def render_image(self, raw, fit="contain", invert=False, threshold=128):
        from PIL import Image, ImageOps
        W, H = self.cfg["oled_width"], self.cfg["oled_height"]
        im = Image.open(io.BytesIO(raw)).convert("L")
        if invert:
            im = ImageOps.invert(im)
        if fit == "stretch":
            im = im.resize((W, H))
        else:
            im = im.copy()
            im.thumbnail((W, H))
        bw = im.point(lambda p: 255 if p >= threshold else 0).convert("1")
        canvas = Image.new("1", (W, H), 0)
        canvas.paste(bw, ((W - bw.width) // 2, (H - bw.height) // 2))
        return canvas

    # -- page list (dynamic: disks expand, ram is conditional) --
    def build_pages(self, m):
        out = []
        for tok in self.pages:
            if tok == "cpu":
                out.append({"type": "metric", "icon": "temp", "title": "CPU SICAKLIK",
                            "big": "--" if m["cpu"] is None else f"{m['cpu']:.0f}", "unit": "°C"})
            elif tok == "ssd":
                out.append({"type": "metric", "icon": "temp", "title": "SSD SICAKLIK",
                            "big": "--" if m["ssd"] is None else f"{m['ssd']:.0f}", "unit": "°C",
                            "big_size": 46, "big_y": 12, "unit_size": 16})
            elif tok == "load":
                v = m["load"]
                pc = None if v is None else v / self.nproc * 100.0
                out.append({"type": "metric", "icon": "gauge", "title": "SISTEM YUK",
                            "big": "--" if pc is None else f"{pc:.0f}",
                            "unit": "" if pc is None else "%", "big_size": 40, "big_y": 14,
                            "unit_size": 16,
                            "sub": None if v is None else f"{self.nproc} cekirdek",
                            "bar": None if pc is None else min(pc, 100)})
            elif tok == "rpifx":
                out.append({"type": "rpifx", "dwell": self.cfg.get("oled_banner_dwell", 6)})
            elif tok == "ram":
                v = m["ram"]
                if v is not None and v >= self.thresholds["ram"][0]:
                    out.append({"type": "metric", "icon": "ram", "title": "RAM UYARI",
                                "big": f"{v:.0f}", "unit": "%", "bar": v,
                                "sub": "bellek dolmak uzere"})
            elif tok == "disks":
                for dsk in self.disks():
                    sub = (f"{human_bytes(dsk['used'])}/{human_bytes(dsk['total'])}"
                           if dsk["pct"] is not None else f"{human_bytes(dsk['total'])} - mount yok")
                    out.append({"type": "metric", "icon": "disk", "title": dsk["name"],
                                "big": "--" if dsk["pct"] is None else f"{dsk['pct']:.0f}",
                                "unit": "" if dsk["pct"] is None else "%",
                                "big_align": "right", "big_size": 32, "big_y": 15,
                                "unit_size": 14, "sub": sub, "bar": dsk["pct"]})
            elif tok == "clock":
                out.append({"type": "clock"})
            elif tok == "banner":
                out.append({"type": "banner", "dwell": self.cfg.get("oled_banner_dwell", 6)})
            elif tok == "speed":
                if self.speed_mbps is None:
                    big, sub = "--", "olculuyor..."
                else:
                    big = f"{self.speed_mbps:.0f}"
                    age = "" if not self.speed_ts else f"{int((time.time() - self.speed_ts) / 60)} dk once"
                    sub = f"Mbps indirme  {age}"
                out.append({"type": "metric", "icon": "down", "title": "INDIRME HIZI",
                            "big": big, "unit": "" if big == "--" else "Mb", "sub": sub})
            elif tok.startswith("anim:"):
                out.append({"type": "anim", "name": tok.split(":", 1)[1],
                            "dwell": self.cfg["oled_anim_dwell"]})
        return out

    def _render_spec(self, spec):
        t = spec["type"]
        if t == "metric":
            return self.render_metric(spec)
        if t == "clock":
            return self.render_clock()
        if t == "banner":
            return self.render_banner()
        if t == "rpifx":
            return self.render_rpifx()
        if t == "anim":
            return self.render_anim(self._resolve_anim(spec["name"]), self.anim_frame)
        return self._blank()

    def _resolve_anim(self, name):
        # "*"/"random"/"any" -> rotate through the whole pool so every anim shows up
        if name not in ("*", "random", "any"):
            return name
        if self.anim_frame == 0 or self._anim_cur is None:
            self._anim_cur = self.ANIM_NAMES[self._anim_cursor % len(self.ANIM_NAMES)]
            self._anim_cursor += 1
        return self._anim_cur

    # -- API-driven overrides --
    def _set_override(self, image, duration, kind):
        exp = None if not duration else time.monotonic() + float(duration)
        with self.lock:
            self.override = {"image": image, "expires": exp, "kind": kind}

    def set_text(self, lines, size=None, align="center", invert=False, duration=0):
        if not self.dev:
            raise RuntimeError("oled disabled")
        self._set_override(self.render_text(lines, size, align, invert), duration, "text")

    def set_image(self, raw, fit="contain", invert=False, threshold=128, duration=0):
        if not self.dev:
            raise RuntimeError("oled disabled")
        self._set_override(self.render_image(raw, fit, invert, threshold), duration, "image")

    def clear_override(self):
        with self.lock:
            self.override = None

    def set_enabled(self, val):
        with self.lock:
            self.enabled = bool(val) and self.dev is not None
            self._cleared = False

    def set_contrast(self, val):
        if self.dev:
            self.dev.contrast(max(0, min(255, int(val))))

    # -- main render tick --
    def render_once(self):
        if not self.dev:
            return
        now = time.monotonic()
        dt = (now - self._last_render) if self._last_render else 0.0
        self._last_render = now
        if not self.enabled:
            if not self._cleared:
                self.dev.clear()
                self._cleared = True
                self.showing = "off"
            return
        self._cleared = False
        # self-heal, EVERY frame: re-assert the full power-on state. A loose
        # connector momentarily browns out the panel, which resets it to its
        # factory default (charge-pump OFF, display OFF) -> stays black even though
        # we keep writing GDDRAM. display-ON (0xAF) alone is NOT enough: without the
        # charge pump the glass has no drive voltage. So re-send charge-pump-on +
        # contrast + resume + normal + display-on. Cheap (7 cmd bytes); luma re-sends
        # the column/page addressing on every display() so layout stays correct.
        try:
            self.dev.command(0x8D, 0x14,                       # charge pump ON
                             0xA4, 0xA6,                        # resume RAM, non-inverted
                             0x81, int(self.cfg["oled_contrast"]) & 0xFF,
                             0xAF)                              # display ON
        except Exception:
            pass
        m = self.read_metrics()
        alerts = oled_build_alerts(m, read_throttled(), root_readonly(),
                                   self.thresholds, self.nproc)
        with self.lock:
            ov = self.override
            if ov and ov["expires"] is not None and now >= ov["expires"]:
                self.override = ov = None

        if alerts and (self.cfg["oled_alarm_override"] or not ov):
            step = max(1, int(self.cfg["oled_alert_rot"] / max(0.2, self.cfg["oled_refresh"])))
            a = alerts[(self.tick // step) % len(alerts)]
            self.dev.display(self.render_alert(a, invert=(self.tick % 2 == 0)))
            self.showing = f"alarm:{a[0]}"
            self._cur_is_anim = False
            self.dwell = 0.0
            self.tick += 1
            return
        if ov:
            self.dev.display(ov["image"])
            self.showing = f"override:{ov['kind']}"
            self._cur_is_anim = False
            self.tick += 1
            return

        pages = self.build_pages(m)
        if not pages:
            self.tick += 1
            return
        self.idx %= len(pages)
        spec = pages[self.idx]
        self._cur_is_anim = spec["type"] == "anim"
        self.dev.display(self._render_spec(spec))
        self.showing = spec.get("title") or spec["type"] + (f":{self._resolve_anim(spec.get('name',''))}" if spec["type"] == "anim" else "")
        if self._cur_is_anim:
            self.anim_frame += 1
        self.dwell += dt
        if self.dwell >= spec.get("dwell", self.cfg["oled_page_dwell"]):
            self.dwell = 0.0
            self.anim_frame = 0
            self.idx = (self.idx + 1) % len(pages)
        self.tick += 1

    def next_delay(self):
        return self.cfg["oled_anim_frame"] if self._cur_is_anim else self.cfg["oled_refresh"]

    def status(self):
        m = self.read_metrics() if self.dev else {}
        with self.lock:
            ov = self.override
            exp_in = None if not ov or ov["expires"] is None else max(0, round(ov["expires"] - time.monotonic(), 1))
        alerts = []
        if self.dev and self.enabled:
            alerts = [a[0] for a in oled_build_alerts(
                m, read_throttled(), root_readonly(), self.thresholds, self.nproc)]
        return {
            "enabled": self.enabled, "available": self.dev is not None,
            "showing": self.showing, "pages": self.pages,
            "disks": [{"name": d["name"], "size": human_bytes(d["total"]),
                       "used_pct": None if d["pct"] is None else round(d["pct"], 1)} for d in self.disks()] if self.dev else [],
            "download_mbps": None if self.speed_mbps is None else round(self.speed_mbps, 1),
            "override": None if not ov else {"kind": ov["kind"], "expires_in": exp_in},
            "alerts": alerts,
            "metrics": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in m.items()},
        }

    def close(self):
        self._alive = False


# ---- daemon -----------------------------------------------------------------
class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = print
        self.h = open_chip()
        self.fans = [Fan(self.h, f, self.log) for f in cfg["fans"]]
        self.by_name = {f.name: f for f in self.fans}
        self.rgb = RgbController(cfg, self.log)
        self.oled = OledController(cfg, self.log, lambda: self.temp or cpu_temp(cfg["temp_path"]))
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

    def oled_loop(self):
        while self.running:
            try:
                self.oled.render_once()
            except Exception as e:
                self.log(f"[pironman-extend] oled render error: {e}")
            time.sleep(self.oled.next_delay())

    def status(self):
        return {"cpu_temp": round(self.temp, 1),
                "fans": [f.status() for f in self.fans],
                "rgb": self.rgb.status(self.temp),
                "oled": self.oled.status()}

    def close(self):
        if not self.running:      # close() can fire twice (signal + atexit); once is enough
            return
        self.running = False
        for f in self.fans:
            try:
                f.close()
            except Exception:
                pass
        self.oled.close()
        try:
            lgpio.gpiochip_close(self.h)
        except Exception:
            pass                  # 'unknown handle' if already closed — harmless


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
            if p == f"{API}/oled":
                return self._send(200, daemon.oled.status())
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
            if p == f"{API}/oled":
                o = daemon.oled
                if "enabled" in body:
                    o.set_enabled(body["enabled"])
                if "contrast" in body:
                    o.set_contrast(body["contrast"])
                return self._send(200, o.status())
            if p == f"{API}/oled/text":
                o = daemon.oled
                if not o.dev:
                    return self._send(409, {"error": "oled unavailable"})
                lines = body.get("lines")
                if lines is None:
                    lines = str(body.get("text", "")).split("\n")
                if not any(str(x).strip() for x in lines):
                    return self._send(400, {"error": "text or lines required"})
                try:
                    o.set_text([str(x) for x in lines], size=body.get("size"),
                               align=body.get("align", "center"),
                               invert=bool(body.get("invert", False)),
                               duration=float(body.get("duration", 0)))
                except Exception as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, o.status())
            if p == f"{API}/oled/image":
                o = daemon.oled
                if not o.dev:
                    return self._send(409, {"error": "oled unavailable"})
                b64 = body.get("image_b64", "")
                if not b64:
                    return self._send(400, {"error": "image_b64 required (PNG/JP/BMP)"})
                try:
                    raw = base64.b64decode(b64.split(",", 1)[-1])
                    o.set_image(raw, fit=body.get("fit", "contain"),
                                invert=bool(body.get("invert", False)),
                                threshold=int(body.get("threshold", 128)),
                                duration=float(body.get("duration", 0)))
                except Exception as e:
                    return self._send(400, {"error": f"bad image: {e}"})
                return self._send(200, o.status())
            if p == f"{API}/oled/clear":
                daemon.oled.clear_override()
                return self._send(200, daemon.oled.status())
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
    if d.oled.dev is not None:
        threading.Thread(target=d.oled_loop, daemon=True).start()
        if any(str(p).startswith("speed") for p in cfg["oled_pages"]):
            threading.Thread(target=d.oled.speedtest_loop, daemon=True).start()
    print(f"[pironman-extend] up: {len(d.fans)} fan(s), rgb_enabled={d.rgb.enabled}, "
          f"oled={'on' if d.oled.dev else 'off'}, "
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
    # --- OLED anomaly/threshold logic ---
    th = DEFAULTS["oled_thresholds"]
    assert oled_severity("cpu", 85, th, 4) == 2
    assert oled_severity("cpu", 72, th, 4) == 1
    assert oled_severity("cpu", 50, th, 4) == 0
    assert oled_severity("load", 4 * 3.0, th, 4) == 2      # per-core 3.0
    assert oled_severity("load", 4 * 1.6, th, 4) == 1
    assert oled_severity("cpu", None, th, 4) == 0
    clean = {"cpu": 40, "load": 1.0, "disk": 30, "ram": 50, "ssd": 30}
    assert oled_build_alerts(clean, 0, False, th, 4) == []
    assert oled_build_alerts(clean, 0x1, False, th, 4)[0][0] == "DUSUK VOLTAJ"
    assert oled_build_alerts(clean, 0, True, th, 4)[0][0] == "DISK SALT-OKUR"
    assert oled_build_alerts({**clean, "cpu": 90}, 0, False, th, 4)[0][0] == "CPU KRITIK"
    assert human_bytes(476 * 1024**3) == "476G"
    assert human_bytes(1000204140544) == "932G"
    assert human_bytes(int(1.5 * 1024**4)) == "1.5T"
    assert human_bytes(None) == "-"
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
