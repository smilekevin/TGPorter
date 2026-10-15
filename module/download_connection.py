"""Give each download worker its own media session and TCP connection."""

import asyncio
from itertools import count

from loguru import logger
from module.telegram import pyrogram
from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid
from pyrogram.session import Auth, Session


_connection_ids = count(1)


class _MediaSession:
    """Remember the redirect DC before Kurigram asks for its CDN session."""

    def __init__(self, owner, session):
        self.owner = owner
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    async def invoke(self, query, **kwargs):
        # The pool must see even short FloodWaits before it creates more
        # connections; SDK-internal sleeping would hide the account cooldown.
        kwargs["sleep_threshold"] = 0
        result = await self.session.invoke(query, **kwargs)
        if isinstance(result, raw.types.upload.FileCdnRedirect):
            self.owner.cdn_dc_id = result.dc_id
        return result


class _DownloadClient:
    """Share account settings, but never share the DC media-session cache."""

    def __init__(self, client):
        self.client = client
        self.media_sessions = {}
        self.cdn_dc_id = None
        # Each worker downloads one part at a time. The SDK's shared semaphore
        # must not serialize workers or shrink a saved plan after restart.
        self.get_file_semaphore = asyncio.Semaphore(1)
        self.connections = []
        self.task = asyncio.current_task()
        self.stopped_by_client = False

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def get_session(self, dc_id, *, is_media=False, is_cdn=False, temporary=False):
        """Own every media/CDN session, including one interrupted during startup."""
        if is_cdn:
            dc_id = self.cdn_dc_id or dc_id
        key = (dc_id, is_cdn)
        if not is_cdn and key in self.media_sessions:
            return _MediaSession(self, self.media_sessions[key])
        if is_cdn:
            # Kurigram stops temporary CDN sessions after each range. Keep each
            # one registered for interrupted cleanup, but never reuse it.
            key = (dc_id, is_cdn, len(self.media_sessions))

        option = await self.client.get_dc_option(
            dc_id, is_media=is_media, ipv6=self.ipv6, is_cdn=is_cdn)
        test_mode = await self.storage.test_mode()
        current_dc = await self.storage.dc_id() == dc_id
        if current_dc and not is_cdn:
            auth_key = await self.storage.auth_key()
        else:
            auth_key = await Auth(self, dc_id, option.ip_address, option.port, test_mode).create()
        session = Session(self, dc_id, option.ip_address, option.port, auth_key,
                          test_mode, is_media=is_media, is_cdn=is_cdn)
        # Register before start: cancellation must still stop its background tasks.
        self.media_sessions[key] = session
        await session.start()
        if not current_dc and not is_cdn:
            for _ in range(3):
                exported = await self.client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id),
                                                    sleep_threshold=0)
                try:
                    await session.invoke(raw.functions.auth.ImportAuthorization(
                        id=exported.id, bytes=exported.bytes), sleep_threshold=0)
                except AuthBytesInvalid:
                    continue
                else:
                    break
            else:
                raise AuthBytesInvalid
        return _MediaSession(self, session)

    def connection_factory(self, *args, **kwargs):
        connection = self.client.connection_factory(*args, **kwargs)
        self.connections.append(connection)
        return connection


async def _close_resources(stream, download_client):
    try:
        if stream is not None:
            await stream.aclose()
    finally:
        # Includes a session whose startup was interrupted. Pyrogram stores it
        # in the cache before awaiting its startup.
        sessions = list(download_client.media_sessions.values())
        results = await asyncio.gather(*(session.stop() for session in sessions),
                                       return_exceptions=True)
        download_client.media_sessions.clear()
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Closing independent download connection failed: {}", result)
        # CDN sessions are created inside Pyrogram rather than its DC cache.
        # Also close their transports if cancellation interrupted SDK cleanup.
        await asyncio.gather(*(connection.close() for connection in download_client.connections),
                             return_exceptions=True)
        download_client.connections.clear()


class DownloadConnection:
    """One worker owns its media connection for all queued parts it downloads."""

    def __init__(self, client, label):
        self.client = client
        self.label = label
        self.download_client = _DownloadClient(client) if isinstance(client, Client) else None
        self.connection_id = next(_connection_ids)
        self.opened = False
        self.stream = None

    async def __aenter__(self):
        if self.download_client is not None:
            if getattr(self.client, "_download_connections_closing", False):
                raise ConnectionError("Download client is restarting")
            if not hasattr(self.client, "_independent_download_connections"):
                self.client._independent_download_connections = set()
            self.client._independent_download_connections.add(self.download_client)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        async def close():
            if self.download_client is not None:
                await _close_resources(self.stream, self.download_client)
            elif self.stream is not None:
                await self.stream.aclose()

        cleanup = asyncio.create_task(close())
        cancelled = False
        try:
            # Finish closing sockets even if gather() sends a second cancellation.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
            if self.opened:
                logger.info("Closed independent download connection #{} for {}",
                            self.connection_id, self.label)
        finally:
            if self.download_client is not None:
                self.client._independent_download_connections.discard(self.download_client)
        if exc_type is asyncio.CancelledError or cancelled:
            if self.download_client is not None and self.download_client.stopped_by_client:
                raise ConnectionError("Download connection closed for client restart") from None
            if cancelled:
                raise asyncio.CancelledError

    async def get_file(self, file_id, size, limit, offset, progress):
        if self.download_client is None:
            stream = self.client.get_file(file_id, size, limit, offset, progress=progress)
        else:
            # Pyrogram uses this worker's private DC cache for every queued part.
            stream = Client.get_file(self.download_client, file_id, size, limit, offset,
                                     progress=progress)
        self.stream = stream
        try:
            async for chunk in stream:
                if self.download_client is not None and not self.opened:
                    self.opened = True
                    logger.info("Opened independent download connection #{} for {} (DC {})",
                                self.connection_id, self.label, file_id.dc_id)
                yield chunk
        finally:
            # Pyrogram also owns temporary CDN sessions inside this generator.
            await stream.aclose()
            self.stream = None


async def close_download_connections(client):
    """Stop active workers before the main client closes its account storage."""
    client._download_connections_closing = True
    connections = list(getattr(client, "_independent_download_connections", ()))
    tasks = set()
    for connection in connections:
        connection.stopped_by_client = True
        if connection.task is not asyncio.current_task() and not connection.task.done():
            tasks.add(connection.task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
