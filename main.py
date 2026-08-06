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
import shutil
import struct
import logging
import requests
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Tuple

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
        douyin_env_enabled = os.environ.get('DOUYIN_ENABLED', '').lower()
        self.douyin_enabled = (douyin_env_enabled == 'true') or \
            (douyin_env_enabled == '' and file_config.get('douyin', {}).get('enabled', False))
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

    def _save_debug_response(self, data, tag: str = ""):
        """将 API 响应数据保存到文件，便于离线分析"""
        try:
            debug_path = Path(__file__).parent / "debug_wechat_response.json"
            debug_entry = {
                "tag": tag,
                "timestamp": datetime.now(BJT).isoformat(),
                "data": data
            }
            with open(debug_path, 'w', encoding='utf-8') as f:
                json.dump(debug_entry, f, ensure_ascii=False, indent=2, default=str)
            logger.info(f"调试响应已保存: {debug_path} (tag={tag})")
        except Exception as e:
            logger.warning(f"保存调试响应失败: {e}")

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

                # 第一页时保存调试信息：完整响应结构
                if page == 1 and videos:
                    self._save_debug_response(videos[0], "fetch_user_videos_first_video")

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
        username = self.config.wechat_username

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
                # 尝试多个 URL 字段
                media_url = media.get("full_url") or media.get("url") or media.get("mp4_url") or ""
                # 直接从 fetch_user_videos 响应中提取 decode_key（避免后续再调 fetch_video_detail）
                decode_key = str(media.get("decode_key") or "")

                # 记录 media 对象的可用字段（调试用）
                if media_url and "encfilekey" in media_url.lower():
                    media_keys = list(media.keys()) if isinstance(media, dict) else "N/A"
                    logger.info(f"视频 {video_id} media字段: {media_keys}, decode_key={decode_key}")

                parsed.append({
                    "id": video_id,
                    "title": title,
                    "create_time": int(create_time) if create_time else 0,
                    "duration_sec": int(duration),
                    "share_url": share_url_base,
                    "media_url": media_url,
                    "decode_key": decode_key,
                    "username": username,
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
# Isaac64 PRNG（微信视频号视频解密用）
# ============================================================
class Isaac64:
    """
    Isaac64 伪随机数生成器，用于微信视频号视频解密。
    算法移植自 https://github.com/ltaoo/wx_channels_download/blob/main/pkg/decrypt/decrypt.go
    """

    MASK64 = 0xFFFFFFFFFFFFFFFF
    GOLDEN = 0x9e3779b97f4a7c13
    ENCRYPT_LEN = 131072  # 128 KB，仅前 128KB 被加密

    def __init__(self, seed: int):
        self.rand_cnt = 255
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.seed = [0] * 256
        self.mm = [0] * 256
        self._init(seed)

    def _mix(self, vals):
        """混合函数，vals 是 [a,b,c,d,e,f,g,h] 列表，原地修改"""
        a, b, c, d, e, f, g, h = vals
        a = (a - e) & self.MASK64
        f ^= (h >> 9)
        h = (h + a) & self.MASK64
        b = (b - f) & self.MASK64
        g ^= (a << 9) & self.MASK64
        a = (a + b) & self.MASK64
        c = (c - g) & self.MASK64
        h ^= (b >> 23)
        b = (b + c) & self.MASK64
        d = (d - h) & self.MASK64
        a ^= (c << 15) & self.MASK64
        c = (c + d) & self.MASK64
        e = (e - a) & self.MASK64
        b ^= (d >> 14)
        d = (d + e) & self.MASK64
        f = (f - b) & self.MASK64
        c ^= (e << 20) & self.MASK64
        e = (e + f) & self.MASK64
        g = (g - c) & self.MASK64
        d ^= (f >> 17)
        f = (f + g) & self.MASK64
        h = (h - d) & self.MASK64
        e ^= (g << 14) & self.MASK64
        g = (g + h) & self.MASK64
        return [a, b, c, d, e, f, g, h]

    def _init(self, enc_key: int):
        """初始化 ISAAC64 状态"""
        enc_key &= self.MASK64
        g = self.GOLDEN
        vals = [g] * 8  # a=b=c=d=e=f=g=h=golden

        self.seed[0] = enc_key
        for i in range(1, 256):
            self.seed[i] = 0

        for _ in range(4):
            vals = self._mix(vals)

        # 第一遍：用 Seed 初始化 MM
        for i in range(0, 256, 8):
            vals[0] = (vals[0] + self.seed[i]) & self.MASK64
            vals[1] = (vals[1] + self.seed[i + 1]) & self.MASK64
            vals[2] = (vals[2] + self.seed[i + 2]) & self.MASK64
            vals[3] = (vals[3] + self.seed[i + 3]) & self.MASK64
            vals[4] = (vals[4] + self.seed[i + 4]) & self.MASK64
            vals[5] = (vals[5] + self.seed[i + 5]) & self.MASK64
            vals[6] = (vals[6] + self.seed[i + 6]) & self.MASK64
            vals[7] = (vals[7] + self.seed[i + 7]) & self.MASK64
            vals = self._mix(vals)
            self.mm[i] = vals[0]
            self.mm[i + 1] = vals[1]
            self.mm[i + 2] = vals[2]
            self.mm[i + 3] = vals[3]
            self.mm[i + 4] = vals[4]
            self.mm[i + 5] = vals[5]
            self.mm[i + 6] = vals[6]
            self.mm[i + 7] = vals[7]

        # 第二遍：用 MM 再次混合
        for i in range(0, 256, 8):
            vals[0] = (vals[0] + self.mm[i]) & self.MASK64
            vals[1] = (vals[1] + self.mm[i + 1]) & self.MASK64
            vals[2] = (vals[2] + self.mm[i + 2]) & self.MASK64
            vals[3] = (vals[3] + self.mm[i + 3]) & self.MASK64
            vals[4] = (vals[4] + self.mm[i + 4]) & self.MASK64
            vals[5] = (vals[5] + self.mm[i + 5]) & self.MASK64
            vals[6] = (vals[6] + self.mm[i + 6]) & self.MASK64
            vals[7] = (vals[7] + self.mm[i + 7]) & self.MASK64
            vals = self._mix(vals)
            self.mm[i] = vals[0]
            self.mm[i + 1] = vals[1]
            self.mm[i + 2] = vals[2]
            self.mm[i + 3] = vals[3]
            self.mm[i + 4] = vals[4]
            self.mm[i + 5] = vals[5]
            self.mm[i + 6] = vals[6]
            self.mm[i + 7] = vals[7]

        self._isaac64()

    def _isaac64(self):
        """生成一批 256 个随机数"""
        self.cc = (self.cc + 1) & self.MASK64
        self.bb = (self.bb + self.cc) & self.MASK64

        for i in range(256):
            remainder = i % 4
            if remainder == 0:
                self.aa = (self.aa ^ ((self.aa << 21) & self.MASK64)) ^ self.MASK64
            elif remainder == 1:
                self.aa ^= (self.aa >> 5)
            elif remainder == 2:
                self.aa ^= (self.aa << 12) & self.MASK64
            else:
                self.aa ^= (self.aa >> 33)

            self.aa = (self.aa + self.mm[(i + 128) % 256]) & self.MASK64
            x = self.mm[i]
            y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & self.MASK64
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & self.MASK64
            self.seed[i] = self.bb

    def random(self) -> int:
        """返回一个 64 位随机数"""
        result = self.seed[self.rand_cnt]
        if self.rand_cnt == 0:
            self._isaac64()
            self.rand_cnt = 255
        else:
            self.rand_cnt -= 1
        return result

    def generate_keystream(self, length: int = 131072) -> bytes:
        """生成指定长度的密钥流（BigEndian 字节序列）"""
        result = bytearray(length)
        offset = 0
        while offset < length:
            rand_val = self.random()
            chunk = struct.pack('>Q', rand_val)  # BigEndian uint64
            for b in chunk:
                if offset >= length:
                    break
                result[offset] = b
                offset += 1
        return bytes(result)


# ============================================================
# 视频处理器（下载 -> 解密 -> 转录 -> 总结）
# ============================================================
class VideoProcessor:
    """下载视频音频、语音转文字、AI总结"""

    # 微信视频号 V2 API 仅在 .io 域名稳定
    WECHAT_API_BASE = "https://api.tikhub.io/api/v1"

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

        # 微信视频号 API 会话（POST + JSON body）
        self._wechat_session = None
        if config.tikhub_api_key:
            self._wechat_session = requests.Session()
            self._wechat_session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Content-Type": "application/json",
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

    def _download_audio_direct(self, audio_url: str, output_path: Path) -> Optional[str]:
        """使用 requests 直接下载音频/视频文件，然后提取音频。返回音频文件路径，失败返回 None"""
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

            # 使用统一的多策略音频提取
            audio_result = self._run_ffmpeg_extract(raw_path, output_path)
            raw_path.unlink(missing_ok=True)

            return audio_result
        except Exception as e:
            logger.error(f"直接下载音频失败: {e}")
            return None

    def download_audio(self, video_url: str, video_id: str) -> Optional[str]:
        """下载抖音视频音频：优先 TikHub 直链，失败再试 yt-dlp"""
        audio_path = self.temp_dir / f"{video_id}.mp3"

        # 检查是否已有缓存的音频文件（可能是任意格式）
        for ext in ['.mp3', '.m4a', '.wav']:
            existing = self.temp_dir / f"{video_id}{ext}"
            if existing.exists():
                logger.info(f"音频文件已存在: {existing}")
                return str(existing)

        # 方案1：TikHub 直链（无需 Cookie，成功率更高）
        logger.info(f"尝试从 TikHub 获取音频直链 (video_id={video_id})")
        audio_url = self._get_audio_url_from_tikhub(video_id)
        if audio_url:
            logger.info(f"获取到直链，开始下载: {audio_url[:80]}...")
            direct_result = self._download_audio_direct(audio_url, audio_path)
            if direct_result:
                logger.info(f"音频下载完成: {direct_result}")
                return direct_result
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
            for ext in ['.mp3', '.m4a', '.webm', '.opus', '.wav']:
                actual = audio_path.with_suffix(ext)
                if actual.exists():
                    if ext != '.mp3':
                        # 转换为 mp3
                        converted = self._run_ffmpeg_extract(actual, audio_path)
                        actual.unlink()
                        if converted:
                            logger.info(f"音频下载完成: {converted}")
                            return converted
                        logger.warning(f"yt-dlp 文件 {ext} 转换失败，直接使用原文件")
                        return str(actual)
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

    def _run_ffmpeg_extract(self, video_path: Path, audio_path: Path) -> Optional[str]:
        """
        用 ffmpeg 从视频文件提取音频，尝试多种策略。
        返回成功生成的音频文件路径（可能是 .mp3/.m4a/.wav），失败返回 None。
        """
        # 策略列表：(输出后缀, ffmpeg参数, 说明)
        strategies = [
            (".mp3", ["-vn", "-acodec", "libmp3lame", "-qscale:a", "5"], "MP3 (libmp3lame)"),
            (".m4a", ["-vn", "-acodec", "aac", "-b:a", "128k"], "AAC (m4a)"),
            (".wav", ["-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"], "WAV 16kHz mono"),
        ]

        for ext, extra_args, desc in strategies:
            out_path = audio_path.with_suffix(ext)
            cmd = ["ffmpeg", "-y", "-i", str(video_path)] + extra_args + [str(out_path)]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=180
                )
                if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1024:
                    size_kb = out_path.stat().st_size / 1024
                    logger.info(f"音频提取成功 [{desc}]: {out_path} ({size_kb:.1f} KB)")
                    return str(out_path)

                # 记录失败详情
                err_tail = result.stderr[-500:] if result.stderr else "(无stderr)"
                logger.warning(f"音频提取失败 [{desc}] (rc={result.returncode}): {err_tail}")
            except subprocess.TimeoutExpired:
                logger.warning(f"音频提取超时 [{desc}]")
            except Exception as e:
                logger.warning(f"音频提取异常 [{desc}]: {e}")

        logger.error("所有音频提取策略均失败")
        return None

    def _probe_video(self, video_path: Path) -> bool:
        """用 ffprobe 检查视频文件是否包含音频流，返回 True 表示有音频"""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,codec_name", "-of", "json", str(video_path)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                logger.warning(f"ffprobe 失败: {result.stderr[:300]}")
                # ffprobe 失败不阻止后续尝试，ffmpeg 可能仍能处理
                return True

            probe_data = json.loads(result.stdout)
            streams = probe_data.get("streams", [])
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            logger.info(f"ffprobe 检测到流: {streams}")
            if not has_audio:
                logger.error("视频文件中没有音频流")
            return has_audio
        except Exception as e:
            logger.warning(f"ffprobe 异常: {e}")
            return True  # 不阻止后续尝试

    def _get_wechat_video_url_and_key(self, video_id: str, username: str = "",
                                       share_url: str = "") -> Optional[tuple]:
        """
        通过 TikHub fetch_video_detail 获取视频下载 URL 和 decode_key。
        微信视频号视频是加密的：前 128KB 被 XOR 加密，需要 decode_key 解密。

        参数优先级：object_id > share_url
        返回 (full_url, decode_key) 元组，失败返回 None。
        """
        if not self._wechat_session:
            return None

        # 按优先级构建请求参数
        payloads = []
        # 优先使用 object_id（即 video_id，来自 fetch_user_videos 的 id 字段）
        if video_id:
            payloads.append({"object_id": video_id, "raw": False})
        # 其次使用 share_url
        if share_url:
            payloads.append({"share_url": share_url, "raw": False})

        for payload in payloads:
            try:
                logger.info(f"调用 fetch_video_detail: params={list(payload.keys())}")
                url = f"{self.WECHAT_API_BASE}/wechat_channels/v2/fetch_video_detail"
                resp = self._wechat_session.post(url, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data", {})

                # raw=False 模式: data.media 是一个对象，包含 full_url 和 decode_key
                media = data.get("media", {})
                if isinstance(media, dict):
                    full_url = media.get("full_url") or ""
                    decode_key = media.get("decode_key") or ""

                    if full_url:
                        logger.info(f"fetch_video_detail 返回: full_url={full_url[:80]}..., decode_key={decode_key}")
                        return (full_url, decode_key)

                # 如果 raw=False 没找到，尝试从 raw=True 模式的嵌套结构中提取
                logger.warning("raw=False 模式未找到 media.full_url，尝试 raw=True")
                payload["raw"] = True
                resp = self._wechat_session.post(url, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data", {})

                # raw=True 模式: data.objects[0].objectDesc.media[0] 包含 url, urlToken, decodeKey
                objects = data.get("objects", [])
                if objects:
                    obj_desc = objects[0].get("objectDesc", {})
                    media_list = obj_desc.get("media", [])
                    if media_list and isinstance(media_list, list):
                        m = media_list[0]
                        raw_url = m.get("url", "")
                        url_token = m.get("urlToken", "")
                        decode_key = m.get("decodeKey", "")
                        full_url = raw_url + url_token if raw_url else ""
                        if full_url:
                            logger.info(f"raw=True 模式找到: url={raw_url[:60]}..., decode_key={decode_key}")
                            return (full_url, decode_key)

                logger.warning(f"fetch_video_detail 未返回有效的 URL+key (params={list(payload.keys())})")
            except Exception as e:
                logger.warning(f"fetch_video_detail 调用失败: {e}")

        return None

    def _decrypt_wechat_video(self, video_path: Path, decode_key: str) -> bool:
        """
        解密微信视频号加密视频。
        仅前 128KB 被 XOR 加密，使用 Isaac64 PRNG 生成密钥流。

        Args:
            video_path: 加密视频文件路径（原地解密）
            decode_key: 解密密钥（数字字符串，如 "2136343393"）

        Returns:
            True 表示解密成功，False 表示失败或无需解密
        """
        if not decode_key or decode_key == "0":
            logger.info("decode_key 为空或0，视频未加密，无需解密")
            return True

        try:
            key_int = int(decode_key)
        except (ValueError, TypeError):
            logger.error(f"decode_key 无法转换为整数: {decode_key}")
            return False

        if key_int == 0:
            logger.info("decode_key=0，视频未加密")
            return True

        try:
            # 读取加密视频文件
            with open(video_path, 'rb') as f:
                data = bytearray(f.read())

            enc_len = min(Isaac64.ENCRYPT_LEN, len(data))
            logger.info(f"开始解密视频: {video_path.name}, 加密长度={enc_len}, decode_key={decode_key}")

            # 生成密钥流并 XOR 解密
            isaac = Isaac64(key_int)
            keystream = isaac.generate_keystream(enc_len)

            for i in range(enc_len):
                data[i] ^= keystream[i]

            # 验证解密结果：检查 MP4 ftyp 签名
            if len(data) >= 8 and b'ftyp' in bytes(data[4:12]):
                logger.info("解密成功！检测到 MP4 ftyp 签名")
            else:
                logger.warning(f"解密后未检测到 ftyp 签名，前12字节: {bytes(data[:12]).hex()}")
                # 仍然写入，可能是其他格式

            # 写回解密后的文件
            with open(video_path, 'wb') as f:
                f.write(data)

            logger.info(f"视频解密完成: {video_path}")
            return True

        except Exception as e:
            logger.error(f"视频解密失败: {e}")
            return False

    def _download_video_file(self, video_url: str, video_path: Path) -> Optional[int]:
        """
        下载视频文件到指定路径，返回文件大小（字节），失败返回 None。
        尝试 HTTP→HTTPS 升级和 Range header。
        """
        urls_to_try = [video_url]
        # 尝试 HTTPS 升级
        if video_url.startswith("http://"):
            urls_to_try.append(video_url.replace("http://", "https://", 1))

        for url in urls_to_try:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://channels.weixin.qq.com/",
                    "Accept": "*/*",
                    "Range": "bytes=0-",  # 某些 CDN 需要 Range header 才能返回视频数据
                }
                resp = requests.get(url, headers=headers, timeout=180, stream=True)
                resp.raise_for_status()

                content_length = resp.headers.get("Content-Length")
                if content_length:
                    logger.info(f"预期视频大小: {int(content_length) / 1024 / 1024:.2f} MB")

                total_size = 0
                with open(video_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            total_size += len(chunk)

                if total_size > 10240:
                    logger.info(f"视频下载完成: {video_path} ({total_size / 1024 / 1024:.2f} MB)")
                    return total_size
                else:
                    logger.warning(f"下载文件过小 ({total_size} bytes)，尝试下一个URL")
                    video_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"下载失败 ({url[:60]}...): {e}")
                video_path.unlink(missing_ok=True)

        return None

    def download_audio_from_video_url(self, video_url: str, video_id: str,
                                       username: str = "", share_url: str = "",
                                       decode_key: str = "") -> Optional[str]:
        """
        下载微信视频号视频并提取音频。
        完整流程：
        1. 优先使用 fetch_user_videos 已返回的 media_url + decode_key（省一次 API 调用）
        2. 缺失时通过 fetch_video_detail 获取 full_url + decode_key
        3. 下载加密视频
        4. 用 decode_key 解密（Isaac64 XOR，前128KB）
        5. 提取音频
        6. 失败则直接传视频给 Whisper
        """
        audio_path = self.temp_dir / f"{video_id}.mp3"
        video_path = self.temp_dir / f"{video_id}.mp4"
        decrypted_path = self.temp_dir / f"{video_id}_dec.mp4"

        # 检查是否已有缓存的音频文件（可能是任意格式）
        for ext in ['.mp3', '.m4a', '.wav']:
            existing = self.temp_dir / f"{video_id}{ext}"
            if existing.exists():
                logger.info(f"音频文件已存在: {existing}")
                return str(existing)

        # 步骤1：确定下载URL和解密密钥
        # 优先使用 fetch_user_videos 已返回的 media_url + decode_key（省一次API调用）
        fetch_url = ""
        resolved_key = decode_key

        if video_url:
            fetch_url = video_url
            if decode_key:
                logger.info(f"使用 fetch_user_videos 返回的 media_url + decode_key (video_id={video_id}, key={decode_key})")
            else:
                logger.info(f"有 media_url 但无 decode_key，尝试 fetch_video_detail 补充 (video_id={video_id})")
        else:
            logger.info(f"无 media_url，通过 fetch_video_detail 获取 (video_id={video_id})")

        # 如果缺少 URL 或 decode_key，通过 fetch_video_detail 补充
        if not fetch_url or not resolved_key:
            url_and_key = self._get_wechat_video_url_and_key(video_id, username, share_url)
            if url_and_key:
                detail_url, detail_key = url_and_key
                if not fetch_url:
                    fetch_url = detail_url
                if not resolved_key:
                    resolved_key = detail_key
                logger.info(f"fetch_video_detail 补充: URL={fetch_url[:80]}..., decode_key={resolved_key}")
            elif not fetch_url:
                logger.error("无法获取视频URL，fetch_video_detail 和 media_url 均失败")
                return None

        if not fetch_url:
            logger.error("无可用视频URL")
            return None

        # 步骤2：下载视频
        file_size = self._download_video_file(fetch_url, video_path)
        if not file_size:
            logger.error("视频下载失败")
            return None

        # 步骤3：解密（如需要）
        use_path = video_path  # 默认使用原始下载文件
        if resolved_key and resolved_key != "0":
            logger.info(f"视频需要解密 (decode_key={resolved_key})")
            shutil.copy2(video_path, decrypted_path)
            if self._decrypt_wechat_video(decrypted_path, resolved_key):
                # 验证解密后的视频
                with open(decrypted_path, 'rb') as f:
                    magic = f.read(12)
                if b'ftyp' in magic:
                    logger.info("解密后视频验证成功 (MP4 ftyp)")
                    use_path = decrypted_path
                else:
                    logger.warning(f"解密后视频验证失败，前12字节: {magic[:12].hex()}")
                    # 仍然尝试使用解密后的文件
                    use_path = decrypted_path
            else:
                logger.error("视频解密失败，尝试使用原始文件")
        else:
            logger.info("视频无需解密 (decode_key 为空或0)")

        # 步骤4：提取音频
        self._probe_video(use_path)
        audio_result = self._run_ffmpeg_extract(use_path, audio_path)
        if audio_result:
            video_path.unlink(missing_ok=True)
            decrypted_path.unlink(missing_ok=True)
            return audio_result

        # 步骤5：音频提取失败，尝试直接传视频给 Whisper
        if use_path.exists() and use_path.stat().st_size > 10240:
            logger.warning("音频提取失败，使用视频文件直接转录")
            result_path = str(use_path)
            if use_path != video_path:
                video_path.unlink(missing_ok=True)
            return result_path

        # 清理临时文件
        video_path.unlink(missing_ok=True)
        decrypted_path.unlink(missing_ok=True)
        logger.error("微信视频号视频下载和解密全部失败")
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

            prompt = f"""你是专业的股市内容分析助手，分析对象是一位资深股市博主（"真如铁"/"笑傲江湖真如铁"）的视频。
该博主的所有视频都与股市相关，包括：正式的市场分析、技术解读，也包括以幽默段子、隐喻、暗号、生活类比等方式传达的操作信号和市场观点。

请将以下视频转录文本总结为结构化摘要，务必从股市视角解读内容。

请严格按照以下格式输出：

**核心观点**
{{用1-2句话概括作者对市场的总体看法。即使是段子或隐喻，也要提炼出背后的市场判断}}

**市场方向**
{{看多/看空/震荡，涉及哪些指数、板块或个股。如果作者通过幽默方式暗示方向，请明确指出}}

**关键信息**
{{用要点列出提到的重要数据、技术信号、政策解读、情绪暗示等。注意识别：
- 谐音梗、段子背后可能暗示的板块或个股
- 以生活类比、打电话等场景隐喻的市场操作信号
- 看似闲聊实则传达的市场情绪或择时判断
- 任何"抄底""逃顶""加仓""空仓"等操作暗示}}

**操作建议**
{{如果有具体的操作建议请列出，包括隐含在段子或比喻中的操作暗示。没有则写"无具体建议"}}

要求：
- 保持简洁，总字数控制在500字以内
- 如果转录文本较短或内容不清晰，结合标题和上下文尽力提取市场含义
- 所有内容都从股市角度解读，不要判定为"与股市无关"
- 对于幽默/段子类内容，重点分析其背后的市场信号和操作暗示

视频标题：{title}

转录文本：
{context}"""

            response = client.chat.completions.create(
                model=self.config.llm_model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=800,
                temperature=0.3
            )

            summary = response.choices[0].message.content.strip()
            logger.info(f"总结完成，长度: {len(summary)} 字符")
            return summary

        except Exception as e:
            logger.error(f"AI总结失败: {e}")
            return None

    def summarize_from_metadata(self, video: dict) -> Optional[str]:
        """转录为空时，根据视频元数据（标题、时长、互动数据）从股市角度生成分析"""
        if not self.config.llm_api_key:
            return None

        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)

            title = video.get("title", "无标题")
            duration = video.get("duration_sec", 0)
            likes = video.get("likes", 0)
            comments = video.get("comments", 0)

            prompt = f"""你是专业的股市内容分析助手，分析对象是一位资深股市博主（"真如铁"/"笑傲江湖真如铁"）的视频。
该博主的所有视频都与股市相关。这条视频的语音转录失败（可能视频极短或无语音），但请根据以下元数据从股市角度进行分析推断。

视频元数据：
- 标题：{title}
- 时长：{duration}秒
- 点赞数：{likes}
- 评论数：{comments}

请严格按照以下格式输出：

**核心观点**
{{根据标题和互动数据，推断作者可能传达的市场观点。即使是极短视频，标题也可能包含重要信号}}

**市场方向**
{{根据标题中的关键词推断市场方向（看多/看空/震荡），如标题含"抄底""加仓"等暗示方向}}

**关键信息**
{{分析标题中可能的市场信号、谐音梗、隐喻等。极短视频往往是博主快速传递某种市场情绪或操作信号}}

**操作建议**
{{根据推断给出可能的操作暗示。无法确定时写"需结合原视频确认"}}"""

            response = client.chat.completions.create(
                model=self.config.llm_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.3
            )

            summary = response.choices[0].message.content.strip()
            logger.info(f"元数据总结完成，长度: {len(summary)} 字符")
            return summary

        except Exception as e:
            logger.error(f"元数据总结失败: {e}")
            return None

    def cleanup(self, video_id: str):
        """清理临时音频/视频文件"""
        for ext in ['.mp3', '.mp4', '.m4a', '.wav', '.webm', '.opus']:
            f = self.temp_dir / f"{video_id}{ext}"
            if f.exists():
                f.unlink()
                logger.info(f"已清理临时文件: {f}")
        # 清理解密后的视频文件
        dec_f = self.temp_dir / f"{video_id}_dec.mp4"
        if dec_f.exists():
            dec_f.unlink()
            logger.info(f"已清理临时文件: {dec_f}")


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
def format_message(main_nickname: str, summaries: list,
                   douyin_enabled: bool = True, wechat_enabled: bool = True) -> tuple:
    """将多条视频总结格式化为一条推送消息"""
    now_bjt = datetime.now(BJT)
    date_str = now_bjt.strftime('%Y年%m月%d日')

    douyin_count = sum(1 for s in summaries if s["video"].get("platform") == "douyin")
    wechat_count = sum(1 for s in summaries if s["video"].get("platform") == "wechat")

    title = f"{main_nickname} 今日股市观点 ({date_str})"

    # 根据启用的平台生成统计行
    count_parts = []
    if douyin_enabled:
        count_parts.append(f"抖音 {douyin_count} 条")
    if wechat_enabled:
        count_parts.append(f"视频号 {wechat_count} 条")
    count_str = " | ".join(count_parts) if count_parts else "无视频"

    parts = [
        f"## 🎬 {main_nickname} 今日股市观点",
        f"",
        f"> 📅 {date_str} | {count_str}",
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
        audio_path = processor.download_audio_from_video_url(
            video.get("media_url", ""), video["id"],
            username=video.get("username", ""), share_url=video.get("share_url", ""),
            decode_key=video.get("decode_key", "")
        )
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
        logger.warning("转录为空，使用视频元数据生成股市视角分析")
        if config.llm_api_key:
            summary = processor.summarize_from_metadata(video)
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
    if config.douyin_enabled:
        logger.info(f"抖音监控: {config.douyin_nickname} (抖音号: {config.douyin_unique_id})")
    else:
        logger.info("抖音监控: 已禁用")
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
    if config.douyin_enabled:
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
    else:
        logger.info("抖音监控已禁用，跳过")

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
        # 根据启用的平台选择主昵称
        main_nickname = config.wechat_nickname if not config.douyin_enabled else config.douyin_nickname
        title, content = format_message(main_nickname, all_summaries, config.douyin_enabled, config.wechat_enabled)
        logger.info(f"准备推送: {title}")
        success = notifier.push(title, content)

        if success:
            logger.info("推送成功！")
        else:
            logger.error("推送失败，请检查推送配置")
            # 以非零退出码结束，使 GitHub Actions 步骤显示 failure
            sys.exit(1)

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
