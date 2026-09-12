"""TMDB API client.

Backport of upstream Riven's TMDB indexer support into v0.23.6. Supplies movie
metadata (and IMDb id resolution) so the indexer no longer depends on Trakt.
"""

import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from requests import Session

from program.utils.request import (
    BaseRequestHandler,
    HttpMethod,
    ResponseObject,
    ResponseType,
    create_service_session,
    get_rate_limit_params,
    logger,
)

# Riven's bundled read token. Overridable via settings/env so a revoked token
# can be swapped without a code change - the failure mode that took Trakt down.
DEFAULT_TMDB_READ_ACCESS_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6"
    "IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ."
    "xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"
)


class TMDBAPIError(Exception):
    """Base exception for TMDBApi related errors"""


class TMDBRequestHandler(BaseRequestHandler):
    def __init__(self, session: Session, response_type=ResponseType.SIMPLE_NAMESPACE, request_logging: bool = False):
        super().__init__(
            session,
            response_type=response_type,
            custom_exception=TMDBAPIError,
            request_logging=request_logging,
        )

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> ResponseObject:
        return super()._request(method, endpoint, **kwargs)


class TMDBAPI:
    """Handles TMDB API communication"""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, read_access_token: str = ""):
        token = (
            read_access_token
            or os.environ.get("TMDB_READ_ACCESS_TOKEN")
            or DEFAULT_TMDB_READ_ACCESS_TOKEN
        )
        rate_limit_params = get_rate_limit_params(max_calls=50, period=1)
        session = create_service_session(rate_limit_params=rate_limit_params)
        if self.is_read_access_token(token):
            session.headers.update({"Authorization": f"Bearer {token}"})
        else:
            # A v3 API key (what Diskovarr and most self-hosters have) is sent
            # as the api_key query parameter instead of a bearer JWT.
            session.params = {**getattr(session, "params", {}), "api_key": token}
        self.request_handler = TMDBRequestHandler(session)

    @staticmethod
    def is_read_access_token(value: str) -> bool:
        """v4 read-access tokens are JWTs; v3 keys are 32 hex characters."""
        return value.count(".") == 2 and value.startswith("eyJ")

    def validate(self) -> bool:
        """Cheap authenticated call to confirm the token works."""
        return self._get("movie/550") is not None

    def _get(self, endpoint: str) -> Optional[Any]:
        try:
            response = self.request_handler.execute(
                HttpMethod.GET, f"{self.BASE_URL}/{endpoint}", timeout=30
            )
            if response.is_ok and response.data:
                return response.data
            return None
        except Exception as e:
            logger.debug(f"TMDB request failed for {endpoint}: {e}")
            return None

    # ------------------------------------------------------------- endpoints

    def find_by_imdb_id(self, imdb_id: str) -> Dict[str, Optional[int]]:
        """Resolve an IMDb id to TMDB ids, keyed by media type."""
        result: Dict[str, Optional[int]] = {"movie": None, "tv": None}
        if not imdb_id:
            return result
        data = self._get(f"find/{imdb_id}?external_source=imdb_id")
        if not data:
            return result
        movies = getattr(data, "movie_results", None) or []
        shows = getattr(data, "tv_results", None) or []
        if movies:
            result["movie"] = getattr(movies[0], "id", None)
        if shows:
            result["tv"] = getattr(shows[0], "id", None)
        return result

    def get_movie(self, tmdb_id: int) -> Optional[SimpleNamespace]:
        """Movie details plus external ids and alternative titles in one call."""
        return self._get(
            f"movie/{tmdb_id}?append_to_response=external_ids,alternative_titles"
        )

    def get_imdb_id(self, tmdb_id: int, media_type: str = "movie") -> Optional[str]:
        """Resolve a TMDB id to an IMDb id. Replaces Trakt's tmdb->imdb lookup."""
        if not tmdb_id:
            return None
        _type = "tv" if media_type in ("tv", "show") else "movie"
        data = self._get(f"{_type}/{tmdb_id}/external_ids")
        imdb_id = getattr(data, "imdb_id", None) if data else None
        return imdb_id if imdb_id and imdb_id.startswith("tt") else None

    @staticmethod
    def get_aliases(movie: SimpleNamespace) -> Dict[str, List[str]]:
        """Map alternative_titles into Trakt's {country: [titles]} shape for RTN."""
        aliases: Dict[str, List[str]] = {}
        if not movie:
            return aliases
        container = getattr(movie, "alternative_titles", None)
        titles = getattr(container, "titles", None) or getattr(container, "results", None) or []
        for entry in titles:
            title = getattr(entry, "title", None)
            if not title:
                continue
            country = (getattr(entry, "iso_3166_1", None) or "us").lower()
            aliases.setdefault(country, [])
            if title not in aliases[country]:
                aliases[country].append(title)
        return aliases
