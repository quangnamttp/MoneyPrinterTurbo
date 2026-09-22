"""
Jamendo 在线曲库搜索与下载。

用于背景音乐设置的"在线推荐"标签页：按视频主题/关键词搜索 Jamendo 曲库，
选中后下载并复用现有的自定义背景音乐存储与安全校验（app.services.bgm），
不需要额外维护一套文件写入逻辑。Jamendo 需要免费申请的 client_id，
参见 https://developer.jamendo.com。
"""

import os
from io import BytesIO

import requests
from loguru import logger

from app.config import config
from app.services import bgm as bgm_service

DEFAULT_BASE_URL = "https://api.jamendo.com/v3.0"
SEARCH_PATH = "/tracks"
SEARCH_TIMEOUT = (10, 30)
DOWNLOAD_TIMEOUT = (10, 60)
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50


class JamendoError(RuntimeError):
    """表示 Jamendo 请求、响应协议或下载失败。"""


def get_client_id() -> str:
    """优先读取 WebUI 保存的配置，未配置时允许使用环境变量。"""
    configured = str(config.app.get("jamendo_client_id", "") or "").strip()
    return configured or os.getenv("JAMENDO_CLIENT_ID", "").strip()


def is_enabled() -> bool:
    return bool(get_client_id())


class JamendoTrack:
    """一条 Jamendo 搜索结果，只保留 UI 展示和下载需要的字段。"""

    def __init__(
        self,
        *,
        track_id: str,
        name: str,
        artist_name: str,
        duration: int,
        audio_url: str,
        license_ccurl: str,
        image: str = "",
    ):
        self.track_id = track_id
        self.name = name or "Untitled"
        self.artist_name = artist_name or ""
        self.duration = duration
        self.audio_url = audio_url
        self.license_ccurl = license_ccurl or ""
        self.image = image or ""

    @property
    def is_royalty_free(self) -> bool:
        """
        粗略区分"可免费商用"和"需要额外授权才能商用"。

        Jamendo 曲目都遵循知识共享许可，但 NC（非商业）或 ND（禁止演绎）条款
        意味着用于商业/付费视频前仍需要通过 Jamendo Licensing 额外授权。
        不带这两个限制的许可（如 CC BY、CC BY-SA）默认允许商业用途。
        """
        license_lower = self.license_ccurl.lower()
        return "-nc" not in license_lower and "-nd" not in license_lower


def search_tracks(query: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> list[JamendoTrack]:
    """按关键词搜索 Jamendo 曲库，返回轻量曲目列表。"""
    client_id = get_client_id()
    if not client_id:
        raise JamendoError("Jamendo client ID is required")

    query = (query or "").strip()
    if not query:
        return []

    try:
        response = requests.get(
            f"{DEFAULT_BASE_URL}{SEARCH_PATH}",
            params={
                "client_id": client_id,
                "format": "json",
                "limit": max(1, min(limit, MAX_SEARCH_LIMIT)),
                "search": query,
                "audioformat": "mp32",
                "include": "musicinfo",
            },
            timeout=SEARCH_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise JamendoError(f"failed to connect to Jamendo: {exc}") from exc

    if not response.ok:
        raise JamendoError(f"Jamendo search failed: HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise JamendoError("Jamendo returned an invalid response") from exc

    tracks = []
    for item in payload.get("results") or []:
        audio_url = str(item.get("audio") or "").strip()
        if not audio_url:
            # 部分曲目不提供可直接下载的音频地址，搜索结果里直接跳过。
            continue
        try:
            duration = int(item.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        tracks.append(
            JamendoTrack(
                track_id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                artist_name=str(item.get("artist_name") or ""),
                duration=duration,
                audio_url=audio_url,
                license_ccurl=str(item.get("license_ccurl") or ""),
                image=str(item.get("image") or ""),
            )
        )
    return tracks


def download_track_as_bgm(track: JamendoTrack) -> str:
    """
    下载所选曲目并保存为背景音乐文件。

    复用 bgm_service.save_bgm_upload 的分块校验和原子落盘逻辑，返回值可以
    直接赋给 params.bgm_file，后续渲染流程按"自定义背景音乐"同样处理，
    不需要专门的 Jamendo 播放分支。
    """
    try:
        response = requests.get(track.audio_url, timeout=DOWNLOAD_TIMEOUT)
    except requests.RequestException as exc:
        raise JamendoError(f"failed to download Jamendo track: {exc}") from exc
    if not response.ok:
        raise JamendoError(
            f"failed to download Jamendo track: HTTP {response.status_code}"
        )

    filename = f"jamendo-{track.track_id or 'track'}.mp3"
    try:
        stored_name = bgm_service.save_bgm_upload(filename, BytesIO(response.content))
    except (bgm_service.BgmUploadError, bgm_service.BgmServiceError) as exc:
        raise JamendoError(str(exc)) from exc
    logger.info(
        f"Jamendo track downloaded as background music: "
        f"track_id={track.track_id}, stored_name={stored_name}"
    )
    return stored_name
