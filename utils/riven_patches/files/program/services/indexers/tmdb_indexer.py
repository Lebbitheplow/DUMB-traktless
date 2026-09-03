"""TMDB indexer - handles movies."""

from datetime import datetime
from typing import Generator, Optional

from kink import di
from loguru import logger

from program.apis.tmdb_api import TMDBAPI
from program.media.item import MediaItem, Movie
from program.services.indexers.base import BaseIndexer


class TMDBIndexer(BaseIndexer):
    """Indexes movies using TMDB."""

    key = "TMDBIndexer"

    def __init__(self):
        super().__init__()
        self.api = di[TMDBAPI]

    def run(self, in_item: MediaItem, log_msg: bool = True) -> Generator[Movie, None, None]:
        """Index a movie from its IMDb id."""
        if not in_item:
            logger.error("Item is None")
            return
        if not (imdb_id := in_item.imdb_id):
            logger.error(f"Item {in_item.log_string} does not have an imdb_id, cannot index it")
            return
        if imdb_id in self.failed_ids:
            return

        item = self._create_movie(imdb_id)
        if not item:
            self.failed_ids.add(imdb_id)
            return

        item = self.copy_items(in_item, item)
        item.indexed_at = datetime.now()

        if log_msg:
            logger.info(f"Indexed IMDb id ({imdb_id}) as Movie: {item.log_string}")
        yield item

    def _create_movie(self, imdb_id: str) -> Optional[Movie]:
        tmdb_id = self.api.find_by_imdb_id(imdb_id).get("movie")
        if not tmdb_id:
            logger.debug(f"TMDB has no movie for imdb id: {imdb_id}")
            return None

        details = self.api.get_movie(tmdb_id)
        if not details:
            logger.error(f"Failed to fetch TMDB movie details for {imdb_id}")
            return None

        aired_at = self.parse_date(getattr(details, "release_date", None))
        genres = self.normalise_genres(
            [getattr(g, "name", None) for g in (getattr(details, "genres", None) or [])]
        )
        countries = getattr(details, "production_countries", None) or []
        country = (
            (getattr(countries[0], "iso_3166_1", None) or "").lower() if countries else None
        )
        external = getattr(details, "external_ids", None)

        item = {
            "title": getattr(details, "title", None),
            "year": aired_at.year if aired_at else None,
            "status": getattr(details, "status", None),
            "aired_at": aired_at,
            # id is derived as f"{type}_{trakt_id}"; namespace it so TMDB ids
            # cannot collide with existing Trakt-derived primary keys.
            "trakt_id": f"tmdb{tmdb_id}",
            "imdb_id": getattr(external, "imdb_id", None) or imdb_id,
            "tvdb_id": None,
            "tmdb_id": tmdb_id,
            "genres": genres,
            "network": None,
            "country": country,
            "language": getattr(details, "original_language", None),
            "aliases": self.api.get_aliases(details),
            "requested_at": datetime.now(),
            "type": "movie",
        }
        item["is_anime"] = self.is_anime(genres, country)
        return Movie(item)
