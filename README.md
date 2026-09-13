# Apple Store 自提库存监控

一个通过 Apple Store 在线商店查询直营店自提状态的低延迟监控程序。城市、定位、门店、产品型号、轮询间隔、桌面通知和 Bark 推送都由 JSON 配置控制。程序只查询和提醒，不登录、不下单。

仓库中的 `config.json` 保留当前本机配置：北京 6 家直营店、14 个 iPhone 型号，共 84 个“型号 × 门店”组合。通用化改动不会改变这套监控范围。`config.example.json` 是用于创建其他城市配置的模板。

## 功能

- 一个实例可监控任意一个城市、1～128 家直营店和 1～64 个产品型号。
- 每家门店每轮只发送一个合并请求，不会为每个“型号 × 门店”组合单独请求。
- 仅在 `pickupDisplay=available` 且 `storePickEligible=true` 时判定为可自提。
- 每个“产品编号 × 门店”独立去重；持续有货不会重复提醒，明确无货后再次有货会重新提醒。
- 通过独立的无头 Chrome 会话完成 Apple 页面握手，自动处理 403、541、限流、缓存过期和退避重试。
- 支持电脑声音、macOS 桌面通知，以及最多 8 台设备的 Bark 推送。
- Bark 推送使用 Apple 官方图标；发送有货和“监控已恢复”，不发送接口异常或部分库存未知。
- 状态和日志写入 `runtime/`，Bark 地址单独保存在被 Git 忽略的 `.bark-url`。

## 环境要求

- macOS
- Python 3.9 或更高版本
- `/Applications` 中安装 Google Chrome 或 Microsoft Edge
- 查询期间电脑保持开机、联网和开盖

程序只使用 Python 标准库，无需安装 pip 依赖。临时浏览器使用全新资料目录，不读取日常浏览器的 Cookie、账号或历史，结束或重建会话时会自动删除。

## 快速开始

1. 复制 `config.example.json` 为 `config.json`，或直接修改已有的 `config.json`。
2. 运行离线配置校验：

   ```sh
   python3 monitor.py --validate-config
   ```

3. 如需手机推送，在 iPhone 或 iPad 安装 Bark，然后双击 `configure-bark.command`。已有配置时，新地址会追加；多个地址也可以用英文逗号分隔后一次输入。
4. 双击 `start-monitor.command` 启动监控。按 `Ctrl+C` 停止。

也可以直接在终端运行：

```sh
cd /path/to/iphone-pickup-monitor
python3 monitor.py --validate-config
python3 monitor.py --setup-bark
python3 monitor.py --test-notify
caffeinate -i python3 monitor.py
```

已有 Bark 配置时追加一台设备：

```sh
python3 monitor.py --add-bark
```

## 配置城市、门店和型号

默认读取同目录的 `config.json`。也可以指定其他文件：

```sh
python3 monitor.py --config /path/to/my-config.json --validate-config
python3 monitor.py --config /path/to/my-config.json
```

核心结构如下：

```json
{
  "location": "200000",
  "city": "上海",
  "stores": {
    "STORE_ID_1": "门店名称一",
    "STORE_ID_2": "门店名称二"
  },
  "interval_seconds": 30,
  "timeout_seconds": 60,
  "max_cache_age_seconds": 30,
  "desktop_notifications": true,
  "sound": true,
  "alert_title": "上海 Apple Store 自提有货",
  "notification_group": "上海 Apple Store 自提",
  "products": [
    {
      "product_name": "显示在通知中的型号、容量和颜色",
      "part_number": "XXXXXX/A",
      "product_url": "https://www.apple.com.cn/shop/buy-iphone/iphone-model/xxxxxx/a"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `location` | 是 | Apple 自提查询使用的位置，可填写当地邮编或站点支持的位置文本。 |
| `city` | 是 | Apple 响应中的城市名称。程序会拒绝把其他城市的门店数据当成目标结果。 |
| `stores` | 是 | 门店编号到显示名称的映射；只监控这里列出的门店。支持 1～128 家。 |
| `products` | 是 | 产品数组。每项包含名称、完整产品编号和对应 Apple 商品页。支持 1～64 项。 |
| `interval_seconds` | 否 | 完整轮次的开始间隔，默认及最小值为 30 秒，最大 3600 秒。 |
| `timeout_seconds` | 否 | 单次网络操作超时，默认 60 秒，可设为 1～60 秒。 |
| `max_cache_age_seconds` | 否 | 可接受的响应缓存年龄，默认 30 秒，可设为 0～300 秒。 |
| `desktop_notifications` | 否 | 是否显示桌面通知，默认 `true`。 |
| `sound` | 否 | 是否播放电脑提示音，默认 `true`。 |
| `alert_title` | 否 | 有货通知标题；默认根据 `city` 生成。 |
| `notification_group` | 否 | Bark 通知分组；默认根据 `city` 生成。 |

`product_url` 必须来自 `https://www.apple.com/` 或 `https://www.apple.com.cn/`，同一配置中的商品必须属于同一个区域站点。程序会从第一个商品链接自动推导 Apple Store 域名和浏览器握手页面。

产品编号通常可从 Apple 商品页地址和自提查询请求中核对。门店编号可在 Apple 商品页执行一次自提搜索后，从浏览器开发者工具的 `pickup-message` 响应中查看。配置完成后先运行 `--validate-config`；它只校验结构并显示城市、门店数、型号数和组合数，不请求 Apple 接口。

一个运行实例对应一个城市配置。如果需要同时监控多个城市，建议复制项目目录，为每个城市使用独立的 `config.json`、`runtime/` 和进程。

## Bark 推送

Bark 基础地址格式为 `https://api.day.app/你的Key`，也支持 HTTPS 自建服务。不要把 Key 写进 `config.json`、README 或 Git。

地址每行一个，保存在 `.bark-url`；文件权限仅允许当前用户读写。也可以使用环境变量 `BARK_URLS` 提供英文逗号分隔的多个地址，单设备环境变量 `BARK_URL` 仍兼容。修改地址后需要重启正在运行的监控。

同一轮中，同型号有货的多家门店会合并为一条通知，不同型号分开发送。通知包含型号、门店、自提说明和检测时间，点击后打开对应 Apple 商品页。每台 Bark 设备使用独立发送队列，一台失败不会阻塞其他设备或库存查询。

## 延迟与可靠性

- 默认每 30 秒开始一轮。轮次内各门店依次查询，每两次门店请求至少间隔 2 秒。
- 实际提醒延迟还包括目标门店在轮次中的顺序、Apple 的库存更新与缓存，以及通知服务送达时间。
- Apple 没有向程序提供库存事件推送，查询结果表示“当前可否下单自提”，不表示库存件数。
- HTTP 403 或 541 会标为库存未知，并销毁当前 Chrome 会话。失败按 30、60、120、240、300 秒退避；服务器提供更长 `Retry-After` 时会遵从。
- 单个门店或型号缺失会标为未知，其他组合仍正常判断。未知状态不会被当作无货或有货。
- Bark 失败最多重试 3 次；超过 90 秒仍未开始的旧提醒会丢弃，避免发送过时库存。

## 状态、日志和命令

`runtime/status.json` 保存最新健康状态、城市、门店数、型号数、组合数、进程 PID、检查时间、请求耗时、缓存年龄和各组合结果。`runtime/monitor.log` 保存轮换日志，`runtime/launcher.log` 保存后台启动输出。

```sh
# 离线校验配置
python3 monitor.py --validate-config

# 查询一轮，不发送通知
python3 monitor.py --once --silent

# 连续查询三轮
python3 monitor.py --count 3 --silent

# 测试电脑和所有 Bark 设备
python3 monitor.py --test-notify

# 停止本目录中的监控
python3 monitor.py --stop

# 运行离线测试
python3 -m unittest discover -s tests -v
```

项目有单实例锁，同一目录不能同时运行两个监控进程。`start-monitor.command` 使用 `caffeinate` 防止电脑因闲置睡眠，但不能阻止关机、合盖休眠或断网。

## 文件与隐私

- `monitor.py`：监控程序。
- `config.json`：当前实际配置。
- `config.example.json`：通用配置模板。
- `start-monitor.command`、`stop-monitor.command`、`view-logs.command`：macOS 快捷命令。
- `configure-bark.command`：安全配置一个或多个 Bark 地址。
- `tests/`：离线测试和脱敏库存夹具。

`.gitignore` 排除了 `.bark-url`、`runtime/`、`.DS_Store`、Python 缓存和本地分发压缩包。上传或分享项目前，仍应检查未跟踪文件和压缩包内容。

## 数据来源与验证

- [Apple Store 在线商店](https://www.apple.com.cn/shop/buy-iphone)
- [Apple 中国大陆直营店列表](https://www.apple.com.cn/retail/storelist/)
- [Bark 官方推送文档](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)

离线测试覆盖配置校验、组合解析、跨门店和型号去重、未知状态、业务错误、HTTP 限流、缓存过期、通知链接、多 Bark 设备、渠道过滤、Chrome 请求参数、541 会话重建和合并请求。Bark 真机送达仍需使用自己的地址运行测试通知。
