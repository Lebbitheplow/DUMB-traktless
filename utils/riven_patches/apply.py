#!/usr/bin/env python3
"""Traktless Riven patch set.

DUMB does not vendor Riven - it downloads a `rivenmedia/riven` archive into
`/riven/backend` on every install/update and, because `riven_backend` runs with
`clear_on_update: true` (protecting only `/riven/backend/data`), everything under
`src/` is wiped each time. This module re-applies our local fixes afterwards.

Five groups of changes, all against Riven v0.23.6:

1. TMDB/TVDB indexer backport - replaces `TraktIndexer` with the `IndexerService`
   composite backported from Riven v1.0.0, so indexing no longer needs Trakt.
   Adds six new modules (see `files/`) and rewires the call sites.
2. Scraper bucket hang - `use_memory_list` defaults to True. `MemoryQueueBucket`
   wraps a bounded Queue whose `put()` has no timeout, and
   `requests_ratelimiter._fill_bucket()` (fired on any 429) pushes filler items
   without checking capacity. On a full bucket that `put()` blocks forever: the
   scraper worker never returns, `Scraping.scrape()` waits on it in
   `as_completed()` for good, and the item's DB session is stranded
   idle-in-transaction. Observed wedging the whole scrape pipeline every 16-40h.
   `MemoryListBucket` enforces the same rates but drops overflow instead.
3. AllDebrid v4.1 - the v4 endpoint no longer returns a usable `downloaded`
   figure, so progress is derived from `statusCode` instead (4 == Ready).
4. RealDebrid error handling - upstream assumes every exception carries a
   `.response`, so a connection error raises AttributeError instead of
   RealDebridError. Also maps 402/451, which RD returns for DMCA-blocked hashes.
5. Overseerr efficiency - prefer the `imdbId` already present on the request
   payload instead of issuing a second Overseerr lookup per pending item.

Idempotent: safe to run repeatedly. Fails loudly and names the file if an anchor
is missing, which means upstream changed that code and the patch needs reviewing
by hand rather than being silently skipped.

Used two ways:

* Imported by `utils/setup.py`, which calls `apply_riven_patches()` from
  `additional_setup()` after each Riven Backend install or update.
* Standalone, to patch a container running the stock upstream image:

      docker cp utils/riven_patches DUMB:/tmp/riven_patches
      docker exec DUMB /riven/backend/venv/bin/python /tmp/riven_patches/apply.py
"""

import argparse
import shutil
import sys
from pathlib import Path

try:  # Available when imported inside DUMB; absent when run standalone.
    from utils.global_logger import logger
except ImportError:  # pragma: no cover - standalone execution
    logger = None

HERE = Path(__file__).resolve().parent
FILES = HERE / "files"

DEFAULT_CONFIG_DIR = "/riven/backend"

# (group, path, marker_meaning_already_applied, old, new)
PATCHES = [
    # -- 1. TMDB/TVDB indexer backport ------------------------------------
    (
        "indexer",
        "program/state_transition.py",
        "from program.services.indexers.composite import IndexerService",
        "from program.services.indexers.trakt import TraktIndexer",
        "from program.services.indexers.composite import IndexerService",
    ),
    (
        "indexer",
        "program/state_transition.py",
        "next_service = IndexerService",
        "next_service = TraktIndexer",
        "next_service = IndexerService",
    ),
    (
        "indexer",
        "program/program.py",
        "from program.services.indexers.composite import IndexerService",
        "from program.services.indexers.trakt import TraktIndexer",
        "from program.services.indexers.composite import IndexerService",
    ),
    (
        "indexer",
        "program/program.py",
        "IndexerService: IndexerService(),",
        "TraktIndexer: TraktIndexer(),",
        "IndexerService: IndexerService(),",
    ),
    (
        "indexer",
        "program/program.py",
        "self.services[IndexerService]",
        "self.services[TraktIndexer]",
        "self.services[IndexerService]",
    ),
    (
        "indexer",
        "routers/secure/items.py",
        "from program.services.indexers.composite import IndexerService",
        "from program.services.indexers.trakt import TraktIndexer",
        "from program.services.indexers.composite import IndexerService",
    ),
    (
        "indexer",
        "routers/secure/items.py",
        "all_services[IndexerService]",
        "all_services[TraktIndexer]",
        "all_services[IndexerService]",
    ),
    (
        "indexer",
        "routers/secure/scrape.py",
        "from program.services.indexers.composite import IndexerService",
        "from program.services.indexers.trakt import TraktIndexer",
        "from program.services.indexers.composite import IndexerService",
    ),
    (
        "indexer",
        "routers/secure/scrape.py",
        "services[IndexerService]",
        "services[TraktIndexer]",
        "services[IndexerService]",
    ),
    (
        "indexer",
        "routers/secure/scrape.py",
        "IndexerService().run(prepared_item)",
        "TraktIndexer().run(prepared_item)",
        "IndexerService().run(prepared_item)",
    ),
    (
        "indexer",
        "program/services/indexers/__init__.py",
        "from .composite import IndexerService",
        "from .trakt import TraktIndexer  # noqa",
        "from .composite import IndexerService  # noqa\n"
        "from .trakt import TraktIndexer  # noqa  (kept for rollback; no longer wired in)",
    ),
    (
        "indexer",
        "program/settings/models.py",
        'tvdb_api_key: str = ""',
        "class IndexerModel(Observable):\n    update_interval: int = 60 * 60",
        "class IndexerModel(Observable):\n    update_interval: int = 60 * 60\n"
        "    # Leave blank to use the bundled defaults / plain env vars\n"
        "    # (TMDB_READ_ACCESS_TOKEN, TVDB_API_KEY). Set here to override.\n"
        '    tmdb_read_access_token: str = ""\n'
        '    tvdb_api_key: str = ""',
    ),
    (
        "indexer",
        "program/apis/__init__.py",
        "from .tvdb_api import TVDBAPI",
        "from .trakt_api import TraktAPI, TraktAPIError",
        "from .tmdb_api import TMDBAPI, TMDBAPIError\n"
        "from .trakt_api import TraktAPI, TraktAPIError\n"
        "from .tvdb_api import TVDBAPI, TVDBAPIError",
    ),
    (
        "indexer",
        "program/apis/__init__.py",
        "__setup_tvdb()",
        "    __setup_trakt()\n    __setup_plex()",
        "    __setup_trakt()\n    __setup_tmdb()\n    __setup_tvdb()\n    __setup_plex()",
    ),
    (
        "indexer",
        "program/apis/__init__.py",
        "def __setup_tvdb():",
        "def __setup_plex():",
        "def __setup_tmdb():\n"
        "    tmdbApi = TMDBAPI(settings_manager.settings.indexer.tmdb_read_access_token)\n"
        "    di[TMDBAPI] = tmdbApi\n\n"
        "def __setup_tvdb():\n"
        "    tvdbApi = TVDBAPI(settings_manager.settings.indexer.tvdb_api_key)\n"
        "    di[TVDBAPI] = tvdbApi\n\n"
        "def __setup_plex():",
    ),
    (
        "indexer",
        "program/apis/overseerr_api.py",
        "from program.apis.tmdb_api import TMDBAPI",
        "from program.apis.trakt_api import TraktAPI",
        "from program.apis.tmdb_api import TMDBAPI",
    ),
    (
        "indexer",
        "program/apis/overseerr_api.py",
        "self.tmdb_api = di[TMDBAPI]",
        "self.trakt_api = di[TraktAPI]",
        "self.tmdb_api = di[TMDBAPI]",
    ),
    (
        "indexer",
        "program/apis/overseerr_api.py",
        "self.tmdb_api.get_imdb_id(",
        """        # Try alternate IDs if IMDb ID is not available
        alternate_ids = [("tmdbId", self.trakt_api.get_imdbid_from_tmdb)]
        for id_attr, fetcher in alternate_ids:
            external_id_value = getattr(response.data.externalIds, id_attr, None)
            if external_id_value:
                _type = data.media_type
                if _type == "tv":
                    _type = "show"
                try:
                    new_imdb_id: Union[str, None] = fetcher(external_id_value, type=_type)
                    if not new_imdb_id:
                        continue
                    return new_imdb_id
                except Exception as e:
                    logger.error(f"Error fetching alternate ID: {str(e)}")
                    continue""",
        """        # Fall back to resolving the IMDb id from the TMDB id directly.
        tmdb_id = getattr(response.data.externalIds, "tmdbId", None)
        if tmdb_id:
            try:
                new_imdb_id: Union[str, None] = self.tmdb_api.get_imdb_id(
                    tmdb_id, media_type=data.media_type
                )
                if new_imdb_id:
                    return new_imdb_id
            except Exception as e:
                logger.error(f"Error fetching alternate ID: {str(e)}")

        return None""",
    ),
    # -- 2. Scraper bucket hang -------------------------------------------
    (
        "scraper-bucket",
        "program/utils/request.py",
        "use_memory_list: bool = True",
        "use_memory_list: bool = False",
        "use_memory_list: bool = True",
    ),
    (
        "scraper-bucket",
        "program/utils/request.py",
        "MemoryListBucket applies",
        "    :param use_memory_list: If true, use MemoryListBucket instead of"
        " MemoryQueueBucket for in-memory limiting.\n",
        "    :param use_memory_list: If true, use MemoryListBucket instead of"
        " MemoryQueueBucket for in-memory limiting.\n"
        "        Defaults to True: MemoryQueueBucket wraps a *bounded* Queue whose put() has no timeout, and\n"
        "        requests_ratelimiter._fill_bucket() (called on any 429) pushes filler items without checking\n"
        "        capacity - its own source carries a TODO admitting this. On a full bucket that put() blocks\n"
        "        forever, the scraper worker never returns, Scraping.scrape() waits on it in as_completed()\n"
        "        indefinitely, and the item's DB session is left idle-in-transaction. MemoryListBucket applies\n"
        "        the same rates but drops overflow instead of blocking.\n",
    ),
    # -- 3. AllDebrid v4.1 ------------------------------------------------
    (
        "alldebrid",
        "program/services/downloaders/alldebrid.py",
        'BASE_URL = "https://api.alldebrid.com/v4.1"',
        'BASE_URL = "https://api.alldebrid.com/v4"',
        'BASE_URL = "https://api.alldebrid.com/v4.1"',
    ),
    (
        "alldebrid",
        "program/services/downloaders/alldebrid.py",
        'progress=100 if info.get("statusCode") == 4 else 0',
        'progress=(info["size"] / info["downloaded"]) if info["downloaded"] != 0 else 0',
        'progress=100 if info.get("statusCode") == 4 else 0',
    ),
    # -- 4. RealDebrid error handling -------------------------------------
    (
        "realdebrid",
        "program/services/downloaders/realdebrid.py",
        "response = getattr(e, 'response', None)",
        """            if e.response.status_code == 503:
                logger.debug(f"Failed to add torrent {infohash}: [503] Infringing Torrent or Service Unavailable")
                raise RealDebridError("Infringing Torrent or Service Unavailable")
            elif e.response.status_code == 429:
                logger.debug(f"Failed to add torrent {infohash}: [429] Rate Limit Exceeded")
                raise RealDebridError("Rate Limit Exceeded")
            elif e.response.status_code == 404:
                logger.debug(f"Failed to add torrent {infohash}: [404] Torrent Not Found or Service Unavailable")
                raise RealDebridError("Torrent Not Found or Service Unavailable")
            elif e.response.status_code == 400:
                logger.debug(f"Failed to add torrent {infohash}: [400] Torrent file is not valid")
                raise RealDebridError("Torrent file is not valid")
            elif e.response.status_code == 502:
                logger.debug(f"Failed to add torrent {infohash}: [502] Bad Gateway")
                raise RealDebridError("Bad Gateway")
            else:
                logger.debug(f"Failed to add torrent {infohash}: {e}")
                raise RealDebridError(f"Failed to add torrent {infohash}: {e}")""",
        """            response = getattr(e, 'response', None)
            if response is not None:
                if response.status_code == 503:
                    logger.debug(f"Failed to add torrent {infohash}: [503] Infringing Torrent or Service Unavailable")
                    raise RealDebridError("Infringing Torrent or Service Unavailable")
                elif response.status_code == 429:
                    logger.debug(f"Failed to add torrent {infohash}: [429] Rate Limit Exceeded")
                    raise RealDebridError("Rate Limit Exceeded")
                elif response.status_code == 404:
                    logger.debug(f"Failed to add torrent {infohash}: [404] Torrent Not Found or Service Unavailable")
                    raise RealDebridError("Torrent Not Found or Service Unavailable")
                elif response.status_code == 400:
                    logger.debug(f"Failed to add torrent {infohash}: [400] Torrent file is not valid")
                    raise RealDebridError("Torrent file is not valid")
                elif response.status_code == 402:
                    logger.debug(f"Failed to add torrent {infohash}: [402] Payment Required — hash may be DMCA-blocked on this account")
                    raise RealDebridError("Payment Required — hash may be DMCA-blocked")
                elif response.status_code == 451:
                    logger.debug(f"Failed to add torrent {infohash}: [451] Infringing file (DMCA-blocked)")
                    raise RealDebridError("Infringing file (DMCA-blocked)")
                elif response.status_code == 502:
                    logger.debug(f"Failed to add torrent {infohash}: [502] Bad Gateway")
                    raise RealDebridError("Bad Gateway")
                else:
                    logger.debug(f"Failed to add torrent {infohash}: [{response.status_code}] {e}")
                    raise RealDebridError(f"Failed to add torrent {infohash}: {e}")
            logger.debug(f"Failed to add torrent {infohash}: {e}")
            raise RealDebridError(f"Failed to add torrent {infohash}: {e}")""",
    ),
    # -- 5. Overseerr efficiency ------------------------------------------
    (
        "overseerr",
        "program/apis/overseerr_api.py",
        'imdb_id = getattr(item.media, "imdbId", None)',
        "            imdb_id = self.get_imdb_id(item.media)",
        "            # Try to get IMDb ID from the request response directly first (more efficient)\n"
        '            imdb_id = getattr(item.media, "imdbId", None)\n'
        "            if not imdb_id:\n"
        "                # Fallback: fetch from Overseerr API\n"
        "                imdb_id = self.get_imdb_id(item.media)",
    ),
]


def _log(level, message):
    if logger is not None:
        getattr(logger, level)(message)
    else:
        print(message)


def _copy_new_files(src_root):
    """Drop in the modules that have no upstream counterpart."""
    copied = []
    for source in sorted(FILES.rglob("*.py")):
        relative = source.relative_to(FILES)
        target = src_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(relative))
    return copied


def _apply_patches(src_root):
    """Rewrite the upstream call sites. Returns (applied, skipped, failures)."""
    applied, skipped, failures = [], [], []
    for group, relative, marker, old, new in PATCHES:
        label = f"[{group}] {relative}"
        path = src_root / relative
        if not path.exists():
            failures.append(f"{label}: file not found at {path}")
            continue

        text = path.read_text()
        if marker in text:
            skipped.append(label)
            continue
        if old not in text:
            first_line = old.strip().splitlines()[0][:70]
            failures.append(
                f"{label}: anchor not found, upstream changed this code: {first_line}"
            )
            continue

        path.write_text(text.replace(old, new))
        applied.append(label)
    return applied, skipped, failures


def apply_riven_patches(config_dir=DEFAULT_CONFIG_DIR):
    """Apply the traktless patch set to a Riven checkout.

    Returns a ``(success, error)`` tuple, matching the convention the rest of
    DUMB's setup helpers use.
    """
    src_root = Path(config_dir) / "src"
    if not src_root.is_dir():
        return False, f"Riven source not found at {src_root}"

    copied = _copy_new_files(src_root)
    for relative in copied:
        _log("debug", f"Riven patches: copied {relative}")

    applied, skipped, failures = _apply_patches(src_root)
    for label in applied:
        _log("debug", f"Riven patches: patched {label}")
    for label in skipped:
        _log("debug", f"Riven patches: already applied {label}")

    if failures:
        for failure in failures:
            _log("error", f"Riven patches: {failure}")
        return False, (
            f"{len(failures)} Riven patch(es) failed to apply; "
            "upstream Riven likely changed. Review the errors above."
        )

    _log(
        "info",
        f"Applied traktless Riven patch set: {len(copied)} module(s) copied, "
        f"{len(applied)} patch(es) applied, {len(skipped)} already present.",
    )
    return True, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help=f"Riven backend directory containing src/ (default: {DEFAULT_CONFIG_DIR})",
    )
    args = parser.parse_args(argv)

    success, error = apply_riven_patches(args.config_dir)
    if not success:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    print("Done. Restart Riven Backend to load the patched code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
