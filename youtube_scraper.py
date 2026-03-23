"""
YouTube Scraper using yt-dlp
Extracts video metadata and captions/subtitles.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

import yt_dlp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class ScraperError(Exception):
    """Base exception for scraper errors."""


class VideoUnavailableError(ScraperError):
    """Video is private, deleted, or geo-blocked."""


class RateLimitError(ScraperError):
    """YouTube is rate-limiting requests (HTTP 429)."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class VideoMetadata:
    id: str
    title: str
    description: Optional[str]
    uploader: Optional[str]
    upload_date: Optional[str]
    duration: Optional[int]
    view_count: Optional[int]
    like_count: Optional[int]
    thumbnail: Optional[str]
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    webpage_url: Optional[str] = None
    captions: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class YouTubeScraper:
    """Scraper for YouTube video metadata and captions using yt-dlp."""

    DEFAULT_CAPTION_LANGS = ["de", "en"]
    # Delays between retries: 2s, 4s, 8s, 16s
    RETRY_DELAYS = [2, 4, 8, 16]

    def __init__(
        self,
        caption_langs: Optional[list[str]] = None,
        prefer_auto_captions: bool = True,
        cookies_file: Optional[str] = None,
        retries: int = 3,
    ):
        self.caption_langs = caption_langs or self.DEFAULT_CAPTION_LANGS
        self.prefer_auto_captions = prefer_auto_captions
        self.cookies_file = cookies_file
        self.retries = min(retries, len(self.RETRY_DELAYS))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ydl_opts(self) -> dict:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": self.prefer_auto_captions,
            "subtitleslangs": self.caption_langs,
            "subtitlesformat": "vtt",
            "quiet": True,
            "no_warnings": False,
            "extract_flat": False,
            # yt-dlp built-in retry for network errors
            "retries": self.retries,
            "fragment_retries": self.retries,
        }
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        return opts

    @staticmethod
    def _classify_ydl_error(exc: yt_dlp.utils.DownloadError) -> ScraperError:
        msg = str(exc).lower()
        if any(k in msg for k in ("private video", "this video is private")):
            return VideoUnavailableError(f"Video is private: {exc}")
        if any(k in msg for k in ("video unavailable", "has been removed", "not available")):
            return VideoUnavailableError(f"Video unavailable: {exc}")
        if "429" in msg or "too many requests" in msg:
            return RateLimitError(f"Rate limited by YouTube: {exc}")
        return ScraperError(str(exc))

    def _extract_info_with_retry(self, url: str, ydl_opts: dict) -> dict:
        """Call yt-dlp extract_info with exponential-backoff retry on transient errors."""
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(self.retries + 1):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info is None:
                    raise ValueError(f"No info returned for: {url}")
                return info
            except yt_dlp.utils.DownloadError as exc:
                classified = self._classify_ydl_error(exc)
                # Don't retry permanent errors
                if isinstance(classified, VideoUnavailableError):
                    raise classified from exc
                last_exc = classified
            except Exception as exc:
                last_exc = exc

            if attempt < self.retries:
                delay = self.RETRY_DELAYS[attempt]
                logger.warning(
                    "Attempt %d/%d failed for %s — retrying in %ds: %s",
                    attempt + 1, self.retries + 1, url, delay, last_exc,
                )
                time.sleep(delay)

        raise last_exc

    def _extract_captions_from_info(self, info: dict) -> dict[str, str]:
        """Extract caption text from the info dict subtitles/automatic_captions."""
        captions: dict[str, str] = {}
        PREFERRED_EXTS = ("vtt", "json3", "srv3", "srv2", "srv1", "ttml")

        for source_key in ("subtitles", "automatic_captions"):
            source = info.get(source_key) or {}
            for lang, formats in source.items():
                if lang in captions or not isinstance(formats, list):
                    continue
                for fmt in formats:
                    if fmt.get("ext") in PREFERRED_EXTS:
                        caption_url = fmt.get("url")
                        if caption_url:
                            text = self._fetch_caption_text(caption_url, fmt.get("ext", "vtt"))
                            if text:
                                captions[lang] = text
                                break

        return captions

    def _fetch_caption_text(self, url: str, ext: str) -> Optional[str]:
        """Download and parse caption content from a URL, with retry."""
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "yt-dlp"})
                with urllib.request.urlopen(req, timeout=15) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                if ext == "json3":
                    return self._parse_json3(raw)
                return raw
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    last_exc = RateLimitError(f"Rate limited fetching caption: {url}")
                elif exc.code in (403, 404, 410):
                    logger.debug("Caption not accessible (%d): %s", exc.code, url)
                    return None
                else:
                    last_exc = exc
            except Exception as exc:
                last_exc = exc

            if attempt < self.retries:
                delay = self.RETRY_DELAYS[attempt]
                logger.debug("Caption fetch attempt %d failed, retrying in %ds", attempt + 1, delay)
                time.sleep(delay)

        logger.warning("Could not fetch caption after %d attempts: %s", self.retries + 1, last_exc)
        return None

    @staticmethod
    def _parse_json3(raw: str) -> Optional[str]:
        try:
            data = json.loads(raw)
            lines = []
            for event in data.get("events", []):
                segs = event.get("segs", [])
                text = "".join(s.get("utf8", "") for s in segs).strip()
                if text:
                    lines.append(text)
            return "\n".join(lines) or None
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape(self, url: str) -> VideoMetadata:
        """Scrape metadata and captions for a single YouTube video URL."""
        info = self._extract_info_with_retry(url, self._build_ydl_opts())
        captions = self._extract_captions_from_info(info)

        return VideoMetadata(
            id=info.get("id", ""),
            title=info.get("title", ""),
            description=info.get("description"),
            uploader=info.get("uploader"),
            upload_date=info.get("upload_date"),
            duration=info.get("duration"),
            view_count=info.get("view_count"),
            like_count=info.get("like_count"),
            thumbnail=info.get("thumbnail"),
            tags=info.get("tags") or [],
            categories=info.get("categories") or [],
            webpage_url=info.get("webpage_url"),
            captions=captions,
        )

    def scrape_playlist(self, url: str, on_progress=None) -> list[VideoMetadata]:
        """Scrape all videos in a YouTube playlist.

        Args:
            url: Playlist URL.
            on_progress: Optional callback(current, total, video_url) called per video.
        """
        flat_opts = {**self._build_ydl_opts(), "extract_flat": "in_playlist"}
        playlist_info = self._extract_info_with_retry(url, flat_opts)

        entries = playlist_info.get("entries") or []
        total = len(entries)
        results: list[VideoMetadata] = []
        errors: list[dict] = []

        for idx, entry in enumerate(entries, start=1):
            video_url = entry.get("url") or entry.get("webpage_url")
            if not video_url:
                continue

            if on_progress:
                on_progress(idx, total, video_url)
            else:
                print(f"[{idx}/{total}] {video_url}")

            try:
                results.append(self.scrape(video_url))
            except VideoUnavailableError as exc:
                logger.warning("Skipping unavailable video %s: %s", video_url, exc)
                errors.append({"url": video_url, "reason": str(exc)})
            except RateLimitError as exc:
                logger.error("Rate limit hit on %s — stopping playlist scrape: %s", video_url, exc)
                raise
            except ScraperError as exc:
                logger.warning("Skipping %s due to error: %s", video_url, exc)
                errors.append({"url": video_url, "reason": str(exc)})

        if errors:
            print(f"\n[warn] {len(errors)} video(s) skipped:")
            for e in errors:
                print(f"  - {e['url']}: {e['reason']}")

        return results

    def save_to_json(self, metadata: VideoMetadata, output_dir: str = ".") -> str:
        """Serialize metadata to a JSON file and return the file path."""
        os.makedirs(output_dir, exist_ok=True)
        filename = os.path.join(output_dir, f"{metadata.id}.json")
        payload = {
            "id": metadata.id,
            "title": metadata.title,
            "description": metadata.description,
            "uploader": metadata.uploader,
            "upload_date": metadata.upload_date,
            "duration_seconds": metadata.duration,
            "view_count": metadata.view_count,
            "like_count": metadata.like_count,
            "thumbnail": metadata.thumbnail,
            "tags": metadata.tags,
            "categories": metadata.categories,
            "webpage_url": metadata.webpage_url,
            "captions": metadata.captions,
        }
        with open(filename, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return filename


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="YouTube metadata & caption scraper")
    parser.add_argument("url", help="YouTube video or playlist URL")
    parser.add_argument(
        "--langs",
        nargs="+",
        default=["de", "en"],
        metavar="LANG",
        help="Caption languages to fetch (default: de en)",
    )
    parser.add_argument(
        "--no-auto-captions",
        action="store_true",
        help="Disable automatic/auto-generated captions",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        metavar="DIR",
        help="Directory to save JSON files (default: output/)",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        metavar="FILE",
        help="Path to Netscape cookies file for age-restricted videos",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Treat URL as a playlist",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Max retry attempts on transient errors (default: 3)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    scraper = YouTubeScraper(
        caption_langs=args.langs,
        prefer_auto_captions=not args.no_auto_captions,
        cookies_file=args.cookies,
        retries=args.retries,
    )

    try:
        if args.playlist:
            videos = scraper.scrape_playlist(args.url)
            for video in videos:
                path = scraper.save_to_json(video, args.output_dir)
                print(f"Saved: {path}  ({video.title!r})")
            print(f"\nTotal: {len(videos)} videos scraped.")
        else:
            video = scraper.scrape(args.url)
            path = scraper.save_to_json(video, args.output_dir)
            print(f"Title      : {video.title}")
            print(f"Uploader   : {video.uploader}")
            print(f"Duration   : {video.duration}s")
            print(f"Views      : {video.view_count}")
            print(f"Upload date: {video.upload_date}")
            print(f"Tags       : {', '.join(video.tags[:5])}")
            print(f"Captions   : {list(video.captions.keys())}")
            print(f"Saved to   : {path}")

    except VideoUnavailableError as exc:
        print(f"[error] Video not accessible: {exc}")
        raise SystemExit(1)
    except RateLimitError as exc:
        print(f"[error] Rate limited by YouTube. Wait a while and retry. ({exc})")
        raise SystemExit(2)
    except ScraperError as exc:
        print(f"[error] {exc}")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
