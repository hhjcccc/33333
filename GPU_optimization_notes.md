# 自动射击识别：不使用显卡的提速方案（CPU）

按你的要求：**不考虑调用显卡**，只做 CPU 侧优化。

## 先定目标

你的参数下（140x140 区域、毫秒级循环），提速重点是：
1. 降低每帧 Python 层开销
2. 降低 `detect_ring` 的几何计算开销
3. 让状态机逻辑与检测逻辑互不阻塞

## 优先级最高的改动

### 1) 预计算环形与方向掩码（一次性）
不要每帧 `np.where + for + math.hypot`。在 `__init__` 中预计算：
- 环形区域掩码（`inner_r <= d <= outer_r`）
- 8 个方向掩码（L/R/U/D/UL/UR/DL/DR）

每帧只做：
- `ring_red = red_mask & annulus_mask`
- `dirs[dir] = (ring_red & dir_mask).any()`

这样能把大量 Python 循环转成 NumPy 位运算。

### 2) 用布尔切片替代复杂分支
`detect_cross` 已经很轻量，继续保持布尔切片即可，不要引入额外对象。

### 3) 检测与动作分离
- 检测线程：尽量稳定高频，负责更新 `detected` 和时间戳。
- 动作线程：根据状态机执行点击/切枪。

可减少“点击 sleep”对检测帧率的拖累。

### 4) 分段打点，先测再调
对以下阶段单独计时：
- `capture`
- `red_mask`
- `detect_ring`
- `state_machine`

优先优化耗时占比最大的段。

## 可直接替换的 `detect_ring` 思路

在 `__init__` 增加预计算（示意）：

```python
h = w = self.region_half * 2
y, x = np.ogrid[:h, :w]
cx, cy = w // 2, h // 2
dx, dy = x - cx, y - cy
d = np.hypot(dx, dy)

self.annulus_mask = (d >= self.inner_r) & (d <= self.outer_r)
self.dir_masks = {
    "L":  (np.abs(dy) < 8) & (dx < 0),
    "R":  (np.abs(dy) < 8) & (dx > 0),
    "U":  (np.abs(dx) < 8) & (dy < 0),
    "D":  (np.abs(dx) < 8) & (dy > 0),
    "UL": (dx < 0) & (dy < 0),
    "UR": (dx > 0) & (dy < 0),
    "DL": (dx < 0) & (dy > 0),
    "DR": (dx > 0) & (dy > 0),
}
```

把 `detect_ring` 改成：

```python
def detect_ring(self, red_mask):
    ring_red = red_mask & self.annulus_mask

    if ring_red.sum() < 10:
        return False

    dirs = {k: (ring_red & m).any() for k, m in self.dir_masks.items()}
    return (
        (dirs["L"] and dirs["R"]) or
        (dirs["U"] and dirs["D"]) or
        (dirs["UL"] and dirs["DR"]) or
        (dirs["UR"] and dirs["DL"])
    )
```

## 参数建议（先稳再快）

- `sleep_interval`：先从 `0.003` 调到 `0.002` 观察 CPU 占用。
- `fire_cooldown`：保持 `0.06`，先不动，避免“提速后误触发”。
- `target_hold_time`：维持 `0.01`，先保证稳定命中判定。

## 一句话结论

不走 GPU 的前提下，**最有效**的是把 `detect_ring` 改为“预计算掩码 + 向量化布尔运算”，并把检测与点击执行解耦。
