# BTC MA83 多周期穿越提醒（云端版）

纯 Python 脚本直连 OKX 公共行情接口，监控 **BTC-USDT** 的 **1H / 4H / 1D / 3D** 四个周期，
当收盘价穿越 **MA(83)** 均线时，通过**企业微信群机器人**推送提醒。

跑在 **GitHub Actions**（cron 每 5 分钟），**电脑关机也照常运行**，无需自有服务器。

## 文件说明

| 文件 | 作用 |
|------|------|
| `ma83_alert.py` | 检测主脚本（拉数据 → 算 MA83 → 判穿越 → 推送） |
| `.github/workflows/ma83-alert.yml` | GitHub Actions 定时任务配置 |
| `.gitignore` | 忽略缓存和状态文件 |

## 快速部署（5 步）

### 1. 创建 GitHub 仓库

在 GitHub 新建一个**公开仓库**（Public），例如 `ma83-alert`。

> 为什么用公开仓库：GitHub Actions 免费额度里，公开仓库每月 60000 分钟（够每 5 分钟跑），
> 私有仓库仅 2000 分钟（不够）。公开仓库的代码人人可见，因此 **webhook 绝不能写进代码**，
> 必须放到 GitHub Secret 中（见第 3 步）。

### 2. 上传文件

把本目录（`ma83_alert.py`、`.github/workflows/`、`.gitignore`）全部推到仓库根目录。

```bash
cd ma83-alert
git init
git add .
git commit -m "init ma83 alert"
git branch -M main
git remote add origin https://github.com/<你的用户名>/ma83-alert.git
git push -u origin main
```

### 3. 配置 webhook 密钥（强烈建议）

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

| 名称 | 值 |
|------|-----|
| `WECOM_WEBHOOK` | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key` |

> ⚠️ **这一步是必须的**。脚本已改为强制从环境变量 `WECOM_WEBHOOK` 读取，未配置时不会推送。
> 公开仓库任何人都能看到代码，把真实 webhook 写进代码等于把推送权交给别人。

### 4. 首次手动触发测试

进入仓库 **Actions** 页 → 左侧 `BTC MA83 多周期穿越提醒` → 右侧 **Run workflow** 按钮 → 运行。

首次运行只记录状态（不推送），因为要建立"价格在均线上方还是下方"的基准。

### 5. 确认定时任务生效

Actions 页看到 workflow 自动按每 5 分钟触发即表示部署成功。

## 配置项（均可通过环境变量 / Secret 覆盖）

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `WECOM_WEBHOOK` | 无（必填） | 企业微信群机器人 webhook |
| `INST_ID` | `BTC-USDT` | 监控标的 |
| `MA_PERIOD` | `83` | 均线周期 |
| `DATA_SOURCE` | `okx` | 数据源（预留 coinglass / coinmarketcap 扩展） |
| `QUIET_START_HOUR` | `0` | 免打扰开始小时（东八区） |
| `QUIET_END_HOUR` | `7` | 免打扰结束小时（东八区） |
| `MIN_PUSH_INTERVAL` | `3600` | 同一周期两次推送最小间隔（秒，默认 1 小时） |

## 多数据源扩展

脚本内置了数据源抽象层（`get_data_source()` / `fetch_candles()` 接口）。要接入
Coinglass、CoinMarketCap 等平台，只需新增一个实现了 `fetch_candles(inst_id, bar, limit)`
方法的数据源类，并在 `get_data_source()` 里注册即可，检测和推送逻辑无需改动。

## 功能特性

- **多周期监控**：同时监控 1H / 4H / 1D / 3D 四个周期的 MA83 穿越
- **免打扰**：默认 00:00 ~ 07:00（东八区）不推送任何消息，可通过 `QUIET_START_HOUR` / `QUIET_END_HOUR` 调整
- **去重防刷屏**：同一周期两次推送间隔至少 1 小时（可通过 `MIN_PUSH_INTERVAL` 调整）
- **首次建基准**：首次运行只记录价格相对均线的位置，不推送，避免误报

## 注意事项

- **免费额度**：本方案用公开仓库（60000 分钟/月），每 5 分钟一次（每次约 1 分钟）约 9000 分钟/月，**够用**。
- **cron 延迟**：GitHub Actions 的 schedule 在高峰期可能有几分钟延迟，不是精确到秒。
- **穿越判定**：基于 K 线**收盘价**（每根 K 线收线后判断），不是盘中实时价。
- **安全**：公开仓库代码人人可见，webhook 务必走 Secret（脚本已强制要求），不要提交进代码。
