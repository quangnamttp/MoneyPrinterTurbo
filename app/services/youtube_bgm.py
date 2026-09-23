"""
从 YouTube 链接下载音频，裁剪后作为背景音乐使用。

用于背景音乐"在线推荐"标签页的第三个来源：用户粘贴 YouTube 链接，先展示
标题/时长/版权声明供参考，用户选定起始秒数后下载并裁剪，复用现有的自定义
背景音乐存储与安全校验（app.services.bgm）。

重要：YouTube 的"许可"字段是上传者自行声明的，不代表 Content ID 系统对
该音频的真实版权判定，只能作为参考，不是安全保证——这一点必须在 UI 上
向用户说明清楚，不能让用户误以为标了"知识共享"就绝对不会被判定侵权。
"""

import os
import subprocess
import tempfile
from io import BytesIO

from loguru import logger

from app.services import bgm as bgm_service

DOWNLOAD_TIMEOUT_SECONDS = 120
MAX_DURATION_SECONDS = 60 * 20  # 20 分钟以内，避免误粘贴长视频占满存储/超时


class YoutubeBgmError(RuntimeError):
    """表示解析、下载或裁剪 YouTube 音频失败。"""


class YoutubeTrackInfo:
    """一条 YouTube 视频的必要元数据，只保留 UI 展示和下载需要的字段。"""

    def __init__(
        self,
        *,
        video_id: str,
        title: str,
        uploader: str,
        duration: int,
        license_name: str | None,
        thumbnail: str = "",
        preview_stream_url: str = "",
    ):
        self.video_id = video_id
        self.title = title or "Untitled"
        self.uploader = uploader or ""
        self.duration = max(0, int(duration or 0))
        self.license_name = (license_name or "").strip()
        self.thumbnail = thumbnail or ""
        # 只给浏览器 <audio> 标签试听用，几小时后会过期，不能存起来长期用。
        self.preview_stream_url = preview_stream_url or ""

    @property
    def license_status(self) -> str:
        """
        粗略分三档：creative_commons / standard / unknown。

        只是把 YouTube/上传者自行声明的信息转成标签，不代表 Content ID
        的真实判定结果——调用方展示时必须同时给出"仅供参考"的提示。
        """
        name = self.license_name.lower()
        if "creative commons" in name:
            return "creative_commons"
        if name:
            return "standard"
        return "unknown"


def _get_ydl_module():
    try:
        import yt_dlp
    except ImportError as exc:
        raise YoutubeBgmError(
            "yt-dlp is not installed; rebuild the Docker image to pick up requirements.txt"
        ) from exc
    return yt_dlp


def _pick_preview_stream_url(info: dict) -> str:
    """
    从 yt_dlp 的 formats 列表里挑一个纯音频直链，给 <audio> 标签试听用。

    优先选 acodec 有效、vcodec 为 none（纯音频）的格式，按比特率取最高的一个；
    找不到时退回顶层的 info["url"]（可能是视频+音频合并流，仍然能播放）。
    这个直链有 IP/时效限制，只用于当次页面的试听，不写入任何持久化配置。
    """
    formats = info.get("formats") or []
    audio_only = [
        f
        for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none") and f.get("url")
    ]
    if audio_only:
        best = max(audio_only, key=lambda f: f.get("abr") or 0)
        return str(best.get("url") or "")
    return str(info.get("url") or "")


def fetch_info(url: str) -> YoutubeTrackInfo:
    """只拉取元数据（标题/时长/许可声明），不下载音频。"""
    url = (url or "").strip()
    if not url:
        raise YoutubeBgmError("YouTube URL is required")

    yt_dlp = _get_ydl_module()
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt_dlp raises its own broad DownloadError subclasses
        raise YoutubeBgmError(f"failed to read YouTube video info: {exc}") from exc

    if not info:
        raise YoutubeBgmError("YouTube returned no video info")

    duration = int(info.get("duration") or 0)
    if duration > MAX_DURATION_SECONDS:
        raise YoutubeBgmError(
            f"video is too long ({duration}s); please use a link under "
            f"{MAX_DURATION_SECONDS // 60} minutes"
        )

    return YoutubeTrackInfo(
        video_id=str(info.get("id") or ""),
        title=str(info.get("title") or ""),
        uploader=str(info.get("uploader") or ""),
        duration=duration,
        license_name=info.get("license"),
        thumbnail=str(info.get("thumbnail") or ""),
        preview_stream_url=_pick_preview_stream_url(info),
    )


def download_and_trim_as_bgm(
    url: str,
    *,
    start_seconds: float = 0.0,
    trim_duration_seconds: float | None = None,
) -> str:
    """
    下载 YouTube 音频，按用户选择的起始秒数（和可选时长）裁剪，存为背景音乐。

    裁剪只是为了让用户能跳过前奏、选到自己想要的段落；裁剪后的文件长度不需要
    刚好等于视频时长，渲染阶段的 AudioLoop 会按最终视频长度自动循环/截断。
    """
    url = (url or "").strip()
    if not url:
        raise YoutubeBgmError("YouTube URL is required")
    start_seconds = max(0.0, float(start_seconds or 0))

    yt_dlp = _get_ydl_module()

    with tempfile.TemporaryDirectory(prefix="reelforge-yt-bgm-") as tmp_dir:
        raw_template = os.path.join(tmp_dir, "source.%(ext)s")
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": raw_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as exc:
            raise YoutubeBgmError(f"failed to download YouTube audio: {exc}") from exc

        video_id = str((info or {}).get("id") or "track")
        source_path = os.path.join(tmp_dir, "source.mp3")
        if not os.path.isfile(source_path):
            raise YoutubeBgmError("downloaded audio file was not found after extraction")

        trimmed_path = os.path.join(tmp_dir, "trimmed.mp3")
        ffmpeg_cmd = ["ffmpeg", "-y", "-ss", str(start_seconds), "-i", source_path]
        if trim_duration_seconds and trim_duration_seconds > 0:
            ffmpeg_cmd += ["-t", str(trim_duration_seconds)]
        ffmpeg_cmd += ["-acodec", "copy", trimmed_path]

        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise YoutubeBgmError("trimming the downloaded audio timed out") from exc

        if result.returncode != 0 or not os.path.isfile(trimmed_path):
            stderr = result.stderr.decode("utf-8", errors="ignore")[-500:]
            raise YoutubeBgmError(f"failed to trim downloaded audio: {stderr}")

        with open(trimmed_path, "rb") as f:
            trimmed_bytes = f.read()

    filename = f"youtube-{video_id}.mp3"
    try:
        stored_name = bgm_service.save_bgm_upload(filename, BytesIO(trimmed_bytes))
    except (bgm_service.BgmUploadError, bgm_service.BgmServiceError) as exc:
        raise YoutubeBgmError(str(exc)) from exc

    logger.info(
        f"YouTube audio downloaded and trimmed as background music: "
        f"video_id={video_id}, start={start_seconds}s, stored_name={stored_name}"
    )
    return stored_name
