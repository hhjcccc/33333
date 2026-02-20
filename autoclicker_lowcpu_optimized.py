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
    """CPU 优化版：抑制纯红背景/红光晕误检 + 稳定切枪状态机。"""

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
        self.switch_delay = 0.1
        self.min_switch_after_shot = 0.14
        self.post_switch_block = 0.3
        self.pending_switch = False
        self.switch_time = 0.0
        self.switch_armed_by_shot = False
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

        # ---------- 红色识别阈值 ----------
        self.r_strict_min = 230
        self.g_strict_max = 25
        self.b_strict_max = 25
        # 低饱和红（抗锯齿/亮度波动）兜底阈值
        self.r_soft_min = 170
        self.r_dom_delta = 40

        # ---------- 误检抑制 ----------
        # 环区域红色占比过高通常是纯红背景/大面积红光，不是准星环
        self.ring_fill_max = 0.72
        # 十字检测时，侧向参考线允许的红色占比上限
        self.cross_side_red_max = 0.35
        # 中心区域过红通常是身体红光晕贴脸，不是准星图案
        self.center_red_max = 0.45
        self.center_half = 8

        self.click_count = 0

        # 注意：mss 在 Windows 下内部句柄是 thread-local 的，
        # 不能在一个线程创建后在另一个线程直接复用。
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
        return np.asarray(sct.grab(self.capture_region))

    def red_mask(self, img):
        b = img[:, :, 0]
        g = img[:, :, 1]
        r = img[:, :, 2]

        strict = (r >= self.r_strict_min) & (g <= self.g_strict_max) & (b <= self.b_strict_max)
        soft = (r >= self.r_soft_min) & ((r - g) >= self.r_dom_delta) & ((r - b) >= self.r_dom_delta)
        return strict | soft

    def detect_ring(self, mask):
        ring_red = mask & self.annulus_mask
        red_pixels = int(ring_red.sum())
        if red_pixels < 10:
            return False

        fill_ratio = red_pixels / float(self.annulus_mask.sum())
        if fill_ratio > self.ring_fill_max:
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

        v_up = mask[cy - r : cy, cx]
        v_dn = mask[cy + 1 : cy + r, cx]
        if v_up.any() and v_dn.any() and 1 < cx < self.w - 2:
            side_l = mask[cy - r : cy + r, cx - 2]
            side_r = mask[cy - r : cy + r, cx + 2]
            if side_l.mean() <= self.cross_side_red_max and side_r.mean() <= self.cross_side_red_max:
                return True

        h_l = mask[cy, cx - r : cx]
        h_r = mask[cy, cx + 1 : cx + r]
        if h_l.any() and h_r.any() and 1 < cy < self.h - 2:
            side_u = mask[cy - 2, cx - r : cx + r]
            side_d = mask[cy + 2, cx - r : cx + r]
            if side_u.mean() <= self.cross_side_red_max and side_d.mean() <= self.cross_side_red_max:
                return True

        return False

    def detect_crosshair(self, img):
        mask = self.red_mask(img)

        # 中心区域若几乎被红色淹没，多数是红光晕/纯红背景
        cx, cy = self.w // 2, self.h // 2
        ch = self.center_half
        center = mask[cy - ch : cy + ch, cx - ch : cx + ch]
        if center.size > 0 and center.mean() > self.center_red_max:
            return False

        # 某些准星变红时只有中心小点发红，线段不稳定：给一个小点兜底
        dot_half = 2
        dot = mask[cy - dot_half : cy + dot_half + 1, cx - dot_half : cx + dot_half + 1]
        center_dot_ok = dot.size > 0 and int(dot.sum()) >= 3

        return self.detect_ring(mask) or self.detect_cross(mask) or center_dot_ok

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

    def press_weapon_key(self, num, from_shot_chain=False):
        sc = {1: 0x02, 2: 0x03, 3: 0x04, 4: 0x05, 5: 0x06, 6: 0x07}[num]
        win32api.keybd_event(0, sc, win32con.KEYEVENTF_SCANCODE, 0)
        time.sleep(0.01)
        win32api.keybd_event(0, sc, win32con.KEYEVENTF_SCANCODE | win32con.KEYEVENTF_KEYUP, 0)

        self.current_weapon = num
        self.reset_per_weapon_fire_state()
        self.reset_red_state()

        now = time.monotonic()
        if num in self.melee_weapons:
            if from_shot_chain:
                self.pending_switch = True
                self.switch_time = now + 0.08
            else:
                self.pending_switch = False
            self.switch_armed_by_shot = False
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
        print("红阈值 strict:", (self.r_strict_min, self.g_strict_max, self.b_strict_max), "soft:", (self.r_soft_min, self.r_dom_delta))
        print("阈值 ring_fill_max:", self.ring_fill_max, "cross_side_red_max:", self.cross_side_red_max)
        print("center_red_max:", self.center_red_max, "min_switch_after_shot:", self.min_switch_after_shot)
        print("=" * 60)

        pynput_keyboard.Listener(on_press=self.on_press).start()

        self._det_thread = threading.Thread(target=self._detection_worker, daemon=True)
        self._det_thread.start()

        while not self.exit_program:
            now = time.monotonic()

            if self.pending_switch and now >= self.switch_time:
                can_switch = self.current_weapon in self.melee_weapons or self.switch_armed_by_shot
                if can_switch:
                    self.pending_switch = False
                    shot_chain = self.switch_armed_by_shot
                    self.switch_armed_by_shot = False
                    self.press_weapon_key(self.get_next_weapon(), from_shot_chain=shot_chain)
                    continue
                self.pending_switch = False
                self.switch_armed_by_shot = False

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
                        self.switch_armed_by_shot = True
                        self.switch_time = now + max(self.switch_delay, self.min_switch_after_shot)
            else:
                if now - self.last_red_time > self.red_grace_time:
                    self.reset_red_state()
                    self.reset_per_weapon_fire_state()

            time.sleep(self.sleep_interval)

        print("程序结束，总射击次数:", self.click_count)


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    AutoClickerLowCPUOptimized().run()
