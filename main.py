#!/usr/bin/env python3
"""
抖音账号视频自动总结工具
=========================
监控指定抖音账号的新视频 -> 下载音频 -> 语音转文字 -> AI总结 -> 微信推送

使用方式:
  本地运行:  python main.py
  GitHub Actions: 通过 .github/workflows/daily-summary.yml 自动定时运行
"""

import os
import sys
import json
import time
import yaml
import logging
import requests
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 北京时间
BJT = timezone(timedelta(hours=8))


# ============================================================
# 配置管理
# ============================================================
class Config:
    """从 config.yaml 或环境变量加载配置（GitHub Actions 优先读环境变量）"""

    def __init__(self):
        config_path = Path(__file__).parent / "config.yaml"
        file_config = {}
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                file_config = yaml.safe_load(f) or {}

        # 抖音账号
        self.douyin_unique_id = os.environ.get('DOUYIN_UNIQUE_ID') or \
            file_config.get('douyin', {}).get('unique_id', 'zhenrutie001')
        self.douyin_sec_uid = os.environ.get('DOUYIN_SEC_UID') or \
            file_config.get('douyin', {}).get('sec_uid', '')
        self.douyin_nickname = os.environ.get('DOUYIN_NICKNAME') or \
            file_config.get('douyin', {}).get('nickname', '真如铁')

        # TikHub API
        self.tikhub_api_key = os.environ.get('TIKHUB_API_KEY') or \
            file_config.get('tikhub', {}).get('api_key', '')
        # .io 域名在国内被墙；本地测试可改用 https://api.tikhub.dev/api/v1
        # GitHub Actions 在海外运行，默认 .io 不受影响
        self.tikhub_base_url = os.environ.get('TIKHUB_BASE_URL') or \
            file_config.get('tikhub', {}).get('base_url', 'https://api.tikhub.io/api/v1')

        # LLM
        llm_cfg = file_config.get('llm', {})
        self.llm_api_key = os.environ.get('LLM_API_KEY') or llm_cfg.get('api_key', '')
        self.llm_base_url = os.environ.get('LLM_BASE_URL') or \
            llm_cfg.get('base_url', 'https://api.deepseek.com/v1')
        self.llm_model = os.environ.get('LLM_MODEL') or llm_cfg.get('model', 'deepseek-chat')

        # Whisper
        whisper_cfg = file_config.get('whisper', {})
        self.whisper_model = os.environ.get('WHISPER_MODEL_SIZE') or \
            whisper_cfg.get('model_size', 'base')
        self.whisper_language = whisper_cfg.get('language', 'zh')
        self.whisper_device = whisper_cfg.get('device', 'cpu')

        # 推送
        push_cfg = file_config.get('push', {})
        self.push_method = os.environ.get('PUSH_METHOD') or push_cfg.get('method', 'serverchan')
        self.serverchan_key = os.environ.get('SERVERCHAN_KEY') or \
            push_cfg.get('serverchan_key', '')
        self.wecom_webhook = os.environ.get('WECOM_WEBHOOK') or \
            push_cfg.get('wecom_webhook', '')

        # 运行时
        rt_cfg = file_config.get('runtime', {})
        self.lookback_hours = int(os.environ.get('LOOKBACK_HOURS') or rt_cfg.get('lookback_hours', 24))
        self.max_videos = int(os.environ.get('MAX_VIDEOS') or rt_cfg.get('max_videos_per_run', 5))
        self.temp_dir = rt_cfg.get('temp_dir', './temp')
        self.state_file = Path(__file__).parent / rt_cfg.get('state_file', 'state.json')

        # 验证关键配置
        self._validate()

    def _validate(self):
        warnings = []
        if not self.tikhub_api_key:
            warnings.append("TikHub API Key 未配置，将尝试使用 yt-dlp 备用方案")
        if not self.llm_api_key:
            warnings.append("LLM API Key 未配置，无法进行AI总结")
        if self.push_method == 'serverchan' and not self.serverchan_key:
            warnings.append("Server酱 SendKey 未配置，无法推送")
        if self.push_method == 'wecom' and not self.wecom_webhook:
            warnings.append("企业微信 Webhook 未配置，无法推送")
        for w in warnings:
            logger.warning(w)


# ============================================================
# 状态管理（记录已处理的视频，避免重复总结）
# ============================================================
class StateManager:
    """管理已处理视频的状态，避免重复总结"""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {
            "last_video_id": "",
            "last_video_create_time": 0,
            "processed_video_ids": []
        }

    def is_processed(self, video_id: str) -> bool:
        return video_id in self.state.get("processed_video_ids", [])

    def mark_processed(self, video_id: str, create_time: int):
        processed = self.state.get("processed_video_ids", [])
        if video_id not in processed:
            processed.append(video_id)
        # 只保留最近 200 条记录，避免无限增长
        if len(processed) > 200:
            processed = processed[-200:]
        self.state["processed_video_ids"] = processed
        self.state["last_video_id"] = video_id
        self.state["last_video_create_time"] = create_time
        self._save()

    def _save(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        logger.info(f"状态已保存到 {self.state_file}")


# ============================================================
# 抖音监控（获取最新视频）
# ============================================================
class DouyinMonitor:
    """通过 TikHub API 获取抖音账号的最新视频列表"""

    def __init__(self, config: Config):
        self.config = config
        self.TIKHUB_BASE = config.tikhub_base_url
        self.session = requests.Session()
        # 必须带 User-Agent，否则会被 Cloudflare 拦截（error code 1010）
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        if config.tikhub_api_key:
            self.session.headers.update({
                "Authorization": f"Bearer {config.tikhub_api_key}"
            })

    def get_sec_uid(self) -> Optional[str]:
        """通过 unique_id 获取 sec_uid"""
        if self.config.douyin_sec_uid:
            return self.config.douyin_sec_uid

        if not self.config.tikhub_api_key:
            logger.warning("无 TikHub API Key，无法自动获取 sec_uid")
            return None

        try:
            # 通过 TikHub handler_user_profile_v2 获取用户信息
            url = f"{self.TIKHUB_BASE}/douyin/web/handler_user_profile_v2"
            resp = self.session.get(url, params={"unique_id": self.config.douyin_unique_id}, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # 响应结构：data.user_info.sec_uid
            user_info = data.get("data", {}).get("user_info", {}) or data.get("data", {})
            sec_uid = user_info.get("sec_uid") or user_info.get("uid", "")

            if sec_uid:
                logger.info(f"获取到 sec_uid: {sec_uid[:20]}...")
                return sec_uid

            logger.warning(f"未能从响应中提取 sec_uid: {json.dumps(data, ensure_ascii=False)[:200]}")
            return None

        except Exception as e:
            logger.error(f"获取 sec_uid 失败: {e}")
            return None

    def get_latest_videos(self) -> list:
        """获取最新视频列表"""
        sec_uid = self.get_sec_uid()

        if not sec_uid and not self.config.tikhub_api_key:
            logger.error("无法获取视频列表：缺少 sec_uid 和 TikHub API Key")
            return []

        if not self.config.tikhub_api_key:
            logger.error("缺少 TikHub API Key")
            return []

        try:
            # 尝试多个可能的 API 端点
            endpoints = [
                f"{self.TIKHUB_BASE}/douyin/web/fetch_user_post_videos",
                f"{self.TIKHUB_BASE}/douyin/app/v3/fetch_user_post_videos",
            ]

            params = {
                "count": 20,
                "max_cursor": 0,
            }
            if sec_uid:
                params["sec_user_id"] = sec_uid
            else:
                params["unique_id"] = self.config.douyin_unique_id

            for endpoint in endpoints:
                try:
                    logger.info(f"尝试获取视频列表: {endpoint}")
                    resp = self.session.get(endpoint, params=params, timeout=60)
                    resp.raise_for_status()
                    data = resp.json()

                    # 尝试多种可能的响应结构
                    videos = (
                        data.get("data", {}).get("aweme_list") or
                        data.get("data", {}).get("videos") or
                        data.get("aweme_list") or
                        []
                    )

                    if videos:
                        logger.info(f"获取到 {len(videos)} 条视频")
                        return self._parse_videos(videos)

                except requests.RequestException as e:
                    logger.warning(f"端点 {endpoint} 请求失败: {e}")
                    continue

            logger.error("所有端点均未能获取视频列表")
            return []

        except Exception as e:
            logger.error(f"获取视频列表失败: {e}")
            return []

    def _parse_videos(self, raw_videos: list) -> list:
        """解析视频数据，统一格式"""
        parsed = []
        for v in raw_videos:
            try:
                video_id = str(v.get("aweme_id") or v.get("id") or "")
                desc = v.get("desc") or ""
                create_time = v.get("create_time") or 0
                duration = v.get("duration") or 0

                # 获取分享链接
                share_url = v.get("share_url") or ""
                if not share_url and video_id:
                    share_url = f"https://www.douyin.com/video/{video_id}"

                # 获取统计数据
                stats = v.get("statistics") or {}
                digg_count = stats.get("digg_count") or 0
                comment_count = stats.get("comment_count") or 0
                collect_count = stats.get("collect_count") or 0

                parsed.append({
                    "id": video_id,
                    "title": desc.strip() or "无标题",
                    "create_time": int(create_time) if create_time else 0,
                    "duration_sec": int(duration) / 1000 if duration > 1000 else int(duration),
                    "share_url": share_url,
                    "digg_count": digg_count,
                    "comment_count": comment_count,
                    "collect_count": collect_count,
                })
            except Exception as e:
                logger.warning(f"解析视频数据失败: {e}")
                continue

        # 按创建时间降序排列
        parsed.sort(key=lambda x: x["create_time"], reverse=True)
        return parsed

    def filter_new_videos(self, videos: list, state: StateManager) -> list:
        """筛选出新的、未处理的视频"""
        now_ts = int(time.time())
        cutoff_ts = now_ts - self.config.lookback_hours * 3600

        new_videos = []
        for v in videos:
            # 跳过已处理的
            if state.is_processed(v["id"]):
                continue
            # 只处理 lookback_hours 时间内的视频
            if v["create_time"] > 0 and v["create_time"] < cutoff_ts:
                continue
            new_videos.append(v)

        # 限制每次处理的数量
        new_videos = new_videos[:self.config.max_videos]
        logger.info(f"筛选出 {len(new_videos)} 条新视频需要处理")
        return new_videos


# ============================================================
# 视频处理器（下载 -> 转录 -> 总结）
# ============================================================
class VideoProcessor:
    """下载视频音频、语音转文字、AI总结"""

    def __init__(self, config: Config):
        self.config = config
        self.temp_dir = Path(config.temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._whisper_model = None

    def download_audio(self, video_url: str, video_id: str) -> Optional[str]:
        """使用 yt-dlp 下载视频音频"""
        audio_path = self.temp_dir / f"{video_id}.mp3"

        if audio_path.exists():
            logger.info(f"音频文件已存在: {audio_path}")
            return str(audio_path)

        logger.info(f"下载音频: {video_url}")
        cmd = [
            "yt-dlp",
            "-x",                         # 仅提取音频
            "--audio-format", "mp3",       # 转为 MP3
            "--audio-quality", "5",        # 中等音质，够用且省空间
            "-o", str(audio_path.with_suffix('.%(ext)s')),
            "--no-playlist",
            "--no-warnings",
            "--no-check-certificates",     # 抖音证书有时有问题
            video_url
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"yt-dlp 下载失败: {result.stderr[:300]}")
                return None

            # 查找实际生成的文件
            for ext in ['.mp3', '.m4a', '.webm', '.opus']:
                actual = audio_path.with_suffix(ext)
                if actual.exists():
                    if ext != '.mp3':
                        # 转换为 mp3
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", str(actual), "-codec:a", "libmp3lame", "-qscale:a", "5", str(audio_path)],
                            capture_output=True, timeout=60
                        )
                        actual.unlink()
                    logger.info(f"音频下载完成: {audio_path}")
                    return str(audio_path)

            logger.error("未找到下载的音频文件")
            return None

        except subprocess.TimeoutExpired:
            logger.error("下载超时（120秒）")
            return None
        except FileNotFoundError:
            logger.error("yt-dlp 未安装，请运行: pip install yt-dlp")
            return None
        except Exception as e:
            logger.error(f"下载音频失败: {e}")
            return None

    def transcribe(self, audio_path: str) -> Optional[str]:
        """使用 faster-whisper 语音转文字"""
        logger.info(f"开始转录: {audio_path}")

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            logger.error("faster-whisper 未安装，请运行: pip install faster-whisper")
            return None

        try:
            # 延迟加载模型（避免启动时就加载）
            if self._whisper_model is None:
                logger.info(f"加载 Whisper 模型: {self.config.whisper_model}")
                self._whisper_model = WhisperModel(
                    self.config.whisper_model,
                    device=self.config.whisper_device,
                    compute_type="int8"
                )

            segments, info = self._whisper_model.transcribe(
                audio_path,
                language=self.config.whisper_language,
                beam_size=5,
                vad_filter=True           # 过滤静音段
            )

            transcript_parts = []
            for segment in segments:
                transcript_parts.append(segment.text.strip())

            transcript = " ".join(transcript_parts).strip()
            logger.info(f"转录完成，文本长度: {len(transcript)} 字符")

            if not transcript:
                logger.warning("转录结果为空")
                return None

            return transcript

        except Exception as e:
            logger.error(f"转录失败: {e}")
            return None

    def summarize(self, title: str, transcript: str, video_desc: str = "") -> Optional[str]:
        """使用 LLM 总结转录文本"""
        if not self.config.llm_api_key:
            logger.error("LLM API Key 未配置")
            return None

        logger.info(f"开始AI总结: {title[:30]}...")

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=self.config.llm_api_key,
                base_url=self.config.llm_base_url
            )

            # 如果转录文本太短，补充视频描述
            context = transcript
            if len(transcript) < 50 and video_desc:
                context = f"转录文本: {transcript}\n视频描述: {video_desc}"

            prompt = f"""你是专业的股市内容分析助手。请将以下抖音视频的转录文本总结为结构化摘要。

请严格按照以下格式输出：

**核心观点**
{{用1-2句话概括作者对市场的总体看法}}

**市场方向**
{{看多/看空/震荡，涉及哪些指数、板块或个股}}

**关键信息**
{{用要点列出提到的重要数据、技术信号、政策解读等}}

**操作建议**
{{如果有具体的操作建议请列出，没有则写"无具体建议"}}

要求：
- 保持简洁，总字数控制在400字以内
- 如果转录文本质量差或内容不清晰，请尽量提取可用信息并说明
- 如果内容与股市无关，请简要总结视频内容即可

视频标题：{title}

转录文本：
{context}"""

            response = client.chat.completions.create(
                model=self.config.llm_model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=600,
                temperature=0.3
            )

            summary = response.choices[0].message.content.strip()
            logger.info(f"总结完成，长度: {len(summary)} 字符")
            return summary

        except Exception as e:
            logger.error(f"AI总结失败: {e}")
            return None

    def cleanup(self, video_id: str):
        """清理临时音频文件"""
        for ext in ['.mp3', '.m4a', '.webm', '.opus']:
            f = self.temp_dir / f"{video_id}{ext}"
            if f.exists():
                f.unlink()
                logger.info(f"已清理临时文件: {f}")


# ============================================================
# 推送通知
# ============================================================
class PushNotifier:
    """推送消息到微信（Server酱 或 企业微信机器人）"""

    def __init__(self, config: Config):
        self.config = config

    def push(self, title: str, content: str) -> bool:
        """发送推送通知"""
        method = self.config.push_method

        if method == "serverchan":
            return self._push_serverchan(title, content)
        elif method == "wecom":
            return self._push_wecom(title, content)
        else:
            logger.error(f"不支持的推送方式: {method}")
            return False

    def _push_serverchan(self, title: str, content: str) -> bool:
        """通过 Server酱 推送到微信"""
        if not self.config.serverchan_key:
            logger.error("Server酱 SendKey 未配置")
            return False

        try:
            # Server酱 Turbo 接口
            url = f"https://sctapi.ftqq.com/{self.config.serverchan_key}.send"
            resp = requests.post(url, data={
                "title": title[:32],       # 标题限制32字
                "desp": content
            }, timeout=15)

            result = resp.json()
            if result.get("code") == 0:
                logger.info("Server酱推送成功")
                return True
            else:
                logger.error(f"Server酱推送失败: {result}")
                return False

        except Exception as e:
            logger.error(f"Server酱推送异常: {e}")
            return False

    def _push_wecom(self, title: str, content: str) -> bool:
        """通过企业微信机器人推送"""
        if not self.config.wecom_webhook:
            logger.error("企业微信 Webhook 未配置")
            return False

        try:
            # 企业微信 markdown 消息
            msg = {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"## {title}\n\n{content}"
                }
            }
            resp = requests.post(self.config.wecom_webhook, json=msg, timeout=15)
            result = resp.json()

            if result.get("errcode") == 0:
                logger.info("企业微信推送成功")
                return True
            else:
                logger.error(f"企业微信推送失败: {result}")
                return False

        except Exception as e:
            logger.error(f"企业微信推送异常: {e}")
            return False


# ============================================================
# 消息格式化
# ============================================================
def format_message(nickname: str, summaries: list) -> tuple:
    """将多条视频总结格式化为一条推送消息"""
    now_bjt = datetime.now(BJT)
    date_str = now_bjt.strftime('%Y年%m月%d日')

    title = f"{nickname} 今日股市观点 ({date_str})"

    parts = [
        f"## 🎬 {nickname} 今日股市观点",
        f"",
        f"> 📅 {date_str} | 共 {len(summaries)} 条视频更新",
        f"> 🤖 AI自动总结，仅供参考",
        f"",
        f"---",
        f""
    ]

    for i, item in enumerate(summaries, 1):
        v = item["video"]
        summary = item["summary"]

        # 格式化时长
        duration_min = int(v.get("duration_sec", 0) // 60)
        duration_sec = int(v.get("duration_sec", 0) % 60)
        duration_str = f"{duration_min}分{duration_sec}秒" if duration_min > 0 else f"{duration_sec}秒"

        # 格式化互动数据
        likes = v.get("digg_count", 0)
        comments = v.get("comment_count", 0)

        parts.extend([
            f"### 📹 视频{i}：{v['title'][:50]}",
            f"",
            f"⏱ {duration_str} | 👍 {likes} | 💬 {comments}",
            f"",
            summary,
            f"",
            f"🔗 [查看原视频]({v['share_url']})",
            f"",
            f"---",
            f""
        ])

    parts.extend([
        f"",
        f"> ⚠️ 以上内容由AI自动总结，仅供参考，不构成投资建议",
        f"> 🕐 生成时间：{now_bjt.strftime('%Y-%m-%d %H:%M')}"
    ])

    return title, "\n".join(parts)


# ============================================================
# 主流程
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("抖音账号视频自动总结 - 开始运行")
    logger.info("=" * 60)

    # 1. 加载配置
    config = Config()
    logger.info(f"监控账号: {config.douyin_nickname} (抖音号: {config.douyin_unique_id})")

    # 2. 初始化组件
    state = StateManager(config.state_file)
    monitor = DouyinMonitor(config)
    processor = VideoProcessor(config)
    notifier = PushNotifier(config)

    # 3. 获取最新视频
    videos = monitor.get_latest_videos()
    if not videos:
        logger.warning("未获取到任何视频，程序结束")
        return

    # 4. 筛选新视频
    new_videos = monitor.filter_new_videos(videos, state)
    if not new_videos:
        logger.info("没有新视频需要处理，程序结束")
        return

    # 5. 逐个处理视频
    summaries = []
    for video in new_videos:
        logger.info(f"\n处理视频: {video['title'][:50]} (ID: {video['id']})")

        # 5.1 下载音频
        audio_path = processor.download_audio(video["share_url"], video["id"])

        # 5.2 语音转文字
        transcript = None
        if audio_path:
            transcript = processor.transcribe(audio_path)

        # 5.3 AI总结
        summary = None
        if transcript:
            summary = processor.summarize(video["title"], transcript, video.get("title", ""))
        else:
            # 转录失败，尝试仅用标题做简要总结
            logger.warning("转录失败，使用视频标题生成简要摘要")
            if config.llm_api_key:
                fallback = f"**核心观点**\n视频转录失败，无法获取详细内容。\n\n**视频标题**\n{video['title']}"
                summary = fallback
            else:
                summary = f"视频转录失败。标题: {video['title']}"

        if summary:
            summaries.append({
                "video": video,
                "summary": summary
            })

        # 5.4 清理临时文件
        processor.cleanup(video["id"])

        # 5.5 标记为已处理
        state.mark_processed(video["id"], video["create_time"])

        # 避免请求过快
        time.sleep(2)

    # 6. 推送通知
    if summaries:
        title, content = format_message(config.douyin_nickname, summaries)
        logger.info(f"准备推送: {title}")
        success = notifier.push(title, content)

        if success:
            logger.info("推送成功！")
        else:
            logger.error("推送失败，请检查推送配置")

        # 打印内容到控制台（便于调试）
        logger.info("\n" + "=" * 60)
        logger.info("推送内容预览:")
        logger.info("=" * 60)
        print("\n" + content + "\n")
    else:
        logger.warning("没有生成任何总结，跳过推送")

    logger.info("=" * 60)
    logger.info("运行完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
