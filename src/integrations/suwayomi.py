import gi
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, GObject
try:
    gi.require_version('Gly', '2')
    gi.require_version('GlyGtk4', '2')
    from gi.repository import Gly, GlyGtk4
except (ValueError, ImportError):
    # glycin only exists on Linux; elsewhere makeTexture falls back to Pillow.
    Gly = GlyGtk4 = None

import asyncio
import enum
import json
import os
from pathlib import Path
from urllib.parse import urljoin

import httpx
from babel import Locale
from diskcache import Cache
from gql import Client
from gql.transport.aiohttp import AIOHTTPTransport

from . import models
from .queries import *

MODEL_CLASSES = {
    'Manga': models.Manga,
    'Chapter': models.Chapter,
    'Extension': models.Extension,
    'Source': models.Source,
}

FETCH_CONNECTIONS = 8
DECODE_WORKERS = max(2, min(4, os.cpu_count() or 2))
IMAGE_TIMEOUT = 10
IMAGE_RETRIES = 1
CACHE_SIZE_LIMIT = 500 * 1024 * 1024
GRAPHQL_TIMEOUT = 20
LOCAL_SOURCE_ID = "0"

READING_MODES = {
    0: ("single", "vertical"),
    1: ("double", "vertical"),
    2: ("webtoon", "vertical"),
    3: ("webtoon", "horizontal"),
    4: ("webtoon", "vertical"),
}

GLOBAL_MODE_VALUES = {"single": 0, "double": 1}


class SourceMangaType(enum.StrEnum):
    SEARCH = "SEARCH"
    POPULAR = "POPULAR"
    LATEST = "LATEST"


def readingModeValue(mode: str, orientation: str) -> int:
    if mode in GLOBAL_MODE_VALUES:
        return GLOBAL_MODE_VALUES[mode]
    return 3 if orientation == "horizontal" else 4


def makeClient(url: str) -> Client:
    transport = AIOHTTPTransport(timeout=GRAPHQL_TIMEOUT, url=url.rstrip('/') + '/api/graphql')
    return Client(transport=transport, fetch_schema_from_transport=False)


class Suwayomi(GObject.Object):
    __gtype_name__ = 'KaghezIntegrationSuwayomi'

    MODE_KEY = "webUI_readingMode"
    DIRECTION_KEY = "webUI_readingDirection"
    LANGUAGES_KEY = "webUI_browseLanguages"

    url = GObject.Property(type=str, default="http://localhost:4567")
    downloader_state = GObject.Property(type=str, default="STOPPED")
    download_queue = GObject.Property(type=Gio.ListStore)
    library = GObject.Property(type=Gio.ListStore)

    def __init__(self):
        super().__init__()
        self.loaded_models = {}
        self.pending = {}
        self.session = None
        self.connect_lock = asyncio.Lock()
        self.client = makeClient(self.url)

        self.http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                retries=IMAGE_RETRIES,
                limits=httpx.Limits(max_connections=FETCH_CONNECTIONS),
            ),
            timeout=httpx.Timeout(IMAGE_TIMEOUT, pool=None),
            follow_redirects=True,
        )
        self.decode_slots = asyncio.Semaphore(DECODE_WORKERS)
        self.cache = Cache(
            str(Path(GLib.get_user_cache_dir()) / "kaghez" / "images"),
            size_limit=CACHE_SIZE_LIMIT,
            eviction_policy="least-recently-used",
        )

        self.download_queue = Gio.ListStore.new(item_type=models.Download)
        self.library = Gio.ListStore.new(item_type=models.Manga)
        self.sources = Gio.ListStore.new(item_type=models.Source)
        self.extensions = Gio.ListStore.new(item_type=models.Extension)

        self.all_sources = []
        self.languages = ["en", "all"]

    def reconnect(self, url: str):
        self.session = None
        self.url = url
        self.client = makeClient(url)

    async def close(self):
        if self.session is not None:
            await self.client.close_async()
            self.session = None
        await self.http.aclose()
        await asyncio.to_thread(self.cache.close)

    async def getSession(self):
        if self.session is None:
            async with self.connect_lock:
                if self.session is None:
                    self.session = await self.client.connect_async()
        return self.session

    async def query(self, gql_query, variable_values=None, raise_errors=False, retries=1) -> dict:
        for attempt in range(retries + 1):
            try:
                session = await self.getSession()
                return await session.execute(gql_query, variable_values=variable_values) or {}
            except TimeoutError:
                if attempt < retries:
                    continue
                print(f"[query] timed out after {attempt + 1} attempt(s)")
                if raise_errors:
                    raise
                return {}
            except Exception as e:
                print(f"[query] request failed: {type(e).__name__}: {e}")
                if raise_errors:
                    raise
                return {}
        return {}

    async def shared(self, key, factory):
        if key not in self.pending:
            task = asyncio.create_task(factory())
            task.add_done_callback(lambda done: self.pending.pop(key, None))
            self.pending[key] = task
        return await asyncio.shield(self.pending[key])

    async def loadBytes(self, url: str) -> bytes | None:
        cached = await asyncio.to_thread(self.cache.get, url)
        if cached is not None:
            return cached

        try:
            response = await self.http.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            print(f"[loadBytes] request failed for {url}: {type(e).__name__}: {e}")
            return None

        await asyncio.to_thread(self.cache.set, url, response.content)
        return response.content

    async def getPaintable(self, url: str) -> Gdk.Paintable | None:
        raw_bytes = await self.shared(('bytes', url), lambda: self.loadBytes(url))
        if not raw_bytes:
            return None
        try:
            async with self.decode_slots:
                return await asyncio.to_thread(self.makeTexture, raw_bytes)
        except GLib.Error as e:
            print(f"[getPaintable] decode failed for {url}: {e.message}")
            await asyncio.to_thread(self.cache.delete, url)
            return None

    def makeTexture(self, raw_bytes: bytes) -> Gdk.Texture:
        gbytes = GLib.Bytes.new(raw_bytes)
        try:
            return Gdk.Texture.new_from_bytes(gbytes)
        except GLib.Error:
            if Gly is None:
                return self.makeTextureWithPillow(raw_bytes)
            loader = Gly.Loader.new_for_bytes(gbytes)
            frame = loader.load().next_frame()
            return GlyGtk4.frame_get_texture(frame)

    def makeTextureWithPillow(self, raw_bytes: bytes) -> Gdk.Texture:
        """Decode formats GTK can't (WebP, AVIF...) when glycin isn't available."""
        from io import BytesIO
        from PIL import Image
        try:
            with Image.open(BytesIO(raw_bytes)) as image:
                rgba = image.convert('RGBA')
        except Exception as e:
            # getPaintable only expects GLib.Error from decoding.
            raise GLib.Error(f"Pillow couldn't decode image: {e}")
        return Gdk.MemoryTexture.new(
            rgba.width, rgba.height, Gdk.MemoryFormat.R8G8B8A8,
            GLib.Bytes.new(rgba.tobytes()), rgba.width * 4,
        )

    def getModel(self, model_id: int | str, item_type: str) -> GObject.Object | None:
        return self.loaded_models.get((item_type, model_id))

    def makeModel(self, item: dict, item_type: str) -> GObject.Object | None:
        if item is None:
            return None
        item_id = item.get('pkgName') if item_type == 'Extension' else item.get('id')
        if item_id is None:
            return None

        key = (item_type, item_id)
        model = self.loaded_models.get(key)
        if model is None:
            model = MODEL_CLASSES[item_type]()
            self.loaded_models[key] = model

        fill = {
            'Manga': self.fillManga,
            'Chapter': self.fillChapter,
            'Extension': self.fillExtension,
            'Source': self.fillSource,
        }[item_type]
        fill(model, item)
        return model

    def makeModels(self, nodes: list, item_type: str) -> list:
        return [model for model in (self.makeModel(node, item_type) for node in nodes) if model is not None]

    def makeChapterStore(self, chapter_models: list) -> Gio.ListStore:
        store = Gio.ListStore.new(item_type=models.Chapter)
        store.splice(0, 0, chapter_models)
        return store

    def absoluteUrl(self, path: str | None) -> str:
        return urljoin(self.url, path) if path else ""

    def fillManga(self, model, item: dict):
        model.update_data(
            id=item.get('id'),
            title=item.get('title'),
            author=item.get('author'),
            artist=item.get('artist'),
            description=item.get('description'),
            genre=item.get('genre', []),
            status=item.get('status'),
            initialized=item.get('initialized', False),
            in_library=item.get('inLibrary', False),
            unread_count=item.get('unreadCount', 0),
            first_unread_chapter=self.makeModel(item.get('firstUnreadChapter'), 'Chapter'),
            last_read_chapter=self.makeModel(item.get('lastReadChapter'), 'Chapter'),
            real_url=item.get('realUrl'),
            thumbnail_url=self.absoluteUrl(item.get('thumbnailUrl')),
        )

        chapters = item.get('chapters')
        if chapters is not None:
            if 'totalCount' in chapters:
                model.set_property('total_chapters', chapters['totalCount'])
            nodes = chapters.get('nodes')
            if nodes is not None:
                model.set_property('chapters', self.makeChapterStore(self.makeModels(nodes, 'Chapter')))
        if model.chapters is None:
            model.set_property('chapters', self.makeChapterStore([]))

    def fillChapter(self, model, item: dict):
        values = {name: item[key] for key, name in CHAPTER_FIELDS.items() if key in item}
        model.update_data(id=item.get('id'), **values)

    def fillExtension(self, model, item: dict):
        values = {name: item[key] for key, name in EXTENSION_FIELDS.items() if key in item}
        if 'iconUrl' in item:
            values['icon_url'] = self.absoluteUrl(item.get('iconUrl'))
        model.update_data(pkg_name=item.get('pkgName'), **values)

    def fillSource(self, model, item: dict):
        extension = item.get('extension') or {}
        model.update_data(
            id=item.get('id'),
            name=item.get('name'),
            lang=item.get('lang', ''),
            icon_url=self.absoluteUrl(item.get('iconUrl')),
            is_configurable=item.get('isConfigurable', False),
            is_nsfw=item.get('isNsfw', False),
            extension_pkg_name=extension.get('pkgName', ''),
            extension_name=extension.get('name', ''),
        )

    async def getManga(self, manga_id: int) -> models.Manga | None:
        model = self.getModel(manga_id, 'Manga')
        if model and model.initialized:
            return model
        return await self.shared(('manga', manga_id), lambda: self.fetchManga(manga_id))

    async def fetchManga(self, manga_id: int) -> models.Manga | None:
        result = await self.query(
            FETCH_MANGA,
            variable_values={"id": manga_id, "fetchManga": True, "fetchChapters": True},
        )
        data = result.get('fetchMangaAndChapters', {})
        node = data.get('manga')
        if node is None:
            return self.getModel(manga_id, 'Manga')
        chapters_list = data.get('chapters', [])
        node = {**node, 'chapters': {'nodes': chapters_list, 'totalCount': len(chapters_list)}}
        return self.makeModel(node, 'Manga')

    async def getChapters(
        self,
        manga_id: int,
        first: int | None = None,
        after: str | None = None,
    ) -> list:
        variables = {"mangaId": manga_id, "first": first, "after": after}
        result = await self.query(GET_CHAPTERS_MANGA, variable_values=variables)
        connection = result.get('chapters', {})
        chapter_models = self.makeModels(connection.get('nodes', []), 'Chapter')

        model = self.getModel(manga_id, 'Manga')
        if model is not None:
            model.set_property('chapters', self.makeChapterStore(chapter_models))
            model.set_property('total_chapters', connection.get('totalCount', len(chapter_models)))

        return chapter_models

    async def refreshLibrary(self):
        await self.shared(('library',), self.loadLibrary)

    async def loadLibrary(self):
        result = await self.query(GET_LIBRARY)
        if 'mangas' not in result:
            return
        manga_models = self.makeModels(result['mangas'].get('nodes') or [], 'Manga')
        if list(self.library) != manga_models:
            self.library.splice(0, self.library.get_n_items(), manga_models)

    def syncLibraryMembership(self, model: models.Manga, in_library: bool):
        found, position = self.library.find(model)
        if in_library and not found:
            self.library.append(model)
        elif not in_library and found:
            self.library.remove(position)

    async def refreshExtensions(self):
        await self.shared(('extensions',), self.loadExtensions)

    async def loadExtensions(self):
        result = await self.query(FETCH_EXTENSIONS, retries=2)
        data = result.get('fetchExtensions')
        if data is None:
            return
        extension_models = self.makeModels(data.get('extensions') or [], 'Extension')
        if list(self.extensions) != extension_models:
            self.extensions.splice(0, self.extensions.get_n_items(), extension_models)

    async def refreshSources(self):
        await self.shared(('sources',), self.loadSources)

    async def loadSources(self):
        async with asyncio.TaskGroup() as group:
            sources_task = group.create_task(self.query(GET_SOURCES))
            group.create_task(self.getBrowseLanguages())
        result = sources_task.result()
        if 'sources' not in result:
            return
        self.all_sources = self.makeModels(result['sources'].get('nodes') or [], 'Source')
        self.applyLanguages()

    def applyLanguages(self):
        enabled = [
            source for source in self.all_sources
            if source.lang in self.languages or source.id == LOCAL_SOURCE_ID
        ]
        if list(self.sources) != enabled:
            self.sources.splice(0, self.sources.get_n_items(), enabled)

    async def getBrowseLanguages(self) -> list:
        values = await self.getGlobalMetaValues()
        languages = self.readMeta(values, self.LANGUAGES_KEY)
        if isinstance(languages, list):
            self.languages = languages
        return self.languages

    async def setBrowseLanguages(self, languages: list):
        self.languages = languages
        self.applyLanguages()
        await self.setGlobalMeta(self.LANGUAGES_KEY, languages)

    async def getSourceManga(
        self,
        source_id: str,
        fetch_type: str = SourceMangaType.POPULAR,
        page: int = 1,
        query: str | None = None,
        filters: list | None = None,
    ) -> list:
        variables = {
            "source": source_id,
            "type": str(fetch_type),
            "page": page,
            "query": query,
            "filters": filters or None,
        }
        result = await self.query(GET_SOURCE_MANGA, variable_values=variables, raise_errors=True)
        nodes = result.get('fetchSourceManga', {}).get('mangas', [])
        return self.makeModels(nodes, 'Manga')

    def makeFilter(self, node: dict) -> dict:
        default = None
        for key in ("checkBoxDefault", "selectDefault", "triStateDefault", "textDefault", "sortDefault"):
            if key in node:
                default = node[key]
                break
        return {
            "type": node.get("type"),
            "name": node.get("name") or "",
            "default": default,
            "values": node.get("values") or [],
            "filters": [self.makeFilter(child) for child in node.get("filters") or []],
        }

    async def getSourceFilters(self, source_id: str) -> list:
        result = await self.query(GET_SOURCE_FILTERS, variable_values={"id": source_id})
        nodes = (result.get('source') or {}).get('filters') or []
        return [self.makeFilter(node) for node in nodes]

    async def changeExtension(self, pkg_name: str, **patch):
        model = self.getModel(pkg_name, 'Extension')
        if model:
            model.is_busy = True
        try:
            result = await self.query(UPDATE_EXTENSION, variable_values={"pkgName": pkg_name, **patch})
            node = result.get('updateExtension', {}).get('extension')
            if node:
                self.makeModel(node, 'Extension')
        finally:
            if model:
                model.is_busy = False

    async def installExtension(self, pkg_name: str):
        await self.changeExtension(pkg_name, install=True)

    async def updateExtension(self, pkg_name: str):
        await self.changeExtension(pkg_name, update=True)

    async def uninstallExtension(self, pkg_name: str):
        await self.changeExtension(pkg_name, uninstall=True)

    def makePageModel(self, chapter_id: int, index: int, url: str) -> models.Page:
        return models.Page(
            index=index,
            chapter_id=chapter_id,
            url=urljoin(self.url, url),
        )

    async def getChapterPages(self, chapter_id: int) -> list:
        result = await self.query(FETCH_CHAPTER_PAGES, variable_values={"chapterId": chapter_id})
        data = result.get('fetchChapterPages', {})
        urls = data.get('pages', [])
        page_models = [self.makePageModel(chapter_id, index, url) for index, url in enumerate(urls)]

        chapter_node = data.get('chapter')
        if chapter_node:
            chapter_model = self.getModel(chapter_id, 'Chapter')
            if chapter_model:
                chapter_model.update_data(
                    page_count=chapter_node.get('pageCount', len(urls)),
                    is_downloaded=chapter_node.get('isDownloaded', False),
                )
            manga_node = chapter_node.get('manga')
            if manga_node:
                manga_model = self.getModel(manga_node.get('id'), 'Manga')
                if manga_model:
                    manga_model.set_property('download_count', manga_node.get('downloadCount', 0))

        return page_models

    async def updateChapter(
        self,
        chapter_id: int,
        is_read: bool | None = None,
        is_bookmarked: bool | None = None,
        last_page_read: int | None = None,
    ):
        changes = {
            "is_read": is_read,
            "is_bookmarked": is_bookmarked,
            "last_page_read": last_page_read,
        }
        changes = {name: value for name, value in changes.items() if value is not None}

        variables = {"id": chapter_id}
        if is_read is not None:
            variables["isRead"] = is_read
        if is_bookmarked is not None:
            variables["isBookmarked"] = is_bookmarked
        if last_page_read is not None:
            variables["lastPageRead"] = last_page_read

        model = self.getModel(chapter_id, 'Chapter')
        if model:
            model.update_data(**changes)

        result = await self.query(UPDATE_CHAPTER, variable_values=variables)
        node = result.get('updateChapter', {}).get('chapter')
        if node is None:
            return
        manga_node = node.pop('manga', None)
        self.makeModel(node, 'Chapter')
        if manga_node:
            manga_model = self.getModel(manga_node.get('id'), 'Manga')
            if manga_model:
                manga_model.set_property('unread_count', manga_node.get('unreadCount', manga_model.unread_count))

    async def deleteDownloadedChapter(self, chapter_id: int):
        model = self.getModel(chapter_id, 'Chapter')
        if model:
            model.update_data(is_downloaded=False)

        result = await self.query(DELETE_DOWNLOADED_CHAPTER, variable_values={"id": chapter_id})
        chapters = result.get('deleteDownloadedChapter', {}).get('chapters') or []
        if isinstance(chapters, dict):
            chapters = [chapters]

        for node in chapters:
            manga_node = node.pop('manga', None)
            chapter_model = self.getModel(node.get('id'), 'Chapter')
            if chapter_model:
                chapter_model.set_property('is_downloaded', node.get('isDownloaded', False))
            if manga_node:
                manga_model = self.getModel(manga_node.get('id'), 'Manga')
                if manga_model:
                    manga_model.set_property('download_count', manga_node.get('downloadCount', 0))

    async def setMangaLibrary(self, manga_id: int, in_library: bool):
        model = self.getModel(manga_id, 'Manga')
        if model:
            model.update_data(in_library=in_library)
            self.syncLibraryMembership(model, in_library)

        result = await self.query(
            SET_MANGA_LIBRARY,
            variable_values={"id": manga_id, "inLibrary": in_library},
        )
        node = result.get('updateManga', {}).get('manga')
        if node is None or model is None:
            return
        model.update_data(
            in_library=node.get('inLibrary', in_library),
            in_library_at=node.get('inLibraryAt', ''),
            unread_count=node.get('unreadCount', model.unread_count),
        )
        self.syncLibraryMembership(model, model.in_library)

    async def addToLibrary(self, manga_id: int):
        await self.setMangaLibrary(manga_id, True)

    async def removeFromLibrary(self, manga_id: int):
        await self.setMangaLibrary(manga_id, False)

    def makeDownloadModel(self, item: dict) -> models.Download | None:
        chapter = item.get('chapter') or {}
        manga = item.get('manga') or {}
        chapter_id = chapter.get('id')
        if chapter_id is None:
            return None
        key = ('Download', chapter_id)
        model = self.loaded_models.get(key)
        if model is None:
            model = models.Download()
            self.loaded_models[key] = model
        model.update_data(
            chapter_id=chapter_id,
            chapter_name=chapter.get('name') or "",
            manga_id=manga.get('id') or 0,
            manga_title=manga.get('title') or "",
            progress=item.get('progress') or 0.0,
            state=item.get('state') or "",
            tries=item.get('tries') or 0,
        )
        return model

    async def getDownloadStatus(self):
        result = await self.query(GET_DOWNLOAD_STATUS)
        status = result.get('downloadStatus')
        if not status:
            return
        self.downloader_state = status.get('state') or self.downloader_state

        new_models = [m for m in map(self.makeDownloadModel, status.get('queue') or []) if m is not None]

        current_ids = [m.chapter_id for m in self.download_queue]
        new_ids = [m.chapter_id for m in new_models]
        if current_ids != new_ids:
            self.download_queue.splice(0, len(current_ids), new_models)

    async def downloadCommand(self, gql_query, variables=None):
        await self.query(gql_query, variable_values=variables)
        await self.getDownloadStatus()

    async def enqueueChapterDownload(self, chapter_id: int):
        await self.downloadCommand(ENQUEUE_CHAPTER_DOWNLOAD, {"id": chapter_id})

    async def dequeueChapterDownload(self, chapter_id: int):
        await self.downloadCommand(DEQUEUE_CHAPTER_DOWNLOAD, {"id": chapter_id})

    async def reorderChapterDownload(self, chapter_id: int, to: int):
        await self.downloadCommand(REORDER_CHAPTER_DOWNLOAD, {"chapterId": chapter_id, "to": to})

    async def clearDownloader(self):
        await self.downloadCommand(CLEAR_DOWNLOADER)

    async def startDownloader(self):
        await self.downloadCommand(START_DOWNLOADER)

    async def stopDownloader(self):
        await self.downloadCommand(STOP_DOWNLOADER)

    @staticmethod
    def metaValues(nodes: list | None) -> dict:
        return {meta['key']: meta['value'] for meta in nodes or []}

    def readMeta(self, values: dict, key: str):
        try:
            return json.loads(values[key])
        except (KeyError, json.JSONDecodeError):
            return None

    def parseReaderSettings(self, values: dict) -> dict:
        settings = {}
        mode = self.readMeta(values, self.MODE_KEY)
        if mode in READING_MODES:
            settings["mode"], settings["orientation"] = READING_MODES[mode]
        direction = self.readMeta(values, self.DIRECTION_KEY)
        if direction in (0, 1):
            settings["direction"] = "rtl" if direction == 1 else "ltr"
        return settings

    async def getGlobalMetaValues(self) -> dict:
        result = await self.query(GET_GLOBAL_READER_META)
        return self.metaValues((result.get('metas') or {}).get('nodes'))

    async def getReaderSettings(self, manga_id: int) -> dict:
        result = await self.query(GET_READER_META, variable_values={"id": manga_id})
        values = self.metaValues((result.get('metas') or {}).get('nodes'))
        values.update(self.metaValues((result.get('manga') or {}).get('meta')))
        return self.parseReaderSettings(values)

    async def setReaderSettings(self, manga_id: int, settings: dict):
        mode = readingModeValue(settings["mode"], settings["orientation"])
        direction = 1 if settings["direction"] == "rtl" else 0
        async with asyncio.TaskGroup() as group:
            group.create_task(self.setMeta(manga_id, self.MODE_KEY, mode))
            group.create_task(self.setMeta(manga_id, self.DIRECTION_KEY, direction))

    async def setMeta(self, manga_id: int, key: str, value):
        await self.query(SET_MANGA_META, variable_values={
            "mangaId": manga_id,
            "key": key,
            "value": json.dumps(value),
        })

    async def getGlobalReaderSettings(self) -> dict:
        return self.parseReaderSettings(await self.getGlobalMetaValues())

    async def setGlobalReadingMode(self, mode: str, orientation: str):
        await self.setGlobalMeta(self.MODE_KEY, readingModeValue(mode, orientation))

    async def setGlobalReadingDirection(self, direction: str):
        await self.setGlobalMeta(self.DIRECTION_KEY, 1 if direction == "rtl" else 0)

    async def setGlobalMeta(self, key: str, value):
        await self.query(SET_GLOBAL_META, variable_values={
            "key": key,
            "value": json.dumps(value),
        })

    async def getServerSettings(self) -> dict:
        result = await self.query(GET_SERVER_SETTINGS)
        return result.get('settings') or {}

    async def setServerSettings(self, **settings):
        await self.query(SET_SERVER_SETTINGS, variable_values={"settings": settings})

    async def setDownloadAsCbz(self, value: bool):
        await self.setServerSettings(downloadAsCbz=value)

    async def setDownloadsPath(self, path: str):
        await self.setServerSettings(downloadsPath=path)

    async def getExtensionStores(self) -> list:
        settings = await self.getServerSettings()
        return settings.get('extensionRepos') or []

    async def setExtensionStores(self, urls: list):
        await self.setServerSettings(extensionRepos=urls)

    def languageTitle(self, lang: str) -> str:
        if lang == "all":
            return "All languages"
        if not lang or lang == "localsourcelang":
            return "Other"
        try:
            return Locale.parse(lang, sep="-").get_display_name("en")
        except Exception:
            return lang.upper()
