"""
Bandcamp Client for MCP Server
Uses web scraping / Bandcamp's internal JSON APIs since there is no public API.

Bandcamp has moved search, tag browsing and discovery from server-rendered HTML
to JS apps backed by internal JSON endpoints. This client targets those
endpoints directly:
  - search  -> POST /api/bcsearch_public_api/1/autocomplete_elastic
  - tag     -> POST /api/discover/1/discover_web (filtered by tag)
  - discover-> POST /api/discover/1/discover_web
Album and track pages still expose stable JSON-LD, which we parse. Artist
discography lives in the /music grid.
"""
import re
import json
import logging
from urllib.parse import quote_plus, urljoin, urlsplit
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BANDCAMP_BASE = "https://bandcamp.com"
SEARCH_API = f"{BANDCAMP_BASE}/api/bcsearch_public_api/1/autocomplete_elastic"
DISCOVER_API = f"{BANDCAMP_BASE}/api/discover/1/discover_web"

# Bandcamp discover "slice" (sort) slugs.
_SLICE_MAP = {"pop": "top", "top": "top", "new": "new", "rec": "rand", "rand": "rand"}
# Physical/format filter -> discover category id.
_CATEGORY_MAP = {"all": 0, "digital": 1, "vinyl": 2, "cd": 3, "cassette": 4}
# Search result type code -> human label.
_TYPE_LABELS = {"a": "album", "t": "track", "b": "artist", "l": "label", "f": "fan"}


class BandcampClient:
    """Client for Bandcamp data extraction."""

    def __init__(self, user_agent: str | None = None):
        # A browser-like UA is required: Bandcamp serves a challenge shell to
        # unknown agents on some endpoints.
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

    async def _fetch(self, url: str) -> str:
        """Fetch a URL and return the HTML content."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=self.headers, timeout=30.0, follow_redirects=True
            )
            response.raise_for_status()
            return response.text

    async def _post_json(self, url: str, payload: dict) -> dict:
        """POST a JSON payload to a Bandcamp internal API and return the JSON."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _clean_url(url: str) -> str:
        """Drop Bandcamp's tracking query (?from=discover_page etc.)."""
        return url.split("?")[0] if url else url

    async def search(
        self,
        query: str,
        item_type: str = "all",  # all, album, artist, track, label
        page: int = 1,
    ) -> dict:
        """Search Bandcamp via the autocomplete API.

        Note: the API returns a single ranked batch (~50 items); it does not
        paginate, so ``page`` is accepted for compatibility but not used.
        """
        # search_filter narrows server-side where supported; "b" covers both
        # artists and labels, which we split client-side via is_label.
        filter_map = {
            "all": "",
            "album": "a",
            "track": "t",
            "artist": "b",
            "label": "b",
        }
        search_filter = filter_map.get(item_type, "")

        data = await self._post_json(
            SEARCH_API,
            {
                "search_text": query,
                "search_filter": search_filter,
                "full_page": True,
                "fan_id": None,
            },
        )
        raw = data.get("auto", {}).get("results", [])

        results = []
        for item in raw:
            code = item.get("type")
            is_label = item.get("is_label", False)

            # Honor artist/label distinction the API filter can't express.
            if item_type == "artist" and (code != "b" or is_label):
                continue
            if item_type == "label" and (code != "b" or not is_label):
                continue

            label = "label" if (code == "b" and is_label) else _TYPE_LABELS.get(code, "unknown")
            # Bands use item_url_root; albums/tracks use item_url_path.
            url = item.get("item_url_path") or item.get("item_url_root", "")

            result = {
                "type": label,
                "title": item.get("name", ""),
                "url": url,
                "tags": item.get("tag_names", []) or [],
            }
            if item.get("band_name"):
                result["subhead"] = item["band_name"]
            elif item.get("location"):
                result["subhead"] = item["location"]
            if item.get("genre_name"):
                result["genre"] = item["genre_name"]
            if item.get("img"):
                result["image"] = item["img"]

            if result["title"]:
                results.append(result)

        return {"results": results, "pagination": {"page": page, "items": len(results)}}

    async def get_album(self, url: str) -> dict:
        """Get album details from a Bandcamp album URL (JSON-LD, HTML fallback)."""
        html = await self._fetch(url)
        soup = BeautifulSoup(html, "html.parser")

        album = {"url": url}

        json_ld = soup.select_one('script[type="application/ld+json"]')
        if json_ld:
            try:
                data = json.loads(json_ld.string)
                if isinstance(data, dict):
                    album["title"] = data.get("name", "")
                    album["artist"] = data.get("byArtist", {}).get("name", "")
                    album["description"] = data.get("description", "")
                    album["release_date"] = data.get("datePublished", "")
                    album["image"] = data.get("image", "")
                    album["num_tracks"] = data.get("numTracks", 0)

                    tracks = data.get("track", {}).get("itemListElement", [])
                    album["tracks"] = []
                    for t in tracks:
                        track_item = t.get("item", {})
                        album["tracks"].append({
                            "position": t.get("position", 0),
                            "title": track_item.get("name", ""),
                            "duration": track_item.get("duration", ""),
                            "url": track_item.get("@id", ""),
                        })

                    offers = data.get("offers", {})
                    if offers:
                        album["price"] = offers.get("price", "")
                        album["currency"] = offers.get("priceCurrency", "")

                    publisher = data.get("publisher", {})
                    if publisher:
                        album["label"] = publisher.get("name", "")
                        album["label_url"] = publisher.get("@id", "")

                    # JSON-LD keywords are a reliable tag source.
                    kw = data.get("keywords")
                    if isinstance(kw, list):
                        album["tags"] = [k for k in kw if k]
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                logger.warning(f"Failed to parse album JSON-LD: {e}")

        # HTML fallbacks.
        if not album.get("title"):
            title_elem = soup.select_one("#name-section .trackTitle")
            if title_elem:
                album["title"] = title_elem.get_text(strip=True)
        if not album.get("artist"):
            artist_elem = soup.select_one("#name-section a")
            if artist_elem:
                album["artist"] = artist_elem.get_text(strip=True)
        if not album.get("tags"):
            tags = soup.select(".tralbum-tags a.tag")
            album["tags"] = [t.get_text(strip=True) for t in tags]

        about = soup.select_one(".tralbum-about")
        if about:
            album["about"] = about.get_text(strip=True)
        credits = soup.select_one(".tralbum-credits")
        if credits:
            album["credits"] = credits.get_text(strip=True)

        return album

    async def get_artist(self, url: str) -> dict:
        """Get artist/label info; discography is read from the /music grid."""
        html = await self._fetch(url)
        soup = BeautifulSoup(html, "html.parser")

        artist = {"url": url}

        name_elem = soup.select_one("#band-name-location .title")
        if name_elem:
            artist["name"] = name_elem.get_text(strip=True)
        location_elem = soup.select_one("#band-name-location .location")
        if location_elem:
            artist["location"] = location_elem.get_text(strip=True)
        bio_elem = soup.select_one("#bio-text") or soup.select_one(".bio-text")
        if bio_elem:
            artist["bio"] = bio_elem.get_text(strip=True)

        links = []
        for link in soup.select("#band-links li a"):
            links.append({"name": link.get_text(strip=True), "url": link.get("href", "")})
        artist["links"] = links

        # Discography lives on the /music page. Fetch it if the current page
        # isn't already showing the grid.
        artist["discography"] = self._parse_music_grid(soup, url)
        if not artist["discography"]:
            parts = urlsplit(url)
            music_url = f"{parts.scheme}://{parts.netloc}/music"
            try:
                music_soup = BeautifulSoup(await self._fetch(music_url), "html.parser")
                artist["discography"] = self._parse_music_grid(music_soup, music_url)
                if not artist.get("name"):
                    nm = music_soup.select_one("#band-name-location .title")
                    if nm:
                        artist["name"] = nm.get_text(strip=True)
            except httpx.HTTPError as e:
                logger.warning(f"Failed to fetch /music discography: {e}")

        return artist

    @staticmethod
    def _parse_music_grid(soup: BeautifulSoup, base_url: str) -> list:
        """Extract releases from an artist page's #music-grid."""
        albums = []
        for item in soup.select("#music-grid li.music-grid-item, li.music-grid-item"):
            album = {}
            link = item.select_one("a")
            if link and link.get("href"):
                album["url"] = urljoin(base_url, link["href"])
            title = item.select_one(".title")
            if title:
                album["title"] = title.get_text(strip=True)
            img = item.select_one("img")
            if img:
                album["image"] = img.get("src", "") or img.get("data-original", "")
            if album.get("title"):
                albums.append(album)
        return albums

    async def get_track(self, url: str) -> dict:
        """Get track details from a Bandcamp track URL (JSON-LD)."""
        html = await self._fetch(url)
        soup = BeautifulSoup(html, "html.parser")

        track = {"url": url}

        json_ld = soup.select_one('script[type="application/ld+json"]')
        if json_ld:
            try:
                data = json.loads(json_ld.string)
                if isinstance(data, dict):
                    track["title"] = data.get("name", "")
                    track["artist"] = data.get("byArtist", {}).get("name", "")
                    track["duration"] = data.get("duration", "")
                    track["description"] = data.get("description", "")
                    track["release_date"] = data.get("datePublished", "")
                    track["image"] = data.get("image", "")

                    in_album = data.get("inAlbum", {})
                    if in_album:
                        track["album"] = in_album.get("name", "")
                        track["album_url"] = in_album.get("@id", "")

                    offers = data.get("offers", {})
                    if offers:
                        track["price"] = offers.get("price", "")
                        track["currency"] = offers.get("priceCurrency", "")

                    kw = data.get("keywords")
                    if isinstance(kw, list):
                        track["tags"] = [k for k in kw if k]
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                logger.warning(f"Failed to parse track JSON-LD: {e}")

        if not track.get("title"):
            title_elem = soup.select_one("#name-section .trackTitle")
            if title_elem:
                track["title"] = title_elem.get_text(strip=True)
        if not track.get("tags"):
            tags = soup.select(".tralbum-tags a.tag")
            track["tags"] = [t.get_text(strip=True) for t in tags]

        lyrics = soup.select_one(".lyricsText")
        if lyrics:
            track["lyrics"] = lyrics.get_text(strip=True)

        return track

    async def _discover(
        self, tags: list, slice_: str, category_id: int, size: int = 40
    ) -> list:
        """Call the discover_web API and normalize album results."""
        data = await self._post_json(
            DISCOVER_API,
            {
                "category_id": category_id,
                "tag_norm_names": tags,
                "geoname_id": 0,
                "slice": slice_,
                "time_facet_id": None,
                "cursor": "*",
                "size": size,
                "include_result_types": ["a"],
                "followed_bands": False,
            },
        )
        if data.get("__api_special__") == "exception":
            raise RuntimeError(f"Bandcamp discover error: {data.get('error_type')}")

        albums = []
        for r in data.get("results", []):
            album = {
                "title": r.get("title", ""),
                "artist": r.get("band_name") or r.get("album_artist", ""),
                "url": self._clean_url(r.get("item_url", "")),
                "location": r.get("band_location", ""),
                "release_date": r.get("release_date", ""),
                "track_count": r.get("track_count", 0),
            }
            img = r.get("primary_image") or {}
            if img.get("image_id"):
                album["image"] = f"https://f4.bcbits.com/img/a{img['image_id']}_16.jpg"
            price = r.get("price") or {}
            if price.get("is_money"):
                album["price"] = price.get("amount")
                album["currency"] = price.get("currency")
            if album["title"]:
                albums.append(album)
        return albums

    async def get_tag_page(
        self, tag: str, sort: str = "pop", page: int = 1
    ) -> dict:
        """Browse releases for a tag/genre via the discover API."""
        albums = await self._discover(
            tags=[tag.strip().lower().replace(" ", "-")],
            slice_=_SLICE_MAP.get(sort, "top"),
            category_id=0,
        )
        return {"tag": tag, "sort": sort, "page": page, "albums": albums}

    async def discover(
        self,
        genre: str = "",
        subgenre: str = "",
        sort: str = "top",
        format: str = "all",
        location: int = 0,  # kept for API compatibility (unused)
    ) -> dict:
        """Discover new music via the discover API."""
        tags = []
        for g in (genre, subgenre):
            g = (g or "").strip().lower().replace(" ", "-")
            if g:
                tags.append(g)
        albums = await self._discover(
            tags=tags,
            slice_=_SLICE_MAP.get(sort, "top"),
            category_id=_CATEGORY_MAP.get(format, 0),
        )
        for a in albums:
            a.setdefault("genre", genre)
        return {"genre": genre, "subgenre": subgenre, "sort": sort, "albums": albums}
