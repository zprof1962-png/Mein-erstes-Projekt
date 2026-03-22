"""
YouTube Scraper using yt-dlp
Extracts video metadata and captions/subtitles.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional
import yt_dlp


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


class YouTubeScraper:
    """Scraper for YouTube video metadata and captions using yt-dlp."""

    DEFAULT_CAPTION_LANGS = ["de", "en"]

    def __init__(
        self,
        caption_langs: Optional[list[str]] = None,
        prefer_auto_captions: bool = True,
        cookies_file: Optional[str] = None,
    ):
        self.caption_langs = caption_langs or self.DEFAULT_CAPTION_LANGS
        self.prefer_auto_captions = prefer_auto_captions
        self.cookies_file = cookies_file

    def _build_ydl_opts(self, skip_download: bool = True) -> dict:
        opts = {
            "skip_download": skip_download,
            "writesubtitles": True,
            "writeautomaticsub": self.prefer_auto_captions,
            "subtitleslangs": self.caption_langs,
            "subtitlesformat": "vtt",
            "quiet": True,
            "no_warnings": False,
            "extract_flat": False,
        }
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        return opts

    def _extract_captions_from_info(self, info: dict) -> dict[str, str]:
        """Extract caption text from the info dict subtitles/automatic_captions."""
        captions: dict[str, str] = {}

        for source_key in ("subtitles", "automatic_captions"):
            source = info.get(source_key, {})
            if not source:
                continue
            for lang, formats in source.items():
                if lang in captions:
                    continue
                # Prefer vtt, then json3, then any
                for fmt in formats:
                    if fmt.get("ext") in ("vtt", "json3", "srv3", "srv2", "srv1", "ttml"):
                        url = fmt.get("url")
                        if url:
                            text = self._fetch_caption_text(url, fmt.get("ext", "vtt"))
                            if text:
                                captions[lang] = text
                                break

        return captions

    def _fetch_caption_text(self, url: str, ext: str) -> Optional[str]:
        """Download and parse caption content from a URL."""
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except Exception:
            return None

        if ext == "json3":
            return self._parse_json3(raw)
        # For VTT and other formats, return the raw text (simple enough for most use cases)
        return raw

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

    def scrape(self, url: str) -> VideoMetadata:
        """Scrape metadata and captions for a single YouTube video URL."""
        opts = self._build_ydl_opts()

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            raise ValueError(f"Could not extract info for URL: {url}")

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

    def scrape_playlist(self, url: str) -> list[VideoMetadata]:
        """Scrape all videos in a YouTube playlist."""
        opts = {
            **self._build_ydl_opts(),
            "extract_flat": "in_playlist",
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            playlist_info = ydl.extract_info(url, download=False)

        if playlist_info is None:
            raise ValueError(f"Could not extract playlist info for URL: {url}")

        entries = playlist_info.get("entries", [])
        results = []
        for entry in entries:
            video_url = entry.get("url") or entry.get("webpage_url")
            if video_url:
                try:
                    results.append(self.scrape(video_url))
                except Exception as exc:
                    print(f"[warn] Skipping {video_url}: {exc}")

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


def main():
    import argparse

    parser = argparse.ArgumentParser(description="YouTube metadata & caption scraper")
    parser.add_argument("url", help="YouTube video or playlist URL")
    parser.add_argument(
        "--langs",
        nargs="+",
        default=["de", "en"],
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
        help="Directory to save JSON files (default: output/)",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="Path to Netscape cookies file for age-restricted videos",
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Treat URL as a playlist",
    )
    args = parser.parse_args()

    scraper = YouTubeScraper(
        caption_langs=args.langs,
        prefer_auto_captions=not args.no_auto_captions,
        cookies_file=args.cookies,
    )

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


if __name__ == "__main__":
    main()
