"""TMDB/OMDB lookups, rating resolution, title helpers, display formatting."""
import time
import re

from . import rt
from . import images


def _provider_id(providers, *keys):
    """Try multiple key spellings and return the first non-empty value."""
    for k in keys:
        v = providers.get(k)
        if v:
            return v
    return None


def get_omdb_rating(imdb_id=None, title=None, year=None, media_type="series"):
    if not rt.OMDB_KEY:
        return None

    cache_key = imdb_id or f"{title}:{year}"
    if not cache_key:
        return None

    if cache_key in rt.omdb_cache:
        cached_rating, cached_time = rt.omdb_cache[cache_key]
        if time.time() - cached_time < rt.OMDB_CACHE_TTL:
            rt.omdb_cache.move_to_end(cache_key)
            return cached_rating

    rating = None
    try:
        if imdb_id:
            params = {"apikey": rt.OMDB_KEY, "i": imdb_id}
            r = rt.http.get("https://www.omdbapi.com/", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    raw = data.get("imdbRating", "N/A")
                    rating = float(raw) if raw not in ("N/A", "", None) else None
                    rt.log(f"OMDB by ID {imdb_id} → {rating}")

        if rating is None and title:
            params = {"apikey": rt.OMDB_KEY, "t": title, "type": media_type}
            if year:
                params["y"] = year
            r = rt.http.get("https://www.omdbapi.com/", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    raw = data.get("imdbRating", "N/A")
                    rating = float(raw) if raw not in ("N/A", "", None) else None
                    rt.log(f"OMDB by title '{title}' ({year}) → {rating}")
                else:
                    rt.log(f"OMDB title search failed: {data.get('Error')} for '{title}'")

    except Exception as e:
        rt.log(f"OMDB fetch error: {e}")

    if len(rt.omdb_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.omdb_cache)
    rt.omdb_cache[cache_key] = (rating, time.time())
    return rating


def get_tmdb_rating(tmdb_id=None, title=None, year=None, media_type="tv"):
    if not rt.TMDB_KEY:
        return None

    cache_key = f"tmdb:{tmdb_id or f'{title}:{year}'}"
    if cache_key in rt.tmdb_cache:
        cached_rating, cached_time = rt.tmdb_cache[cache_key]
        if time.time() - cached_time < rt.OMDB_CACHE_TTL:
            rt.tmdb_cache.move_to_end(cache_key)
            return cached_rating

    rating = None
    params = {"api_key": rt.TMDB_KEY, "language": "en-US"}
    try:
        if tmdb_id:
            url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
            r = rt.http.get(url, params=params, timeout=10)
            if r.status_code == 200:
                avg = r.json().get("vote_average")
                rating = float(avg) if avg else None
                rt.log(f"TMDB by ID {tmdb_id} ({media_type}) → {rating}")

        if rating is None and title:
            search_url = f"https://api.themoviedb.org/3/search/{media_type}"
            year_param = "first_air_date_year" if media_type == "tv" else "year"
            search_params = {**params, "query": title}
            if year:
                search_params[year_param] = year
            r = rt.http.get(search_url, params=search_params, timeout=10)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    avg = results[0].get("vote_average")
                    rating = float(avg) if avg else None
                    rt.log(f"TMDB search '{title}' ({year}) → {rating}")
                else:
                    rt.log(f"TMDB search: no results for '{title}'")
    except Exception as e:
        rt.log(f"TMDB fetch error: {e}")

    if len(rt.tmdb_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.tmdb_cache)
    rt.tmdb_cache[cache_key] = (rating, time.time())
    return rating


def get_tmdb_media_info(title, media_type="tv", season=None):
    if not rt.TMDB_KEY or not title or is_generic_netflix_title(title):
        return {}

    cache_key = f"netflix:{media_type}:{title.lower()}:s{season}"
    if cache_key in rt.netflix_meta_cache:
        cached_data, cached_time = rt.netflix_meta_cache[cache_key]
        if time.time() - cached_time < rt.POSTER_CACHE_TTL:
            rt.netflix_meta_cache.move_to_end(cache_key)
            return cached_data

    info = {}
    params = {"api_key": rt.TMDB_KEY, "language": "en-US"}
    try:
        search_url = f"https://api.themoviedb.org/3/search/{media_type}"
        r = rt.http.get(search_url, params={**params, "query": title}, timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                result = next((candidate for candidate in results if tmdb_title_matches(title, candidate, media_type)), None)
                if not result:
                    rt.log(f"Netflix TMDB {media_type} search rejected loose matches for '{title}'")
                    if len(rt.netflix_meta_cache) >= rt.CACHE_MAX_SIZE:
                        rt._evict_oldest(rt.netflix_meta_cache)
                    rt.netflix_meta_cache[cache_key] = ({}, time.time())
                    return {}
                tmdb_id = result.get("id")
                details_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
                details = {}
                if tmdb_id:
                    d = rt.http.get(details_url, params=params, timeout=10)
                    if d.status_code == 200:
                        details = d.json()

                poster_path = result.get("poster_path") or details.get("poster_path")

                # Prefer the season-specific poster when we know the season.
                if media_type == "tv" and season and tmdb_id:
                    try:
                        s = rt.http.get(
                            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{int(season)}",
                            params=params, timeout=10
                        )
                        if s.status_code == 200:
                            season_poster = s.json().get("poster_path")
                            if season_poster:
                                poster_path = season_poster
                    except Exception as e:
                        rt.log(f"Netflix TMDB season poster error: {e}")

                genres = [g.get("name") for g in details.get("genres", []) if g.get("name")]
                runtime = None
                official_rating = None

                if media_type == "tv":
                    runtimes = details.get("episode_run_time") or []
                    runtime = runtimes[0] * 60 if runtimes else None
                    year = (details.get("first_air_date") or result.get("first_air_date") or "")[:4] or None
                else:
                    runtime_min = details.get("runtime")
                    runtime = runtime_min * 60 if runtime_min else None
                    year = (details.get("release_date") or result.get("release_date") or "")[:4] or None

                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
                if poster_url and rt.UPLOAD_ENABLED:
                    uploaded_poster = images.upload_image(poster_url, square=True)
                    if uploaded_poster:
                        poster_url = uploaded_poster

                info = {
                    "tmdb_id": tmdb_id,
                    "rating": float(result.get("vote_average")) if result.get("vote_average") else None,
                    "poster": poster_url,
                    "genres": genres,
                    "runtime": runtime,
                    "official_rating": official_rating,
                    "year": int(year) if year and year.isdigit() else None,
                }
                rt.log(f"Netflix TMDB {media_type} search '{title}'{f' S{season}' if season else ''} → poster={'yes' if info.get('poster') else 'no'} rating={info.get('rating')}")
    except Exception as e:
        rt.log(f"Netflix TMDB lookup error: {e}")

    if len(rt.netflix_meta_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.netflix_meta_cache)
    rt.netflix_meta_cache[cache_key] = (info, time.time())
    return info


def normalize_lookup_title(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def is_generic_netflix_title(value):
    normalized = (value or "").strip().lower()
    return normalized in (
        "", "netflix", "watching netflix", "now playing",
        "disney+", "disney plus", "disneyplus", "watching disney+",
        "tv 2 play", "tv2 play", "tv 2", "tv2",
    )


def tmdb_title_matches(query, candidate, media_type):
    query_norm = normalize_lookup_title(query)
    names = [
        candidate.get("name"),
        candidate.get("original_name"),
        candidate.get("title"),
        candidate.get("original_title"),
    ]
    long_enough = len(query_norm) > 7
    for name in names:
        name_norm = normalize_lookup_title(name)
        if name_norm and (name_norm == query_norm or (long_enough and (query_norm in name_norm or name_norm in query_norm))):
            return True
    return False


def resolve_rating(community_rating, official_rating, tmdb_id, imdb_id, title, year, media_type="tv", critic_rating=None):
    """Pick a rating, trying each source in the user-configured rt.RATING_ORDER and
    returning the first that yields a value. Lookups are lazy: a source is only
    queried (network calls for tmdb/omdb) when the order reaches it."""
    rt.debug(f"resolve_rating: title='{title}' year={year} type={media_type} | emby={community_rating} tmdb_id={tmdb_id} imdb_id={imdb_id} critic={critic_rating} order={rt.RATING_ORDER}")

    omdb_type = "series" if media_type == "tv" else "movie"

    def _emby():
        return community_rating if community_rating is not None else None

    def _tmdb():
        return get_tmdb_rating(tmdb_id=tmdb_id, title=title, year=year, media_type=media_type)

    def _omdb():
        return get_omdb_rating(imdb_id=imdb_id, title=title, year=year, media_type=omdb_type)

    def _critic():
        if critic_rating is not None and critic_rating > 0:
            return round(critic_rating / 10.0, 1)
        return None

    sources = {
        "emby": _emby, "community": _emby,
        "tmdb": _tmdb,
        "omdb": _omdb, "imdb": _omdb,
        "critic": _critic,
    }

    for key in rt.RATING_ORDER:
        fn = sources.get(key)
        if fn is None:
            continue
        rating = fn()
        if rating is not None:
            rt.debug(f"  → Using {key} rating: {rating}")
            return rating

    rt.debug("  → No rating found from any source")
    return None


def get_series_info(series_id):
    if not series_id:
        return [], None, None, None, None, None, None

    if series_id in rt.series_cache:
        cached_data, cached_time = rt.series_cache[series_id]
        if time.time() - cached_time < rt.POSTER_CACHE_TTL:
            rt.series_cache.move_to_end(series_id)
            d = cached_data
            return d["genres"], d["community_rating"], d["official_rating"], d["imdb_id"], d["tmdb_id"], d.get("critic_rating"), d.get("year")

    try:
        r = rt.http.get(
            f"{rt.SERVER}/Items/{series_id}",
            headers=rt.headers,
            params={"Fields": "Genres,CommunityRating,OfficialRating,CriticRating,ProviderIds,ProductionYear"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            providers = data.get("ProviderIds") or {}
            imdb_id = _provider_id(providers, "Imdb", "IMDB", "imdb")
            tmdb_id = _provider_id(providers, "Tmdb", "TheMovieDb", "Tmdb", "tmdb")
            info = {
                "genres":           data.get("Genres", []),
                "community_rating": data.get("CommunityRating"),
                "official_rating":  data.get("OfficialRating"),
                "critic_rating":    data.get("CriticRating"),
                "year":             data.get("ProductionYear"),
                "imdb_id":          imdb_id,
                "tmdb_id":          tmdb_id,
            }
            rt.log(f"Series info: community={info['community_rating']}, official={info['official_rating']}, critic={info['critic_rating']}, year={info['year']}, imdb={info['imdb_id']}, tmdb={info['tmdb_id']}, raw_providers={list(providers.keys())}")
            if len(rt.series_cache) >= rt.CACHE_MAX_SIZE:
                rt._evict_oldest(rt.series_cache)
            rt.series_cache[series_id] = (info, time.time())
            return info["genres"], info["community_rating"], info["official_rating"], info["imdb_id"], info["tmdb_id"], info["critic_rating"], info["year"]
    except Exception as e:
        rt.log(f"Series info fetch error: {e}")

    return [], None, None, None, None, None, None


def get_season_rating(season_id):
    """Fetch community/critic rating for a season (cached). Returns (community, critic)."""
    if not season_id:
        return None, None

    if season_id in rt.season_cache:
        community, critic, cached_time = rt.season_cache[season_id]
        if time.time() - cached_time < rt.POSTER_CACHE_TTL:
            rt.season_cache.move_to_end(season_id)
            return community, critic

    try:
        r = rt.http.get(
            f"{rt.SERVER}/Items/{season_id}",
            headers=rt.headers,
            params={"Fields": "CommunityRating,CriticRating"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            community = data.get("CommunityRating")
            critic    = data.get("CriticRating")
            rt.log(f"Season rating ({season_id}): community={community}, critic={critic}")
            if len(rt.season_cache) >= rt.CACHE_MAX_SIZE:
                rt._evict_oldest(rt.season_cache)
            rt.season_cache[season_id] = (community, critic, time.time())
            return community, critic
    except Exception as e:
        rt.log(f"Season rating fetch error: {e}")

    if len(rt.season_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.season_cache)
    rt.season_cache[season_id] = (None, None, time.time())
    return None, None


def get_item_rating(item_id):
    try:
        r = rt.http.get(
            f"{rt.SERVER}/Items/{item_id}",
            headers=rt.headers,
            params={"Fields": "CommunityRating,OfficialRating,CriticRating,ProviderIds"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            providers = data.get("ProviderIds") or {}
            community = data.get("CommunityRating")
            official  = data.get("OfficialRating")
            critic    = data.get("CriticRating")
            imdb_id   = _provider_id(providers, "Imdb", "IMDB", "imdb")
            tmdb_id   = _provider_id(providers, "Tmdb", "TheMovieDb", "tmdb")
            rt.log(f"Item rating fetch: community={community}, official={official}, critic={critic}, imdb={imdb_id}, tmdb={tmdb_id}, raw_providers={list(providers.keys())}")
            return community, official, imdb_id, tmdb_id, critic
    except Exception as e:
        rt.log(f"Item rating fetch error: {e}")
    return None, None, None, None, None


def format_runtime(seconds):
    if not seconds or seconds <= 0:
        return None
    total_min = int(seconds // 60)
    if total_min < 60:
        return f"{total_min} min"
    hours = total_min // 60
    mins  = total_min % 60
    return f"{hours}h {mins}min" if mins else f"{hours}h"


def build_large_text(community_rating, official_rating, runtime_seconds):
    runtime_str = format_runtime(runtime_seconds)
    if community_rating:
        rating_str = f"⭐ {community_rating:.1f}"
    elif official_rating:
        rating_str = official_rating
    else:
        rating_str = None

    if rating_str and runtime_str:
        return f"{rating_str} • {runtime_str}"
    elif rating_str:
        return rating_str
    elif runtime_str:
        return runtime_str
    return "Emby"
