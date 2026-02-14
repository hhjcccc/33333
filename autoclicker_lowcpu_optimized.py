import threading
import time
from dataclasses import dataclass

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
    """CPU 优化版：预计算掩码 + 检测/动作分离线程 + 低分配开销。"""

    def __init__(self):
        self.running = False
        self.exit_program = False

        self.sw, self.sh = pyautogui.size()
        self.cx, self.cy = self.sw // 2, self.sh // 2

        # ---------- 识别区域 ----------
        self.region_half = 70
        self.inner_r = 40
        self.outer_r = 70
        self.cross_r = 12

        # ---------- 时间参数 ----------
        self.sleep_interval = 0.003
        self.detect_interval = 0.0015
        self.fire_cooldown = 0.06

        # ---------- 红色行为 ----------
        self.red_grace_time = 0.09
        self.max_red_hold_time = 0.2
        self.last_red_time = 0.0
        self.force_red_reset_time = 0.0

        # ---------- 切枪 ----------
        self.switch_weapon_mode = False
        self.switch_delay = 0.2
        self.post_switch_block = 0.3
        self.pending_switch = False
        self.switch_time = 0.0
        self.block_fire_until = 0.0

        # ---------- 武器 ----------
        self.weapon_cycle = [2, 3, 5, 4, 6]
        self.current_weapon = 2
        self.melee_weapons = {3}

        # ---------- 射击状态 ----------
        self.shot_fired_this_weapon = False
        self.target_hold_start = 0.0
        self.target_hold_time = 0.01
        self.last_fire_time = 0.0

        self.click_count = 0

        # 注意：mss 在 Windows 下内部句柄是 thread-local 的，
        # 不能在一个线程创建后在另一个线程直接复用。
        # 因此主线程不持有全局 mss 实例，检测线程内按线程创建并复用。
        self.sct = None
        self.capture_region = {
            "left": self.cx - self.region_half,
            "top": self.cy - self.region_half,
            "width": self.region_half * 2,
            "height": self.region_half * 2,
        }

        self.h = self.capture_region["height"]
        self.w = self.capture_region["width"]

        # 预计算几何掩码：避免每帧 np.where + for + hypot
        self.annulus_mask, self.dir_masks = self._build_ring_masks()

        # 跨线程检测状态
        self._det_state = DetectionState()
        self._det_lock = threading.Lock()
        self._det_thread = None

    # ================= 预计算 =================

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

    # ================= 图像 =================

    def capture(self, sct):
        # BGRA -> ndarray
        return np.asarray(sct.grab(self.capture_region))

    @staticmethod
    def red_mask(img):
        b = img[:, :, 0]
        g = img[:, :, 1]
        r = img[:, :, 2]
        return (r >= 230) & (g < 25) & (b < 25)

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

    # ================= 状态 =================

    def reset_red_state(self):
        self.last_red_time = 0.0
        self.force_red_reset_time = 0.0
        self.target_hold_start = 0.0

    def reset_per_weapon_fire_state(self):
        self.shot_fired_this_weapon = False

    # ================= 武器 =================

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

    # ================= 鼠标 =================

    def click_left(self):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.005)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.click_count += 1
        self.last_fire_time = time.monotonic()

    # ================= 键盘 =================

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

    # ================= 检测线程 =================

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

    # ================= 主循环（动作状态机） =================

    def run(self):
        print("=" * 60)
        print("Pixel Gun 3D 自动射击（CPU 优化版）")
        print("F2 启动/停止 | F3 退出 | F8 切枪模式")
        print("循环武器:", self.weapon_cycle)
        print("近战跳过:", self.melee_weapons)
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


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    AutoClickerLowCPUOptimized().run()
