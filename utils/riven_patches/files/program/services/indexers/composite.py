"""Composite indexer - TMDB for movies, TVDB for shows.

Replaces TraktIndexer. Mirrors upstream Riven v1.0.0's IndexerService, adapted
to the v0.23.6 service contract.
"""

from typing import Generator, Union

from kink import di
from loguru import logger

from program.apis.tmdb_api import TMDBAPI
from program.media.item import Episode, MediaItem, Movie, Season, Show
from program.services.indexers.base import BaseIndexer
from program.services.indexers.tmdb_indexer import TMDBIndexer
from program.services.indexers.tvdb_indexer import TVDBIndexer


class IndexerService(BaseIndexer):
    """Delegates indexing to TMDB (movies) or TVDB (shows)."""

    key = "IndexerService"

    def __init__(self):
        super().__init__()
        self.tmdb_indexer = TMDBIndexer()
        self.tvdb_indexer = TVDBIndexer()
        self.tmdb_api = di[TMDBAPI]

    def run(
        self, in_item: MediaItem, log_msg: bool = True
    ) -> Generator[Union[Movie, Show, Season, Episode], None, None]:
        """Route to the right indexer based on item type."""
        if not in_item:
            logger.error("Item is None")
            return
        if not (imdb_id := in_item.imdb_id):
            logger.error(f"Item {in_item.log_string} does not have an imdb_id, cannot index it")
            return

        # Explicitly typed items route directly.
        if isinstance(in_item, Movie) or in_item.type == "movie":
            yield from self.tmdb_indexer.run(in_item, log_msg=log_msg)
            return
        if isinstance(in_item, (Show, Season, Episode)) or in_item.type in ("show", "season", "episode"):
            yield from self.tvdb_indexer.run(in_item, log_msg=log_msg)
            return

        # Untyped item (the usual case for incoming requests): one TMDB /find
        # call tells us whether the IMDb id is a movie or a series.
        found = self.tmdb_api.find_by_imdb_id(imdb_id)
        if found.get("movie"):
            yield from self.tmdb_indexer.run(in_item, log_msg=log_msg)
            return
        if found.get("tv"):
            yield from self.tvdb_indexer.run(in_item, log_msg=log_msg)
            return

        # /find knows nothing - try TVDB directly before giving up, since it
        # carries series TMDB sometimes lacks.
        result = next(self.tvdb_indexer.run(in_item, log_msg=log_msg), None)
        if result:
            yield result
            return

        logger.error(f"Failed to index item with imdb_id: {imdb_id}")

    @property
    def failed_ids(self):
        """Union of both indexers' blacklists (they own the real state)."""
        return self.tmdb_indexer.failed_ids | self.tvdb_indexer.failed_ids

    @failed_ids.setter
    def failed_ids(self, value):
        # BaseIndexer.__init__ seeds this; the child indexers hold the real sets.
        self._failed_ids = value
