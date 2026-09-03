"""TVDB indexer - handles shows, seasons and episodes."""

from datetime import datetime
from typing import Dict, Generator, List, Optional

from kink import di
from loguru import logger

from program.apis.tmdb_api import TMDBAPI
from program.apis.tvdb_api import TVDBAPI
from program.media.item import Episode, MediaItem, Season, Show
from program.services.indexers.base import BaseIndexer

# TVDB reports ISO 3166-1 alpha-3; _is_anime and the rest of Riven expect
# alpha-2 (Trakt's format). Getting this wrong silently disables anime routing.
COUNTRY_A3_TO_A2 = {
    "jpn": "jp", "kor": "kr", "chn": "cn", "hkg": "hk", "twn": "tw",
    "usa": "us", "gbr": "gb", "can": "ca", "aus": "au", "fra": "fr",
    "deu": "de", "esp": "es", "ita": "it", "nld": "nl", "swe": "se",
    "nor": "no", "dnk": "dk", "fin": "fi", "bel": "be", "irl": "ie",
    "ind": "in", "bra": "br", "mex": "mx", "rus": "ru", "pol": "pl",
    "tur": "tr", "isr": "il", "nzl": "nz", "zaf": "za", "arg": "ar",
}


class TVDBIndexer(BaseIndexer):
    """Indexes shows (with seasons and episodes) using TVDB."""

    key = "TVDBIndexer"

    def __init__(self):
        super().__init__()
        self.api = di[TVDBAPI]
        self.tmdb_api = di[TMDBAPI]

    def run(self, in_item: MediaItem, log_msg: bool = True) -> Generator[Show, None, None]:
        """Index a show from its IMDb id."""
        if not in_item:
            logger.error("Item is None")
            return
        if not (imdb_id := in_item.imdb_id):
            logger.error(f"Item {in_item.log_string} does not have an imdb_id, cannot index it")
            return
        if imdb_id in self.failed_ids:
            return

        item = self._create_show(imdb_id)
        if not item:
            self.failed_ids.add(imdb_id)
            return

        item = self.copy_items(in_item, item)
        item.indexed_at = datetime.now()

        if log_msg:
            logger.info(f"Indexed IMDb id ({imdb_id}) as Show: {item.log_string}")
        yield item

    def _create_show(self, imdb_id: str) -> Optional[Show]:
        tvdb_id = self.api.search_by_imdb_id(imdb_id)
        if not tvdb_id:
            logger.debug(f"TVDB has no series for imdb id: {imdb_id}")
            return None

        details = self.api.get_series(tvdb_id)
        if not details:
            logger.error(f"Failed to fetch TVDB series details for {imdb_id}")
            return None

        # The frontend routes shows as /tv/{tmdb_id}; without this the link is
        # /tv/null and the page 500s. TVDB is still the metadata source - this
        # is only the id the UI needs, so a miss must not block indexing.
        try:
            tmdb_id = self.tmdb_api.find_by_imdb_id(imdb_id).get("tv")
        except Exception as e:
            logger.debug(f"TMDB id lookup failed for {imdb_id}: {e}")
            tmdb_id = None
        if not tmdb_id:
            logger.debug(f"TMDB has no series for imdb id: {imdb_id}")

        aired_at = self.parse_date(getattr(details, "firstAired", None))
        genres = self.normalise_genres(
            [getattr(g, "name", None) for g in (getattr(details, "genres", None) or [])]
        )
        country_a3 = (getattr(details, "originalCountry", None) or "").lower()
        country = COUNTRY_A3_TO_A2.get(country_a3, country_a3[:2] or None)
        language_a3 = (getattr(details, "originalLanguage", None) or "").lower()
        # ISO 639-2 -> 639-1 (jpn -> ja, not "jp"); reuse the client's map.
        language = TVDBAPI._LANG_2.get(language_a3, language_a3[:2]) if language_a3 else None

        show_dict = {
            "title": getattr(details, "name", None),
            "year": getattr(details, "year", None) or (aired_at.year if aired_at else None),
            "status": getattr(getattr(details, "status", None), "name", None),
            "aired_at": aired_at,
            # id is derived as f"{type}_{trakt_id}"; namespace it so TVDB ids
            # cannot collide with existing Trakt-derived primary keys.
            "trakt_id": f"tvdb{tvdb_id}",
            "imdb_id": imdb_id,
            "tvdb_id": tvdb_id,
            "tmdb_id": tmdb_id,
            "genres": genres,
            "network": getattr(getattr(details, "originalNetwork", None), "name", None),
            "country": country,
            "language": language,
            "aliases": self.api.get_aliases(details),
            "requested_at": datetime.now(),
            "type": "show",
        }
        show_dict["is_anime"] = self.is_anime(genres, country)

        show = Show(show_dict)
        self._add_seasons_to_show(show, tvdb_id, show_dict)
        return show

    def _add_seasons_to_show(self, show: Show, tvdb_id: int, show_dict: dict) -> None:
        """Group TVDB episodes into seasons and attach them to the show."""
        episodes = self.api.get_episodes(tvdb_id)
        if not episodes:
            logger.debug(f"TVDB returned no episodes for series {tvdb_id}")
            return

        by_season: Dict[int, List] = {}
        for episode in episodes:
            season_number = getattr(episode, "seasonNumber", None)
            number = getattr(episode, "number", None)
            # Season 0 is specials - Riven skips it, same as the Trakt indexer.
            if season_number in (None, 0) or number is None:
                continue
            by_season.setdefault(season_number, []).append(episode)

        for season_number in sorted(by_season):
            season = Season({
                "number": season_number,
                "trakt_id": f"tvdb{tvdb_id}_s{season_number}",
                "title": f"{show_dict['title']} Season {season_number}",
                "imdb_id": None,
                "tvdb_id": None,
                "tmdb_id": None,
                "genres": show_dict["genres"],
                "network": show_dict["network"],
                "country": show_dict["country"],
                "language": show_dict["language"],
                "is_anime": show_dict["is_anime"],
                "aliases": {},
                "requested_at": datetime.now(),
                "type": "season",
            })

            for episode in by_season[season_number]:
                aired_at = self.parse_date(getattr(episode, "aired", None))
                season.add_episode(Episode({
                    "number": getattr(episode, "number", None),
                    "trakt_id": f"tvdb{getattr(episode, 'id', None)}",
                    "title": getattr(episode, "name", None),
                    "aired_at": aired_at,
                    "year": aired_at.year if aired_at else None,
                    # TVDB does not expose per-episode IMDb ids here, and Riven
                    # does not need them - symlinking and scraping both use the
                    # show's imdb_id.
                    "imdb_id": None,
                    "tvdb_id": getattr(episode, "id", None),
                    "tmdb_id": None,
                    "genres": show_dict["genres"],
                    "network": show_dict["network"],
                    "country": show_dict["country"],
                    "language": show_dict["language"],
                    "is_anime": show_dict["is_anime"],
                    "aliases": {},
                    "requested_at": datetime.now(),
                    "type": "episode",
                }))

            show.add_season(season)
