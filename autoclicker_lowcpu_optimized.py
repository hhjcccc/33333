import argparse
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import mss
import numpy as np
import pyautogui
import win32api
import win32con
from pynput import keyboard as pynput_keyboard


@dataclass
class DetectionState:
    detected: bool = False
    ts: float = 0.0


class AutoClickerLowCPUOptimized:
    """CPU 优化版：支持 JSON 配置文件。"""

    DEFAULT_CONFIG = {
        "region_half": 70,
        "inner_r": 40,
        "outer_r": 70,
        "cross_r": 12,
        "sleep_interval": 0.003,
        "detect_interval": 0.0015,
        "fire_cooldown": 0.06,
        "red_grace_time": 0.09,
        "max_red_hold_time": 0.2,
        "switch_weapon_mode": False,
        "switch_delay": 0.2,
        "post_switch_block": 0.3,
        "weapon_cycle": [2, 3, 5, 4, 6],
        "current_weapon": 2,
        "melee_weapons": [3],
        "target_hold_time": 0.01,
        "red_threshold": {"r_min": 230, "g_max": 25, "b_max": 25},
    }

    @classmethod
    def load_config(cls, config_path=None):
        cfg = dict(cls.DEFAULT_CONFIG)
        if not config_path:
            return cfg

        path = Path(config_path)
        if not path.exists():
            print(f"[WARN] 配置文件不存在，使用默认配置: {path}")
            return cfg

        with path.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)

        for k, v in user_cfg.items():
            if k == "red_threshold" and isinstance(v, dict):
                merged = dict(cfg["red_threshold"])
                merged.update(v)
                cfg["red_threshold"] = merged
            else:
                cfg[k] = v
        return cfg

    def __init__(self, config=None):
        self.config = dict(self.DEFAULT_CONFIG if config is None else config)

        self.running = False
        self.exit_program = False

        self.sw, self.sh = pyautogui.size()
        self.cx, self.cy = self.sw // 2, self.sh // 2

        # ---------- 识别区域 ----------
        self.region_half = int(self.config["region_half"])
        self.inner_r = int(self.config["inner_r"])
        self.outer_r = int(self.config["outer_r"])
        self.cross_r = int(self.config["cross_r"])

        # ---------- 时间参数 ----------
        self.sleep_interval = float(self.config["sleep_interval"])
        self.detect_interval = float(self.config["detect_interval"])
        self.fire_cooldown = float(self.config["fire_cooldown"])

        # ---------- 红色行为 ----------
        self.red_grace_time = float(self.config["red_grace_time"])
        self.max_red_hold_time = float(self.config["max_red_hold_time"])
        self.last_red_time = 0.0
        self.force_red_reset_time = 0.0

        # ---------- 切枪 ----------
        self.switch_weapon_mode = bool(self.config["switch_weapon_mode"])
        self.switch_delay = float(self.config["switch_delay"])
        self.post_switch_block = float(self.config["post_switch_block"])
        self.pending_switch = False
        self.switch_time = 0.0
        self.block_fire_until = 0.0

        # ---------- 武器 ----------
        self.weapon_cycle = list(self.config["weapon_cycle"])
        self.current_weapon = int(self.config["current_weapon"])
        self.melee_weapons = set(self.config["melee_weapons"])

        # ---------- 射击状态 ----------
        self.shot_fired_this_weapon = False
        self.target_hold_start = 0.0
        self.target_hold_time = float(self.config["target_hold_time"])
        self.last_fire_time = 0.0

        # ---------- 红色阈值 ----------
        thr = self.config["red_threshold"]
        self.r_min = int(thr["r_min"])
        self.g_max = int(thr["g_max"])
        self.b_max = int(thr["b_max"])

        self.click_count = 0

        # mss 句柄线程内创建
        self.sct = None
        self.capture_region = {
            "left": self.cx - self.region_half,
            "top": self.cy - self.region_half,
            "width": self.region_half * 2,
            "height": self.region_half * 2,
        }

        self.h = self.capture_region["height"]
        self.w = self.capture_region["width"]

        self.annulus_mask, self.dir_masks = self._build_ring_masks()

        self._det_state = DetectionState()
        self._det_lock = threading.Lock()
        self._det_thread = None

    def _build_ring_masks(self):
        y, x = np.ogrid[: self.h, : self.w]
        cx, cy = self.w // 2, self.h // 2
        dx = x - cx
        dy = y - cy
        d = np.hypot(dx, dy)

        annulus = (d >= self.inner_r) & (d <= self.outer_r)
        dir_masks = {
            "L": (np.abs(dy) < 8) & (dx < 0),
            "R": (np.abs(dy) < 8) & (dx > 0),
            "U": (np.abs(dx) < 8) & (dy < 0),
            "D": (np.abs(dx) < 8) & (dy > 0),
            "UL": (dx < 0) & (dy < 0),
            "UR": (dx > 0) & (dy < 0),
            "DL": (dx < 0) & (dy > 0),
            "DR": (dx > 0) & (dy > 0),
        }
        return annulus, dir_masks

    def capture(self, sct):
        return np.asarray(sct.grab(self.capture_region))

    def red_mask(self, img):
        b = img[:, :, 0]
        g = img[:, :, 1]
        r = img[:, :, 2]
        return (r >= self.r_min) & (g < self.g_max) & (b < self.b_max)

    def detect_ring(self, mask):
        ring_red = mask & self.annulus_mask
        if ring_red.sum() < 10:
            return False

        d = {k: bool((ring_red & m).any()) for k, m in self.dir_masks.items()}
        return (
            (d["L"] and d["R"])
            or (d["U"] and d["D"])
            or (d["UL"] and d["DR"])
            or (d["UR"] and d["DL"])
        )

    def detect_cross(self, mask):
        cx, cy = self.w // 2, self.h // 2
        r = self.cross_r
        return (mask[cy - r : cy, cx].any() and mask[cy + 1 : cy + r, cx].any()) or (
            mask[cy, cx - r : cx].any() and mask[cy, cx + 1 : cx + r].any()
        )

    def detect_crosshair(self, img):
        mask = self.red_mask(img)
        return self.detect_ring(mask) or self.detect_cross(mask)

    def reset_red_state(self):
        self.last_red_time = 0.0
        self.force_red_reset_time = 0.0
        self.target_hold_start = 0.0

    def reset_per_weapon_fire_state(self):
        self.shot_fired_this_weapon = False

    def get_next_weapon(self):
        idx = self.weapon_cycle.index(self.current_weapon)
        return self.weapon_cycle[(idx + 1) % len(self.weapon_cycle)]

    def press_weapon_key(self, num):
        sc = {1: 0x02, 2: 0x03, 3: 0x04, 4: 0x05, 5: 0x06, 6: 0x07}[num]
        win32api.keybd_event(0, sc, win32con.KEYEVENTF_SCANCODE, 0)
        time.sleep(0.01)
        win32api.keybd_event(0, sc, win32con.KEYEVENTF_SCANCODE | win32con.KEYEVENTF_KEYUP, 0)

        self.current_weapon = num
        self.reset_per_weapon_fire_state()
        self.reset_red_state()

        now = time.monotonic()
        if num in self.melee_weapons:
            self.pending_switch = True
            self.switch_time = now + 0.3
            self.block_fire_until = 0.0
        else:
            self.block_fire_until = now + self.post_switch_block

    def click_left(self):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.005)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.click_count += 1
        self.last_fire_time = time.monotonic()

    def on_press(self, key):
        if key == pynput_keyboard.Key.f2:
            self.running = not self.running
            time.sleep(0.15)
        elif key == pynput_keyboard.Key.f3:
            self.exit_program = True
            return False
        elif key == pynput_keyboard.Key.f8:
            self.switch_weapon_mode = not self.switch_weapon_mode
            time.sleep(0.15)
        return None

    def _detection_worker(self):
        sct = mss.mss()
        while not self.exit_program:
            if not self.running:
                time.sleep(0.01)
                continue

            img = self.capture(sct)
            det = self.detect_crosshair(img)
            now = time.monotonic()

            with self._det_lock:
                self._det_state.detected = det
                self._det_state.ts = now

            time.sleep(self.detect_interval)

    def _read_detection_state(self):
        with self._det_lock:
            return self._det_state.detected, self._det_state.ts

    def run(self):
        print("=" * 60)
        print("Pixel Gun 3D 自动射击（CPU 优化版 / 配置文件版）")
        print("F2 启动/停止 | F3 退出 | F8 切枪模式")
        print("循环武器:", self.weapon_cycle)
        print("近战跳过:", self.melee_weapons)
        print("识别区域:", self.capture_region)
        print("红色阈值:", {"r_min": self.r_min, "g_max": self.g_max, "b_max": self.b_max})
        print("=" * 60)

        pynput_keyboard.Listener(on_press=self.on_press).start()

        self._det_thread = threading.Thread(target=self._detection_worker, daemon=True)
        self._det_thread.start()

        while not self.exit_program:
            now = time.monotonic()

            if self.pending_switch and now >= self.switch_time:
                self.pending_switch = False
                self.press_weapon_key(self.get_next_weapon())
                continue

            if now < self.block_fire_until:
                time.sleep(self.sleep_interval)
                continue

            if not self.running:
                time.sleep(0.05)
                continue

            detected, _ = self._read_detection_state()

            if detected:
                if self.last_red_time == 0.0:
                    self.last_red_time = now
                    self.force_red_reset_time = now
                    self.target_hold_start = now
                    self.reset_per_weapon_fire_state()

                if now - self.force_red_reset_time > self.max_red_hold_time:
                    self.reset_red_state()
                    time.sleep(0.01)
                    continue

                self.last_red_time = now

                can_fire = (
                    self.target_hold_start > 0
                    and now - self.target_hold_start >= self.target_hold_time
                    and not self.shot_fired_this_weapon
                    and self.current_weapon not in self.melee_weapons
                    and now - self.last_fire_time >= self.fire_cooldown
                )

                if can_fire:
                    self.click_left()
                    self.shot_fired_this_weapon = True
                    self.target_hold_start = 0.0

                    if self.switch_weapon_mode and not self.pending_switch:
                        self.pending_switch = True
                        self.switch_time = now + self.switch_delay
            else:
                if now - self.last_red_time > self.red_grace_time:
                    self.reset_red_state()
                    self.reset_per_weapon_fire_state()

            time.sleep(self.sleep_interval)

        print("程序结束，总射击次数:", self.click_count)


def parse_args():
    parser = argparse.ArgumentParser(description="AutoClicker CPU 优化版（配置文件）")
    parser.add_argument(
        "--config",
        default="autoclicker_config.json",
        help="JSON 配置文件路径（默认: autoclicker_config.json）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    args = parse_args()
    config = AutoClickerLowCPUOptimized.load_config(args.config)
    AutoClickerLowCPUOptimized(config=config).run()
