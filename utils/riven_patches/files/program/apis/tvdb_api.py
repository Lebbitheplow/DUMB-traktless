"""TheTVDB v4 API client.

Backport of upstream Riven's TVDB indexer support into v0.23.6. Provides show,
season and episode metadata so the indexer no longer depends on Trakt.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from requests import Session

from program.utils import data_dir_path
from program.utils.request import (
    BaseRequestHandler,
    HttpMethod,
    ResponseObject,
    ResponseType,
    create_service_session,
    get_rate_limit_params,
    logger,
)

# Riven's project key. Overridable via settings/env so a revoked key can be
# swapped without a code change - the failure mode that took Trakt down.
DEFAULT_TVDB_API_KEY = "6be85335-5c4f-4d8d-b945-d3ed0eb8cdce"


class TVDBAPIError(Exception):
    """Base exception for TVDBApi related errors"""


class TVDBRequestHandler(BaseRequestHandler):
    def __init__(self, session: Session, response_type=ResponseType.SIMPLE_NAMESPACE, request_logging: bool = False):
        super().__init__(
            session,
            response_type=response_type,
            custom_exception=TVDBAPIError,
            request_logging=request_logging,
        )

    def execute(self, method: HttpMethod, endpoint: str, **kwargs) -> ResponseObject:
        return super()._request(method, endpoint, **kwargs)


class TVDBAPI:
    """Handles TheTVDB v4 API communication"""

    BASE_URL = "https://api4.thetvdb.com/v4"
    TOKEN_FILE = Path(data_dir_path) / "tvdb_token.json"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("TVDB_API_KEY") or DEFAULT_TVDB_API_KEY
        self.token: Optional[str] = None
        self.token_expires: Optional[datetime] = None

        rate_limit_params = get_rate_limit_params(max_calls=1000, period=300)
        session = create_service_session(rate_limit_params=rate_limit_params)
        self.request_handler = TVDBRequestHandler(session)
        self.session = session

    # ------------------------------------------------------------------ auth

    def validate(self) -> bool:
        """Ensure we can authenticate. Called once at startup."""
        return self._ensure_token() is not None

    def _ensure_token(self) -> Optional[str]:
        """Return a valid bearer token, logging in or refreshing as needed."""
        if self.token and self.token_expires and datetime.now() < self.token_expires:
            return self.token

        if self._load_token_from_file():
            return self.token

        return self._login()

    def _load_token_from_file(self) -> bool:
        try:
            if not self.TOKEN_FILE.exists():
                return False
            data = json.loads(self.TOKEN_FILE.read_text())
            expires = datetime.fromisoformat(data["expires_at"])
            # Refresh a day early rather than risk mid-run expiry.
            if expires - timedelta(days=1) <= datetime.now():
                return False
            self.token = data["token"]
            self.token_expires = expires
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            return True
        except Exception:
            return False

    def _save_token_to_file(self) -> None:
        try:
            self.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.TOKEN_FILE.write_text(
                json.dumps({"token": self.token, "expires_at": self.token_expires.isoformat()})
            )
        except Exception as e:
            logger.debug(f"Could not cache TVDB token: {e}")

    def _login(self) -> Optional[str]:
        try:
            response = self.request_handler.execute(
                HttpMethod.POST,
                f"{self.BASE_URL}/login",
                json={"apikey": self.api_key},
                timeout=30,
            )
            if not response.is_ok or not response.data:
                logger.error("TVDB login failed - check RIVEN_INDEXER_TVDB_API_KEY")
                return None

            token = getattr(getattr(response.data, "data", None), "token", None)
            if not token:
                logger.error("TVDB login returned no token")
                return None

            self.token = token
            # Tokens are valid ~30 days; keep our own conservative expiry.
            self.token_expires = datetime.now() + timedelta(days=25)
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            self._save_token_to_file()
            logger.debug("Obtained new TVDB token")
            return self.token
        except Exception as e:
            logger.error(f"TVDB login error: {e}")
            return None

    def _get(self, endpoint: str) -> Optional[Any]:
        """GET an endpoint, retrying once after a re-login on auth failure."""
        if not self._ensure_token():
            return None

        for attempt in (1, 2):
            try:
                response = self.request_handler.execute(
                    HttpMethod.GET, f"{self.BASE_URL}/{endpoint}", timeout=30
                )
                if response.is_ok and response.data:
                    return getattr(response.data, "data", None)
                # Token may have been revoked early - re-login once.
                if attempt == 1 and response.status_code in (401, 403):
                    self.token = None
                    self.TOKEN_FILE.unlink(missing_ok=True)
                    if not self._login():
                        return None
                    continue
                return None
            except Exception as e:
                if attempt == 1:
                    continue
                logger.debug(f"TVDB request failed for {endpoint}: {e}")
                return None
        return None

    # ------------------------------------------------------------- endpoints

    def search_by_imdb_id(self, imdb_id: str) -> Optional[int]:
        """Resolve an IMDb id to a TVDB series id."""
        if not imdb_id:
            return None
        results = self._get(f"search/remoteid/{imdb_id}")
        if not results:
            return None
        for entry in results:
            series = getattr(entry, "series", None)
            if series and getattr(series, "id", None):
                return int(series.id)
        return None

    def get_series(self, tvdb_id: int) -> Optional[SimpleNamespace]:
        """Extended series record (includes genres, aliases, status)."""
        return self._get(f"series/{tvdb_id}/extended")

    def get_episodes(self, tvdb_id: int) -> List[SimpleNamespace]:
        """All episodes for a series in default (aired) order."""
        episodes: List[SimpleNamespace] = []
        page = 0
        while page < 100:  # hard stop; TVDB pages are 500 episodes each
            data = self._get(f"series/{tvdb_id}/episodes/default?page={page}")
            if not data:
                break
            batch = getattr(data, "episodes", None) or []
            if not batch:
                break
            episodes.extend(batch)
            if len(batch) < 500:
                break
            page += 1
        return episodes

    # TVDB reports ISO 639-2 (eng/por/deu); Trakt reported 2-letter codes and
    # that is what ranking.languages.exclude is matched against. Naive
    # truncation is wrong for several common ones (por -> "po", not "pt").
    _LANG_2 = {
        "eng": "en", "spa": "es", "por": "pt", "deu": "de", "ger": "de",
        "fra": "fr", "fre": "fr", "ita": "it", "jpn": "ja", "kor": "ko",
        "zho": "zh", "chi": "zh", "rus": "ru", "nld": "nl", "dut": "nl",
        "pol": "pl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
        "tur": "tr", "ara": "ar", "heb": "he", "hin": "hi", "ces": "cs",
        "cze": "cs", "ell": "el", "gre": "el", "hun": "hu", "tha": "th",
    }

    @classmethod
    def get_aliases(cls, series: SimpleNamespace) -> Dict[str, List[str]]:
        """Map TVDB aliases into Trakt's {code: [titles]} shape for RTN."""
        aliases: Dict[str, List[str]] = {}
        if not series:
            return aliases
        for alias in getattr(series, "aliases", None) or []:
            name = getattr(alias, "name", None)
            if not name:
                continue
            language = (getattr(alias, "language", None) or "eng").lower()
            code = cls._LANG_2.get(language, language[:2])
            aliases.setdefault(code, [])
            if name not in aliases[code]:
                aliases[code].append(name)
        return aliases
