# 抖音账号视频自动总结工具

监控指定抖音账号的每日视频更新，自动下载音频 → 语音转文字 → AI总结 → 推送到微信。

## 工作流程

```
TikHub API 获取新视频
       ↓
yt-dlp 下载音频
       ↓
Whisper 语音转文字
       ↓
DeepSeek/GPT AI总结
       ↓
Server酱/企业微信 推送到手机
```

## 每月成本估算

| 服务 | 用途 | 费用 |
|------|------|------|
| TikHub API | 获取抖音视频数据 | 约 ¥5-15/月（按量计费） |
| DeepSeek API | AI内容总结 | 约 ¥2-5/月 |
| Server酱 | 微信推送 | 免费 |
| GitHub Actions | 定时运行 | 免费（公开仓库） |
| **合计** | | **约 ¥7-20/月** |

---

## 快速开始（5步完成）

### 第1步：注册获取 API Key

你需要注册以下服务并获取 API Key：

#### 1.1 TikHub API（获取抖音视频数据）
1. 访问 https://user.tikhub.io 注册账号
2. 在用户中心获取 API Key
3. 新用户有免费额度，之后按量计费（约 $0.01/次请求）

#### 1.2 DeepSeek API（AI总结，推荐）
1. 访问 https://platform.deepseek.com 注册
2. 创建 API Key
3. 费用极低，约 ¥0.001/千token

> 也可用其他兼容 OpenAI 接口的大模型（通义千问、智谱GLM等），修改 config.yaml 中的 base_url 和 model 即可。

#### 1.3 Server酱（微信推送，推荐）
1. 访问 https://sct.ftqq.com 微信扫码登录
2. 获取 SendKey
3. 关注「Server酱」微信公众号以接收消息

> 也可用企业微信机器人（见下方说明）。

### 第2步：创建 GitHub 仓库

1. 在 GitHub 上创建一个**公开仓库**（Public，免费使用 Actions）
2. 将本项目所有文件上传到仓库

### 第3步：配置 GitHub Secrets

在仓库页面：`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

依次添加以下 Secrets：

| Secret 名称 | 值 | 说明 |
|-------------|------|------|
| `DOUYIN_UNIQUE_ID` | `zhenrutie001` | 抖音号 |
| `DOUYIN_NICKNAME` | `真如铁` | 昵称（用于推送标题） |
| `DOUYIN_SEC_UID` | `MS4wLjABAAAAf0C1gFEdMvoFGiiMUZbYQeLVpezDCv4fyNjWk9W2myE` | 已验证的sec_uid（可选，预填可省一次API调用） |
| `TIKHUB_API_KEY` | 你的TikHub Key | 从 TikHub 获取 |
| `LLM_API_KEY` | 你的DeepSeek Key | 从 DeepSeek 获取 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | LLM接口地址 |
| `LLM_MODEL` | `deepseek-chat` | 模型名称 |
| `PUSH_METHOD` | `serverchan` | 推送方式 |
| `SERVERCHAN_KEY` | 你的SendKey | 从Server酱获取 |

如果使用企业微信机器人推送，则添加：
| Secret 名称 | 值 |
|-------------|------|
| `PUSH_METHOD` | `wecom` |
| `WECOM_WEBHOOK` | 企业微信机器人Webhook地址 |

### 第4步：启用 GitHub Actions

1. 在仓库页面点击 `Actions` 标签
2. 如果提示需要确认，点击 `I understand my workflows, go ahead and enable them`
3. 每天北京时间 20:00 会自动运行

### 第5步：手动测试

在 `Actions` 页面：
1. 选择 `Daily Douyin Stock Summary` 工作流
2. 点击 `Run workflow` 手动触发一次测试
3. 查看运行日志确认是否成功

---

## 本地测试

如果你想先在本地测试再部署到 GitHub：

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装 ffmpeg（音频处理）
# Windows: 下载 https://ffmpeg.org/download.html
# Mac: brew install ffmpeg
# Linux: sudo apt install ffmpeg

# 3. 编辑配置
# 打开 config.yaml，填入你的 API Key 和推送配置

# 4. 运行
python main.py
```

---

## 企业微信机器人推送（备选方案）

如果不想用 Server酱，可以用企业微信机器人：

1. 打开企业微信 APP 或 PC 端
2. 进入一个群聊（可以只有你自己）→ 右上角 `...` → `群机器人` → `添加`
3. 创建机器人，复制 Webhook 地址（格式：`https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx`）
4. 在 GitHub Secrets 中配置 `PUSH_METHOD=wecom` 和 `WECOM_WEBHOOK=你的Webhook地址`

---

## 配置说明（config.yaml）

```yaml
# 抖音账号
douyin:
  unique_id: "zhenrutie001"    # 抖音号
  nickname: "真如铁"            # 推送消息中显示的昵称

# 大模型配置 - 可切换为其他 OpenAI 兼容接口
llm:
  provider: "deepseek"
  api_key: "your-key"
  base_url: "https://api.deepseek.com/v1"
  model: "deepseek-chat"

# Whisper 模型 - 越大越准但越慢
whisper:
  model_size: "base"           # tiny(最快) / base(推荐) / small(更准) / medium(最准)
  language: "zh"
  device: "cpu"

# 运行参数
runtime:
  lookback_hours: 24           # 查看最近多少小时的视频
  max_videos_per_run: 5        # 每次最多处理几个视频
```

---

## 更换监控的抖音账号

修改 config.yaml 或 GitHub Secrets 中的：
- `DOUYIN_UNIQUE_ID` - 目标抖音号
- `DOUYIN_NICKNAME` - 显示昵称

即可监控任意抖音账号。

---

## 更换推送时间

编辑 `.github/workflows/daily-summary.yml` 中的 cron 表达式：

```yaml
on:
  schedule:
    - cron: '0 12 * * *'   # 12:00 UTC = 20:00 北京时间
```

常用时间（北京时间）：
- `0 12 * * *` → 20:00
- `0 14 * * *` → 22:00
- `0 2 * * *`  → 10:00
- `0 4 * * *`  → 12:00

> 注意：GitHub Actions 定时任务可能有 5-30 分钟的延迟。

---

## 常见问题

### Q: 推送收不到消息？
- Server酱：确认已关注「Server酱」微信公众号
- 企业微信：确认机器人 Webhook 地址正确
- 查看 Actions 运行日志中的错误信息

### Q: 视频下载失败？
- 抖音可能有反爬限制，稍后重试
- 确认网络可以正常访问抖音
- yt-dlp 版本过旧，运行 `pip install -U yt-dlp`

### Q: 转录效果不好？
- 将 whisper model_size 从 `base` 改为 `small`（更准但更慢）
- 确认视频中有清晰的语音内容

### Q: TikHub API 报错？
- 检查 API Key 是否正确
- 检查账户余额是否充足
- 查看 TikHub 文档：https://docs.tikhub.io

### Q: GitHub Actions 没有定时运行？
- 公开仓库的定时任务更可靠
- GitHub Actions 定时可能有延迟，属正常现象
- 可以手动 `Run workflow` 触发

### Q: 想要换成其他大模型？
修改 config.yaml：
```yaml
# 通义千问
llm:
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen-turbo"

# 智谱GLM
llm:
  base_url: "https://open.bigmodel.cn/api/paas/v4"
  model: "glm-4-flash"
```

---

## 项目结构

```
douyin-stock-monitor/
├── .github/
│   └── workflows/
│       └── daily-summary.yml    # GitHub Actions 定时任务
├── main.py                      # 主程序
├── config.yaml                  # 配置文件（本地运行用）
├── requirements.txt             # Python 依赖
├── state.json                   # 状态记录（自动更新）
└── README.md                    # 本文件
```

---

## 免责声明

- 本工具仅供学习和个人使用
- AI 总结的内容可能存在偏差，不构成任何投资建议
- 请遵守抖音平台的使用条款
- 视频内容版权归原作者所有
