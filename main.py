#!/usr/bin/env python3
"""
抖音/微信视频号账号视频自动总结工具
==================================
监控指定账号的新视频 -> 下载音频 -> 语音转文字 -> AI总结 -> 微信推送

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
from typing import Optional, Callable

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

        # 微信视频号
        wechat_cfg = file_config.get('wechat_channels', {})
        wechat_env_enabled = os.environ.get('WECHAT_ENABLED', '').lower()
        self.wechat_enabled = (wechat_env_enabled == 'true') or \
            (wechat_env_enabled == '' and wechat_cfg.get('enabled', False))
        self.wechat_channel_id = os.environ.get('WECHAT_CHANNEL_ID') or \
            wechat_cfg.get('channel_id', '')
        self.wechat_username = os.environ.get('WECHAT_USERNAME') or \
            wechat_cfg.get('username', '')
        self.wechat_nickname = os.environ.get('WECHAT_NICKNAME') or \
            wechat_cfg.get('nickname', '微信视频号')

        # TikHub API
        self.tikhub_api_key = os.environ.get('TIKHUB_API_KEY') or \
            file_config.get('tikhub', {}).get('api_key', '')
        # .dev 域名国内外均可访问，更稳定
        # .io 在国内被墙，且在 GitHub Actions 环境偶发 "Response ended prematurely"（响应被截断）
        self.tikhub_base_url = os.environ.get('TIKHUB_BASE_URL') or \
            file_config.get('tikhub', {}).get('base_url', 'https://api.tikhub.dev/api/v1')

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
        default_state = {
            "last_video_id": "",
            "last_video_create_time": 0,
            "processed_video_ids": [],
            "wechat": {
                "last_video_id": "",
                "last_video_create_time": 0,
                "processed_video_ids": []
            }
        }
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                # 迁移旧格式：确保 wechat 字段存在
                for key, value in default_state.items():
                    if key not in loaded:
                        loaded[key] = value
                if "wechat" in loaded:
                    for key, value in default_state["wechat"].items():
                        if key not in loaded["wechat"]:
                            loaded["wechat"][key] = value
                return loaded
            except (json.JSONDecodeError, IOError):
                pass
        return default_state

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

    def is_processed_wechat(self, video_id: str) -> bool:
        return video_id in self.state.get("wechat", {}).get("processed_video_ids", [])

    def mark_processed_wechat(self, video_id: str, create_time: int):
        wechat_state = self.state.setdefault("wechat", {
            "last_video_id": "",
            "last_video_create_time": 0,
            "processed_video_ids": []
        })
        processed = wechat_state.get("processed_video_ids", [])
        if video_id not in processed:
            processed.append(video_id)
        if len(processed) > 200:
            processed = processed[-200:]
        wechat_state["processed_video_ids"] = processed
        wechat_state["last_video_id"] = video_id
        wechat_state["last_video_create_time"] = create_time
        self._save()

    def _save(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        logger.info(f"状态已保存到 {self.state_file}")


# ============================================================
# 公共工具
# ============================================================
def filter_new_videos(videos: list, config: Config, is_processed: Callable[[str], bool]) -> list:
    """通用新视频筛选逻辑：按时间和已处理状态过滤"""
    now_ts = int(time.time())
    cutoff_ts = now_ts - config.lookback_hours * 3600
    cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=BJT)

    logger.info(f"时间窗口: 只处理 {config.lookback_hours} 小时内 (即 {cutoff_dt.strftime('%Y-%m-%d %H:%M')} 之后) 的视频")

    new_videos = []
    for v in videos:
        video_dt = datetime.fromtimestamp(v["create_time"], tz=BJT) if v["create_time"] > 0 else None
        date_str = video_dt.strftime('%Y-%m-%d %H:%M') if video_dt else "无时间"

        # 跳过已处理的
        if is_processed(v["id"]):
            logger.info(f"  跳过已处理: {v['id']} ({date_str}) {v['title'][:30]}")
            continue
        # 只处理 lookback_hours 时间内的视频
        if v["create_time"] > 0 and v["create_time"] < cutoff_ts:
            logger.info(f"  跳过超期: {v['id']} ({date_str}) {v['title'][:30]}")
            continue
        logger.info(f"  新视频: {v['id']} ({date_str}) {v['title'][:30]}")
        new_videos.append(v)

    # 限制每次处理的数量
    new_videos = new_videos[:config.max_videos]
    logger.info(f"筛选出 {len(new_videos)} 条新视频需要处理")
    return new_videos


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

    def _fetch_videos_once(self, base_url: str, params: dict, max_retries: int = 5) -> list:
        """向指定域名请求视频列表，含指数退避重试"""
        endpoint = f"{base_url}/douyin/web/fetch_user_post_videos"
        backoff = [3, 5, 10, 15, 20]

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"尝试获取视频列表 (第{attempt}/{max_retries}次): {endpoint}")
                resp = self.session.get(endpoint, params=params, timeout=90)
                resp.raise_for_status()
                data = resp.json()

                videos = (
                    data.get("data", {}).get("aweme_list") or
                    data.get("data", {}).get("videos") or
                    data.get("aweme_list") or
                    []
                )

                if videos:
                    logger.info(f"获取到 {len(videos)} 条视频")
                    return videos

                logger.warning(f"响应中无视频数据")
                if attempt < max_retries:
                    time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                    continue
                return []

            except (requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout) as e:
                logger.warning(f"第{attempt}次请求连接中断: {type(e).__name__}: {e}")
                if attempt < max_retries:
                    time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                else:
                    logger.error(f"{base_url} 所有重试均因连接中断失败")
                    return []

            except requests.RequestException as e:
                logger.warning(f"第{attempt}次请求失败: {e}")
                if attempt < max_retries:
                    time.sleep(backoff[min(attempt - 1, len(backoff) - 1)])
                else:
                    logger.error(f"{base_url} 请求失败，已达最大重试次数: {e}")
                    return []

        return []

    def get_latest_videos(self) -> list:
        """获取最新视频列表，主域名失败后回退另一域名"""
        sec_uid = self.get_sec_uid()

        if not sec_uid and not self.config.tikhub_api_key:
            logger.error("无法获取视频列表：缺少 sec_uid 和 TikHub API Key")
            return []

        if not self.config.tikhub_api_key:
            logger.error("缺少 TikHub API Key")
            return []

        params = {
            "count": 20,
            "max_cursor": 0,
        }
        if sec_uid:
            params["sec_user_id"] = sec_uid
        else:
            params["unique_id"] = self.config.douyin_unique_id

        # 主域名
        primary_base = self.TIKHUB_BASE
        videos = self._fetch_videos_once(primary_base, params)
        if videos:
            return self._parse_videos(videos)

        # 回退另一域名
        fallback_base = primary_base.replace("api.tikhub.io", "api.tikhub.dev") if "api.tikhub.io" in primary_base \
            else primary_base.replace("api.tikhub.dev", "api.tikhub.io")
        if fallback_base != primary_base:
            logger.warning(f"主域名失败，尝试回退域名: {fallback_base}")
            videos = self._fetch_videos_once(fallback_base, params, max_retries=3)
            if videos:
                return self._parse_videos(videos)

        logger.error("所有域名均未能获取视频列表")
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
                    "platform": "douyin",
                    "source_nickname": self.config.douyin_nickname,
                })
            except Exception as e:
                logger.warning(f"解析视频数据失败: {e}")
                continue

        # 按创建时间降序排列
        parsed.sort(key=lambda x: x["create_time"], reverse=True)
        return parsed

    def filter_new_videos(self, videos: list, state: StateManager) -> list:
        """筛选出新的、未处理的视频"""
        return filter_new_videos(videos, self.config, state.is_processed)


# ============================================================
# 微信视频号监控
# ============================================================
class WeChatChannelsMonitor:
    """通过 TikHub API 获取微信视频号账号的最新视频列表"""

    # 视频号 V2 API 目前只在 .io 域名稳定工作
    WECHAT_BASE = "https://api.tikhub.io/api/v1"

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if config.tikhub_api_key:
            self.session.headers.update({
                "Authorization": f"Bearer {config.tikhub_api_key}"
            })
        self._username = None

    def resolve_username(self) -> Optional[str]:
        """解析视频号 finder username（v2_...@finder 格式）"""
        if self.config.wechat_username:
            logger.info(f"使用配置中的微信视频号 username")
            return self.config.wechat_username

        if not self.config.tikhub_api_key:
            logger.error("缺少 TikHub API Key，无法解析视频号 username")
            return None

        # 优先通过 share_url / channel_id 解析
        # 如果只有 channel_id，先尝试转成 username
        if self.config.wechat_channel_id and self.config.wechat_channel_id.startswith("sph"):
            try:
                logger.info(f"通过 channel_id 解析 username: {self.config.wechat_channel_id}")
                url = f"{self.WECHAT_BASE}/wechat_channels/v2/fetch_channel_id_to_username"
                resp = self.session.post(url, json={"channel_id": self.config.wechat_channel_id}, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                # 在响应中搜索 v2_...@finder 格式的 username
                username = self._extract_username_from_nested(data)
                if username:
                    logger.info(f"解析到 username: {username[:30]}...")
                    return username

                logger.warning(f"channel_id 响应中未找到 username: {json.dumps(data, ensure_ascii=False)[:300]}")
            except Exception as e:
                logger.warning(f"通过 channel_id 解析 username 失败: {e}")

        # 通过视频分享链接解析（兜底）
        share_url = self._build_share_url()
        if share_url:
            try:
                logger.info(f"通过 share_url 解析 username: {share_url}")
                url = f"{self.WECHAT_BASE}/wechat_channels/v2/fetch_video_detail"
                resp = self.session.post(url, json={"share_url": share_url, "raw": False}, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                username = data.get("data", {}).get("username", "")
                if username:
                    logger.info(f"解析到 username: {username[:30]}...")
                    return username
            except Exception as e:
                logger.warning(f"通过 share_url 解析 username 失败: {e}")

        logger.error("无法解析微信视频号 username，请在 config.yaml 中配置 wechat_channels.username")
        return None

    def _build_share_url(self) -> str:
        """根据 channel_id 构造分享链接"""
        cid = self.config.wechat_channel_id
        if cid and cid.startswith("sph"):
            short_id = cid[3:]  # 去掉 sph 前缀
            return f"https://weixin.qq.com/sph/{short_id}"
        return ""

    def _extract_username_from_nested(self, data: dict) -> str:
        """在嵌套字典中递归查找 v2_...@finder 格式的 username"""
        def search(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and v.startswith("v2_") and "@finder" in v:
                        return v
                    result = search(v)
                    if result:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = search(item)
                    if result:
                        return result
            return None
        return search(data) or ""

    def get_latest_videos(self) -> list:
        """获取视频号最新视频列表，支持分页"""
        username = self.resolve_username()
        if not username:
            return []

        url = f"{self.WECHAT_BASE}/wechat_channels/v2/fetch_user_videos"
        all_videos = []
        last_buffer = ""
        max_pages = 5

        for page in range(1, max_pages + 1):
            try:
                logger.info(f"获取视频号视频列表 (第{page}页)")
                payload = {"username": username, "raw": False}
                if last_buffer:
                    payload["last_buffer"] = last_buffer

                resp = self.session.post(url, json=payload, timeout=60)
                resp.raise_for_status()
                data = resp.json().get("data", {})

                videos = data.get("videos", [])
                if not videos:
                    logger.warning("视频号响应中无视频数据")
                    break

                logger.info(f"第{page}页获取到 {len(videos)} 条视频")
                all_videos.extend(videos)

                last_buffer = data.get("last_buffer", "")
                up_continue = data.get("up_continue", 0)
                if not last_buffer or not up_continue:
                    break

                # 如果已经获取到足够覆盖 lookback 的视频，可以提前停止
                # 按时间排序后最后一条的时间如果早于 cutoff，则不需要继续翻页
                oldest_in_page = min(v.get("create_time", 0) for v in videos)
                cutoff_ts = int(time.time()) - self.config.lookback_hours * 3600
                if oldest_in_page and oldest_in_page < cutoff_ts and len(all_videos) >= self.config.max_videos:
                    logger.info("已获取足够覆盖时间窗口的视频，停止翻页")
                    break

            except Exception as e:
                logger.error(f"获取视频号视频列表失败: {e}")
                break

        return self._parse_videos(all_videos)

    def _parse_videos(self, raw_videos: list) -> list:
        """解析视频号视频数据，统一格式"""
        parsed = []
        share_url_base = self._build_share_url()

        for v in raw_videos:
            try:
                video_id = str(v.get("id") or "")
                if not video_id:
                    continue

                # 标题
                title_items = v.get("title") or []
                title = ""
                if isinstance(title_items, list) and title_items and isinstance(title_items[0], dict):
                    title = title_items[0].get("shortTitle") or ""
                title = title.strip() or "无标题"

                create_time = v.get("create_time") or 0
                media = v.get("media", {})
                duration = media.get("duration") or 0
                media_url = media.get("full_url") or media.get("url") or ""

                parsed.append({
                    "id": video_id,
                    "title": title,
                    "create_time": int(create_time) if create_time else 0,
                    "duration_sec": int(duration),
                    "share_url": share_url_base,
                    "media_url": media_url,
                    "digg_count": v.get("like_count", 0),
                    "comment_count": v.get("comment_count", 0),
                    "collect_count": v.get("fav_count", 0),
                    "platform": "wechat",
                    "source_nickname": v.get("nickname") or self.config.wechat_nickname,
                })
            except Exception as e:
                logger.warning(f"解析视频号视频数据失败: {e}")
                continue

        # 按创建时间降序排列
        parsed.sort(key=lambda x: x["create_time"], reverse=True)
        return parsed

    def filter_new_videos(self, videos: list, state: StateManager) -> list:
        """筛选出新的、未处理的视频"""
        return filter_new_videos(videos, self.config, state.is_processed_wechat)


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

        # 用于从 TikHub 获取直链的会话
        self._tikhub_session = None
        if config.tikhub_api_key and config.tikhub_base_url:
            self._tikhub_session = requests.Session()
            self._tikhub_session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Authorization": f"Bearer {config.tikhub_api_key}",
            })

    def _extract_audio_url(self, aweme: dict) -> Optional[str]:
        """从 TikHub 返回的 aweme_detail 中提取可用音频/视频 URL"""
        # 优先取独立音频流（无视频画面，更小）
        bit_rate_audio = aweme.get("video", {}).get("bit_rate_audio") or []
        if bit_rate_audio:
            url_list = bit_rate_audio[0].get("audio_meta", {}).get("url_list", {})
            for key in ("main_url", "backup_url", "fallback_url"):
                url = url_list.get(key)
                if url:
                    return url

        # 回退到视频播放地址，再让 ffmpeg 提取音频
        play_addr = aweme.get("video", {}).get("play_addr", {})
        for url in play_addr.get("url_list", []):
            if url:
                return url

        return None

    def _get_audio_url_from_tikhub(self, video_id: str) -> Optional[str]:
        """通过 TikHub fetch_one_video 获取无 Cookie 音频直链（主域名失败则回退）"""
        if not self._tikhub_session:
            return None

        bases = [self.config.tikhub_base_url]
        fallback_base = self.config.tikhub_base_url.replace("api.tikhub.io", "api.tikhub.dev") if "api.tikhub.io" in self.config.tikhub_base_url \
            else self.config.tikhub_base_url.replace("api.tikhub.dev", "api.tikhub.io")
        if fallback_base != self.config.tikhub_base_url:
            bases.append(fallback_base)

        for base in bases:
            try:
                endpoint = f"{base}/douyin/web/fetch_one_video"
                resp = self._tikhub_session.get(endpoint, params={"aweme_id": video_id}, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                aweme = data.get("data", {}).get("aweme_detail", {})
                url = self._extract_audio_url(aweme)
                if url:
                    return url
            except Exception as e:
                logger.warning(f"TikHub {base} 获取音频直链失败: {e}")
                continue

        return None

    def _download_audio_direct(self, audio_url: str, output_path: Path) -> bool:
        """使用 requests 直接下载音频/视频文件"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.douyin.com/",
            }
            resp = requests.get(audio_url, headers=headers, timeout=120, stream=True)
            resp.raise_for_status()

            raw_path = output_path.with_suffix('.mp4')
            with open(raw_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            # 转换为 mp3
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_path), "-vn", "-codec:a", "libmp3lame", "-qscale:a", "5", str(output_path)],
                capture_output=True, timeout=120
            )
            raw_path.unlink(missing_ok=True)

            if output_path.exists() and output_path.stat().st_size > 1024:
                return True
            return False
        except Exception as e:
            logger.error(f"直接下载音频失败: {e}")
            return False

    def download_audio(self, video_url: str, video_id: str) -> Optional[str]:
        """下载抖音视频音频：优先 TikHub 直链，失败再试 yt-dlp"""
        audio_path = self.temp_dir / f"{video_id}.mp3"

        if audio_path.exists():
            logger.info(f"音频文件已存在: {audio_path}")
            return str(audio_path)

        # 方案1：TikHub 直链（无需 Cookie，成功率更高）
        logger.info(f"尝试从 TikHub 获取音频直链 (video_id={video_id})")
        audio_url = self._get_audio_url_from_tikhub(video_id)
        if audio_url:
            logger.info(f"获取到直链，开始下载: {audio_url[:80]}...")
            if self._download_audio_direct(audio_url, audio_path):
                logger.info(f"音频下载完成: {audio_path}")
                return str(audio_path)
            logger.warning("TikHub 直链下载失败，尝试 yt-dlp")
        else:
            logger.warning("未获取到 TikHub 直链，尝试 yt-dlp")

        # 方案2：yt-dlp 回退
        logger.info(f"使用 yt-dlp 下载: {video_url}")
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

    def download_audio_from_video_url(self, video_url: str, video_id: str) -> Optional[str]:
        """下载微信视频号视频并提取音频（视频号返回的是完整视频 URL）"""
        audio_path = self.temp_dir / f"{video_id}.mp3"
        video_path = self.temp_dir / f"{video_id}.mp4"

        if audio_path.exists():
            logger.info(f"音频文件已存在: {audio_path}")
            return str(audio_path)

        if not video_url:
            logger.error("视频号 media_url 为空，无法下载")
            return None

        try:
            logger.info(f"下载视频号视频: {video_url[:80]}...")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://channels.weixin.qq.com/",
                "Accept": "*/*",
            }
            resp = requests.get(video_url, headers=headers, timeout=180, stream=True)
            resp.raise_for_status()

            total_size = 0
            with open(video_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total_size += len(chunk)

            logger.info(f"视频下载完成: {video_path} ({total_size / 1024 / 1024:.2f} MB)")

            # 使用 ffmpeg 提取音频
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-codec:a", "libmp3lame", "-qscale:a", "5", str(audio_path)],
                capture_output=True, timeout=180
            )

            # 删除视频文件，保留音频
            video_path.unlink(missing_ok=True)

            if audio_path.exists() and audio_path.stat().st_size > 1024:
                logger.info(f"音频提取完成: {audio_path}")
                return str(audio_path)

            logger.error("音频提取失败或文件为空")
            return None

        except subprocess.TimeoutExpired:
            logger.error("视频下载/提取超时")
            video_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            logger.error(f"下载视频号视频失败: {e}")
            video_path.unlink(missing_ok=True)
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

            prompt = f"""你是专业的股市内容分析助手。请将以下抖音/微信视频号视频的转录文本总结为结构化摘要。

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
        """清理临时音频/视频文件"""
        for ext in ['.mp3', '.mp4', '.m4a', '.webm', '.opus']:
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
def format_message(main_nickname: str, summaries: list) -> tuple:
    """将多条视频总结格式化为一条推送消息"""
    now_bjt = datetime.now(BJT)
    date_str = now_bjt.strftime('%Y年%m月%d日')

    douyin_count = sum(1 for s in summaries if s["video"].get("platform") == "douyin")
    wechat_count = sum(1 for s in summaries if s["video"].get("platform") == "wechat")

    title = f"{main_nickname} 今日股市观点 ({date_str})"

    parts = [
        f"## 🎬 {main_nickname} 今日股市观点",
        f"",
        f"> 📅 {date_str} | 抖音 {douyin_count} 条 | 视频号 {wechat_count} 条",
        f"> 🤖 AI自动总结，仅供参考",
        f"",
        f"---",
        f""
    ]

    for i, item in enumerate(summaries, 1):
        v = item["video"]
        summary = item["summary"]

        platform = v.get("platform", "douyin")
        platform_icon = "📱" if platform == "douyin" else "💬"
        platform_name = "抖音" if platform == "douyin" else "视频号"
        source_name = v.get("source_nickname", main_nickname)

        # 格式化时长
        duration_min = int(v.get("duration_sec", 0) // 60)
        duration_sec = int(v.get("duration_sec", 0) % 60)
        duration_str = f"{duration_min}分{duration_sec}秒" if duration_min > 0 else f"{duration_sec}秒"

        # 格式化互动数据
        likes = v.get("digg_count", 0)
        comments = v.get("comment_count", 0)

        parts.extend([
            f"### {platform_icon} 视频{i}（{platform_name} · {source_name}）：{v['title'][:50]}",
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
# 单条视频处理
# ============================================================
def process_single_video(video: dict, processor: VideoProcessor, config: Config, is_wechat: bool = False) -> Optional[dict]:
    """处理单个视频：下载 -> 转录 -> 总结"""
    logger.info(f"\n处理视频: {video['title'][:50]} (ID: {video['id']})")

    # 下载音频
    if is_wechat:
        audio_path = processor.download_audio_from_video_url(video.get("media_url", ""), video["id"])
    else:
        audio_path = processor.download_audio(video["share_url"], video["id"])

    # 语音转文字
    transcript = None
    if audio_path:
        transcript = processor.transcribe(audio_path)

    # 如果标题为空/无标题，尝试用转录文本前30字作为标题
    if transcript and video.get("title") in ("", "无标题", "视频号更新"):
        title_from_text = transcript.strip().replace("\n", " ")[:40]
        if title_from_text:
            video["title"] = title_from_text
            logger.info(f"自动生成标题: {title_from_text}")

    # AI总结
    summary = None
    if transcript:
        summary = processor.summarize(video["title"], transcript, video.get("title", ""))
    else:
        logger.warning("转录失败，使用视频标题生成简要摘要")
        if config.llm_api_key:
            fallback = f"**核心观点**\n视频转录失败，无法获取详细内容。\n\n**视频标题**\n{video['title']}"
            summary = fallback
        else:
            summary = f"视频转录失败。标题: {video['title']}"

    # 清理临时文件
    processor.cleanup(video["id"])

    if summary:
        return {"video": video, "summary": summary}
    return None


# ============================================================
# 主流程
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("抖音/微信视频号账号视频自动总结 - 开始运行")
    logger.info("=" * 60)

    # 1. 加载配置
    config = Config()
    logger.info(f"抖音监控: {config.douyin_nickname} (抖音号: {config.douyin_unique_id})")
    if config.wechat_enabled:
        logger.info(f"视频号监控: {config.wechat_nickname} (channel_id: {config.wechat_channel_id})")
    else:
        logger.info("微信视频号监控: 已禁用")

    # 2. 初始化组件
    state = StateManager(config.state_file)
    processor = VideoProcessor(config)
    notifier = PushNotifier(config)

    all_summaries = []

    # 3. 处理抖音视频
    logger.info("\n" + "-" * 60)
    logger.info("【抖音】开始获取最新视频")
    logger.info("-" * 60)
    douyin_monitor = DouyinMonitor(config)
    douyin_videos = douyin_monitor.get_latest_videos()
    if douyin_videos:
        douyin_new = douyin_monitor.filter_new_videos(douyin_videos, state)
        for video in douyin_new:
            result = process_single_video(video, processor, config, is_wechat=False)
            if result:
                all_summaries.append(result)
            state.mark_processed(video["id"], video["create_time"])
            time.sleep(2)
    else:
        logger.warning("未获取到抖音视频")

    # 4. 处理微信视频号视频
    if config.wechat_enabled:
        logger.info("\n" + "-" * 60)
        logger.info("【微信视频号】开始获取最新视频")
        logger.info("-" * 60)
        wechat_monitor = WeChatChannelsMonitor(config)
        wechat_videos = wechat_monitor.get_latest_videos()
        if wechat_videos:
            wechat_new = wechat_monitor.filter_new_videos(wechat_videos, state)
            for video in wechat_new:
                result = process_single_video(video, processor, config, is_wechat=True)
                if result:
                    all_summaries.append(result)
                state.mark_processed_wechat(video["id"], video["create_time"])
                time.sleep(2)
        else:
            logger.warning("未获取到视频号视频")

    # 5. 按创建时间排序（合并后保持时间顺序）
    all_summaries.sort(key=lambda x: x["video"]["create_time"], reverse=True)

    # 6. 推送通知
    if all_summaries:
        title, content = format_message(config.douyin_nickname, all_summaries)
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
