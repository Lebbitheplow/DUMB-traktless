"""Shared indexer helpers.

Ported verbatim from the Trakt indexer so TMDB/TVDB indexers behave identically
to what they replace. Deliberately does not use upstream v1.0.0's base.py, which
relies on PEP 695 generics and modules that do not exist in v0.23.6.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger

from program.media.item import MediaItem
from program.settings.manager import settings_manager

ANIME_GENRES = ("animation", "donghua", "anime")
ANIME_COUNTRIES = ("jp", "kr", "cn", "hk")


class BaseIndexer:
    """Common behaviour for metadata indexers."""

    def __init__(self):
        self.settings = settings_manager.settings.indexer
        self.failed_ids = set()
        self.initialized = True

    @staticmethod
    def copy_attributes(source, target):
        """Copy attributes from source to target."""
        attributes = [
            "file", "folder", "update_folder", "symlinked", "is_anime",
            "symlink_path", "subtitles", "requested_by", "requested_at",
            "overseerr_id", "active_stream", "requested_id", "streams",
        ]
        for attr in attributes:
            target.set(attr, getattr(source, attr, None))

    def copy_items(self, itema: MediaItem, itemb: MediaItem):
        """Copy attributes from itema to itemb recursively."""
        is_anime = itema.is_anime or itemb.is_anime
        if itema.type == "mediaitem" and itemb.type == "show":
            itema.seasons = itemb.seasons
        if itemb.type == "show" and itema.type != "movie":
            for seasona in itema.seasons:
                for seasonb in itemb.seasons:
                    if seasona.number == seasonb.number:
                        for episodea in seasona.episodes:
                            for episodeb in seasonb.episodes:
                                if episodea.number == episodeb.number:
                                    self.copy_attributes(episodea, episodeb)
                                    episodeb.set("is_anime", is_anime)
                        seasonb.set("is_anime", is_anime)
            itemb.set("is_anime", is_anime)
        elif itemb.type == "movie":
            self.copy_attributes(itema, itemb)
            itemb.set("is_anime", is_anime)
        else:
            logger.error(
                f"Item types {itema.type} and {itemb.type} do not match cant copy metadata"
            )
        return itemb

    @staticmethod
    def should_submit(item: MediaItem) -> bool:
        if not item.indexed_at or not item.title:
            return True

        settings = settings_manager.settings.indexer

        try:
            interval = timedelta(seconds=settings.update_interval)
            return datetime.now() - item.indexed_at > interval
        except Exception:
            logger.error(f"Failed to parse date: {item.indexed_at}")
            return False

    # ----------------------------------------------------------- mapping aids

    @staticmethod
    def parse_date(value: Optional[str]) -> Optional[datetime]:
        """TMDB and TVDB both use plain YYYY-MM-DD dates."""
        if not value:
            return None
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    @staticmethod
    def is_anime(genres: List[str], country: Optional[str]) -> bool:
        """Mirrors TraktAPI._is_anime so anime routing is unchanged."""
        if not genres or country == "us":
            return False
        if not any(genre in genres for genre in ANIME_GENRES):
            return False
        if (country or "") not in ANIME_COUNTRIES:
            return False
        return True

    @staticmethod
    def normalise_genres(names: List[Optional[str]]) -> List[str]:
        """Trakt supplied lowercase genres and the rest of the code assumes it."""
        return [n.lower() for n in names if n]

    @staticmethod
    def merge_aliases(*sources: Dict[str, List[str]]) -> Dict[str, List[str]]:
        merged: Dict[str, List[str]] = {}
        for source in sources:
            for key, values in (source or {}).items():
                merged.setdefault(key, [])
                for value in values:
                    if value not in merged[key]:
                        merged[key].append(value)
        return merged
