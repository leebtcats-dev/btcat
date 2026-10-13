#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC MA83 多周期穿越提醒（云端版）
================================
纯 Python 直连 OKX 公共行情接口，监控 BTC-USDT 的 1H / 4H / 1D / 3D 四个周期，
当收盘价穿越 MA(83) 均线时，通过企业微信群机器人 webhook 推送提醒。

设计要点：
1. 数据源可扩展 —— 默认用 OKX 公共 REST，预留 Coinglass / CoinMarketCap 接入框架。
2. 状态持久化 —— 用本地状态文件记录每个周期"价格在 MA 上方还是下方"，
   只在"穿越那一刻"推一次，避免同侧反复刷屏。
3. 部署在 GitHub Actions（cron 每 5 分钟），电脑关机也照常运行。

依赖：仅需 requests（`pip install requests`）
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------------------
# 配置区（可通过 GitHub Secrets / 环境变量覆盖，均有默认值）
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

# ---------------------------------------------------------------------------
# 数据源抽象层：预留多数据源扩展
# ---------------------------------------------------------------------------


class OkxSource:
    """OKX 公共行情数据源（免鉴权）。"""

    BASE = "https://www.okx.com/api/v5"

    def fetch_candles(self, inst_id: str, bar: str, limit: int = 120) -> list:
        """拉 K 线，返回升序的 close 列表（最新在最后）。"""
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
        # 转成升序的 (ts, close) 列表
        candles = [(int(r[0]), float(r[4])) for r in rows]
        candles.sort(key=lambda x: x[0])
        return candles


# 未来扩展：Coinglass / CoinMarketCap 数据源只需实现相同的 fetch_candles 接口，
# 然后在这里注册即可。
# class CoinglassSource: ...
# class CoinMarketCapSource: ...


def get_data_source():
    """根据环境变量选择数据源（默认 OKX）。"""
    name = os.environ.get("DATA_SOURCE", "okx").lower()
    if name == "okx":
        return OkxSource()
    # 预留：elif name == "coinglass": return CoinglassSource()
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def check_one_bar(source, bar: str) -> None:
    """检查单个周期是否穿越 MA83，穿越则推送。"""
    print(f"[{bar}] 拉取 K 线 ...")
    candles = source.fetch_candles(INST_ID, bar, limit=120)
    closes = [c for _, c in candles]

    if len(closes) < 2:
        print(f"[{bar}] 数据不足，跳过")
        return

    ma = calc_ma(closes, MA_PERIOD)
    prev_close = closes[-2]
    last_close = closes[-1]

    prev_side = side_of(prev_close, ma)
    last_side = side_of(last_close, ma)

    state = load_state(bar)
    last_recorded_side = state.get("side")  # None 表示首次运行

    # 首次运行：只记录状态，不推送
    if last_recorded_side is None:
        save_state(
            bar,
            {
                "side": last_side,
                "ma": ma,
                "price": last_close,
                "ts": int(time.time()),
            },
        )
        print(f"[{bar}] 首次运行，记录初始状态 side={last_side} (ma={ma:.2f}, price={last_close})")
        return

    # 穿越判定：最新一根相对上一根发生了跨越
    crossed = prev_side != last_side and last_side != 0

    # 状态更新（无论是否穿越都要写回当前 side）
    save_state(
        bar,
        {
            "side": last_side,
            "ma": ma,
            "price": last_close,
            "ts": int(time.time()),
        },
    )

    if crossed:
        direction = "上穿" if last_side == 1 else "下穿"
        diff_pct = (last_close - ma) / ma * 100
        now = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"【BTC MA83 提醒】\n"
            f"{INST_ID} {bar} 线 {direction} MA({MA_PERIOD})\n"
            f"当前价: {last_close:,.2f}\n"
            f"MA{MA_PERIOD}: {ma:,.2f}\n"
            f"偏离: {diff_pct:+.3f}%\n"
            f"时间: {now}"
        )
        print(f"[{bar}] ✅ 触发穿越：{direction}，推送中 ...")
        push_wecom(text)
    else:
        print(f"[{bar}] 未穿越 (prev={prev_side}, last={last_side}, ma={ma:.2f})")


def main() -> None:
    print(f"=== BTC MA83 多周期检测开始 ({datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}) ===")
    source = get_data_source()
    for bar in BARS:
        try:
            check_one_bar(source, bar)
        except Exception as e:  # noqa: BLE001
            print(f"[{bar}] ❌ 处理失败: {type(e).__name__}: {e}")
    print("=== 检测结束 ===\n")


if __name__ == "__main__":
    main()
