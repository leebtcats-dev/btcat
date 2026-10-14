#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC MA83 多周期穿越提醒 + 确认信号（云端版）
==========================================
纯 Python 直连 OKX 公共行情接口，监控 BTC-USDT 的 1H / 4H / 1D / 3D 四个周期。

两种提醒：
1. 普通穿越 —— 收盘价穿越 MA(83) 均线时推送。
2. 确认信号（重点）—— 穿越后的下一根已收线 K 线「整根完全不触及 MA83」时，
   说明突破有效、假突破概率低，用醒目格式重点推送。
   判定标准（最严格）：
     · 上穿确认：该根 K 线最低价 > MA83（整根都在均线上方）
     · 下穿确认：该根 K 线最高价 < MA83（整根都在均线下方）

设计要点：
1. 数据源可扩展 —— 默认用 OKX 公共 REST，预留 Coinglass / CoinMarketCap 接入框架。
2. 只用「已收线」K 线做判定，避免未收线 K 线的价格变动导致误判。
3. 状态持久化 —— 记录每周期的价格位置、待确认方向、上次推送时间。
4. 免打扰 —— 00:00~07:00（东八区）不推送任何消息。
5. 去重 —— 同一周期同类提醒间隔至少 1 小时。
6. 部署在 GitHub Actions（cron 每 5 分钟），电脑关机也照常运行。

依赖：仅需 requests（`pip install requests`）
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------------------
# 配置区（可通过环境变量 / GitHub Secrets 覆盖）
# ---------------------------------------------------------------------------

# 企业微信群机器人 webhook（必须放 GitHub Secrets，变量名 WECOM_WEBHOOK）
# 注意：公开仓库会把代码暴露，webhook 绝不能写死在代码里，必须从 Secret 注入。
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")

# 监控的标的与周期
INST_ID = os.environ.get("INST_ID", "BTC-USDT")
BARS = ["1H", "4H", "1D", "3D"]
MA_PERIOD = int(os.environ.get("MA_PERIOD", "83"))

# 状态文件目录（GitHub Actions 里用工作区目录，本地运行用脚本所在目录）
STATE_DIR = os.environ.get("STATE_DIR", os.path.dirname(os.path.abspath(__file__)))

# 东八区时区（用于消息时间显示）
TZ_CN = timezone(timedelta(hours=8))

# 免打扰时间段（东八区，左闭右开）：00:00 ~ 07:00 不推送
QUIET_START = int(os.environ.get("QUIET_START_HOUR", "0"))
QUIET_END = int(os.environ.get("QUIET_END_HOUR", "7"))

# 同一周期同类提醒的最小推送间隔（秒）：默认 1 小时
MIN_PUSH_INTERVAL = int(os.environ.get("MIN_PUSH_INTERVAL", "3600"))

# ---------------------------------------------------------------------------
# 数据源抽象层：预留多数据源扩展
# ---------------------------------------------------------------------------


class OkxSource:
    """OKX 公共行情数据源（免鉴权）。"""

    BASE = "https://www.okx.com/api/v5"

    def fetch_candles(self, inst_id: str, bar: str, limit: int = 120) -> list:
        """
        拉 K 线，返回升序列表，每项为 dict:
        {ts, o, h, l, c, confirm}  confirm=1 表示已收线，0 表示未收线
        """
        resp = requests.get(
            f"{self.BASE}/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit},
            timeout=20,
            headers={"User-Agent": "ma83-alert/1.0"},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX 返回错误: {payload.get('code')} {payload.get('msg')}")

        rows = payload.get("data", [])
        if not rows:
            raise RuntimeError("OKX 返回空数据")

        # OKX 返回降序（最新在前），每根格式 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        candles = [
            {
                "ts": int(r[0]),
                "o": float(r[1]),
                "h": float(r[2]),
                "l": float(r[3]),
                "c": float(r[4]),
                "confirm": r[8] == "1" if len(r) > 8 else True,
            }
            for r in rows
        ]
        candles.sort(key=lambda x: x["ts"])
        return candles


def get_data_source():
    """根据环境变量选择数据源（默认 OKX）。"""
    name = os.environ.get("DATA_SOURCE", "okx").lower()
    if name == "okx":
        return OkxSource()
    raise RuntimeError(f"未知数据源: {name}")


# ---------------------------------------------------------------------------
# 指标与判定
# ---------------------------------------------------------------------------


def calc_ma(closes: list, period: int) -> float:
    """简单移动平均，取最近 period 根（不足则用全部）。"""
    window = closes[-period:] if len(closes) >= period else closes
    if not window:
        raise RuntimeError("收盘价序列为空")
    return sum(window) / len(window)


def side_of(close: float, ma: float) -> int:
    """价格相对均线的位置：1 = 上方，-1 = 下方，0 = 相等。"""
    if close > ma:
        return 1
    if close < ma:
        return -1
    return 0


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------


def state_file(bar: str) -> str:
    return os.path.join(STATE_DIR, f".ma83_alert_state_{bar}.json")


def load_state(bar: str) -> dict:
    path = state_file(bar)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(bar: str, state: dict) -> None:
    path = state_file(bar)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------


def in_quiet_hours() -> bool:
    """判断当前是否处于免打扰时间段（东八区）。"""
    hour = datetime.now(TZ_CN).hour
    if QUIET_START < QUIET_END:
        return QUIET_START <= hour < QUIET_END
    return hour >= QUIET_START or hour < QUIET_END


def push_wecom(text: str) -> bool:
    """推送文本消息到企业微信群机器人。"""
    if not WECOM_WEBHOOK:
        print("  [推送失败] 未配置 WECOM_WEBHOOK，请在 GitHub Secrets 中设置")
        return False
    try:
        resp = requests.post(
            WECOM_WEBHOOK,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=15,
        )
        result = resp.json()
        ok = result.get("errcode") == 0
        if not ok:
            print(f"  [推送失败] {result}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  [推送异常] {type(e).__name__}: {e}")
        return False


def can_push(state: dict, push_key: str, now_ts: int) -> bool:
    """免打扰 + 间隔去重判断。push_key 用于区分普通穿越与确认信号。"""
    if in_quiet_hours():
        return False
    last = state.get(push_key, 0)
    return not last or (now_ts - last) >= MIN_PUSH_INTERVAL


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def check_one_bar(source, bar: str) -> None:
    """检查单个周期：穿越提醒 + 确认信号提醒。"""
    print(f"[{bar}] 拉取 K 线 ...")
    candles = source.fetch_candles(INST_ID, bar, limit=120)

    # 只使用已收线的 K 线做判定（未收线的最后一根价格仍在变动）
    closed = [c for c in candles if c["confirm"]]
    if len(closed) < MA_PERIOD + 2:
        print(f"[{bar}] 已收线数据不足({len(closed)}根)，跳过")
        return

    closes = [c["c"] for c in closed]
    ma = calc_ma(closes, MA_PERIOD)

    last = closed[-1]      # 最新已收线
    prev = closed[-2]      # 上一根已收线

    prev_side = side_of(prev["c"], ma)
    last_side = side_of(last["c"], ma)

    state = load_state(bar)
    last_recorded_side = state.get("side")
    pending = state.get("pending_confirm")  # 上一根发生穿越的方向，等待这一根确认
    now_ts = int(time.time())

    new_state = {
        "side": last_side,
        "ma": ma,
        "price": last["c"],
        "ts": now_ts,
        "last_push_ts": state.get("last_push_ts", 0),
        "last_confirm_push_ts": state.get("last_confirm_push_ts", 0),
        "pending_confirm": pending,
    }

    # 首次运行：只建基准，不推送
    if last_recorded_side is None:
        new_state["pending_confirm"] = None
        save_state(bar, new_state)
        print(f"[{bar}] 首次运行，记录基准 side={last_side} (ma={ma:.2f}, price={last['c']})")
        return

    now_str = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

    # ---- 1) 确认信号判定：这一根是否确认了上一根的穿越（整根不触及 MA83）----
    confirmed_dir = None
    if pending and pending == last_side:
        if pending == 1 and last["l"] > ma:
            confirmed_dir = "上穿"
        elif pending == -1 and last["h"] < ma:
            confirmed_dir = "下穿"

    if confirmed_dir:
        new_state["pending_confirm"] = None
        diff_pct = (last["c"] - ma) / ma * 100
        text = (
            f"⭐【MA83 确认信号 · 重点】\n"
            f"{INST_ID} {bar} 线 {confirmed_dir} 已确认\n"
            f"确认K线整根未触及 MA({MA_PERIOD})\n"
            f"开{last['o']:.2f} 高{last['h']:.2f} 低{last['l']:.2f} 收{last['c']:.2f}\n"
            f"MA{MA_PERIOD}: {ma:,.2f}\n"
            f"偏离: {diff_pct:+.3f}%\n"
            f"时间: {now_str}"
        )
        if can_push(state, "last_confirm_push_ts", now_ts):
            print(f"[{bar}] ⭐ 确认信号：{confirmed_dir}，重点推送 ...")
            if push_wecom(text):
                new_state["last_confirm_push_ts"] = now_ts
        else:
            print(f"[{bar}] ⭐ 确认信号：{confirmed_dir}，但免打扰/间隔内，跳过")
    elif pending:
        # 有待确认但这一根没确认成功（或方向已变），视为确认失败，清除
        new_state["pending_confirm"] = None
        print(f"[{bar}] 待确认未成立(pending={pending}, last_side={last_side})，清除")

    # ---- 2) 穿越判定：这一根是否发生新的穿越 ----
    crossed = prev_side != last_side and last_side != 0
    if crossed:
        direction = "上穿" if last_side == 1 else "下穿"
        diff_pct = (last["c"] - ma) / ma * 100
        # 记录待确认方向，由下一根已收线 K 线确认
        new_state["pending_confirm"] = last_side
        text = (
            f"【BTC MA83 提醒】\n"
            f"{INST_ID} {bar} 线 {direction} MA({MA_PERIOD})\n"
            f"当前价: {last['c']:,.2f}\n"
            f"MA{MA_PERIOD}: {ma:,.2f}\n"
            f"偏离: {diff_pct:+.3f}%\n"
            f"时间: {now_str}\n"
            f"（等待下一根收线确认）"
        )
        if can_push(state, "last_push_ts", now_ts):
            print(f"[{bar}] ✅ 触发穿越：{direction}，推送中 ...")
            if push_wecom(text):
                new_state["last_push_ts"] = now_ts
        else:
            print(f"[{bar}] ✅ 触发穿越：{direction}，但免打扰/间隔内，跳过")
    else:
        print(f"[{bar}] 未穿越 (prev={prev_side}, last={last_side}, ma={ma:.2f})")

    save_state(bar, new_state)


def main() -> None:
    print(f"=== BTC MA83 检测开始 ({datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}) ===")
    source = get_data_source()
    for bar in BARS:
        try:
            check_one_bar(source, bar)
        except Exception as e:  # noqa: BLE001
            print(f"[{bar}] ❌ 处理失败: {type(e).__name__}: {e}")
    print("=== 检测结束 ===\n")


if __name__ == "__main__":
    main()
