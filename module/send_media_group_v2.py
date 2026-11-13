"""send media group"""
import logging
import os
import re
from datetime import datetime
from typing import Callable, List, Optional, Union

from module.telegram import pyrogram
from pyrogram import raw, types, utils
from pyrogram.file_id import FileType

log = logging.getLogger(__name__)

def _to_input_photo(media) -> raw.types.InputMediaPhoto:
    return raw.types.InputMediaPhoto(
        id=raw.types.InputPhoto(
            id=media.photo.id,
            access_hash=media.photo.access_hash,
            file_reference=media.photo.file_reference,
        )
    )


def _to_input_document(media) -> raw.types.InputMediaDocument:
    return raw.types.InputMediaDocument(
        id=raw.types.InputDocument(
            id=media.document.id,
            access_hash=media.document.access_hash,
            file_reference=media.document.file_reference,
        )
    )


# pylint: disable = R0915, R0902, R0912
async def cache_media(
    client: pyrogram.Client,
    chat_id: Union[int, str],
    media_obj: Union[
        "types.InputMediaPhoto",
        "types.InputMediaVideo",
        "types.InputMediaAudio",
        "types.InputMediaDocument",
    ],
    progress: Callable = None,
    progress_args: tuple = (),
) -> raw.base.InputSingleMedia:
    """
    Caches a media.

    :param client: The pyrogram.Client instance.
    :param chat_id: The ID of the chat.
    :param media: The media to be cached.
    :return: The cached media.
    """
    if isinstance(media_obj, types.InputMediaPhoto):
        if isinstance(media_obj.media, str) and not os.path.isfile(media_obj.media):
            if re.match("^https?://", media_obj.media):
                uploaded = await client.invoke(
                    raw.functions.messages.UploadMedia(
                        peer=await client.resolve_peer(chat_id),
                        media=raw.types.InputMediaPhotoExternal(url=media_obj.media),
                    )
                )
                media = _to_input_photo(uploaded)
            else:
                media = utils.get_input_media_from_file_id(
                    media_obj.media, FileType.PHOTO
                )
        else:
            uploaded = await client.invoke(
                raw.functions.messages.UploadMedia(
                    peer=await client.resolve_peer(chat_id),
                    media=raw.types.InputMediaUploadedPhoto(
                        file=await client.save_file(
                            media_obj.media,
                            progress=progress,
                            progress_args=progress_args,
                        )
                    ),
                )
            )
            media = _to_input_photo(uploaded)
    elif isinstance(
        media_obj,
        (types.InputMediaVideo, types.InputMediaAudio, types.InputMediaDocument),
    ):
        if isinstance(media_obj, types.InputMediaVideo):
            file_type = FileType.VIDEO
            default_name, default_mime = "video.mp4", "video/mp4"
            extra_attrs = [
                raw.types.DocumentAttributeVideo(
                    supports_streaming=media_obj.supports_streaming or None,
                    duration=media_obj.duration,
                    w=media_obj.width,
                    h=media_obj.height,
                )
            ]
            extra_kwargs = {"nosound_video": True}
        elif isinstance(media_obj, types.InputMediaAudio):
            file_type = FileType.AUDIO
            default_name, default_mime = "audio.mp3", "audio/mpeg"
            extra_attrs = [
                raw.types.DocumentAttributeAudio(
                    duration=media_obj.duration,
                    performer=media_obj.performer,
                    title=media_obj.title,
                )
            ]
            extra_kwargs = {}
        else:
            file_type = FileType.DOCUMENT
            default_name, default_mime = "file.zip", "application/zip"
            extra_attrs = []
            extra_kwargs = {}

        if isinstance(media_obj.media, str) and not os.path.isfile(media_obj.media):
            if re.match("^https?://", media_obj.media):
                uploaded = await client.invoke(
                    raw.functions.messages.UploadMedia(
                        peer=await client.resolve_peer(chat_id),
                        media=raw.types.InputMediaDocumentExternal(url=media_obj.media),
                    )
                )
                media = _to_input_document(uploaded)
            else:
                media = utils.get_input_media_from_file_id(media_obj.media, file_type)
        else:
            is_local_path = isinstance(media_obj.media, str)
            file_name = (
                os.path.basename(media_obj.media)
                if is_local_path
                else getattr(media_obj.media, "name", default_name)
            )
            mime_source = media_obj.media if is_local_path else file_name
            uploaded = await client.invoke(
                raw.functions.messages.UploadMedia(
                    peer=await client.resolve_peer(chat_id),
                    media=raw.types.InputMediaUploadedDocument(
                        file=await client.save_file(
                            media_obj.media,
                            progress=progress,
                            progress_args=progress_args,
                        ),
                        thumb=await client.save_file(media_obj.thumb),
                        mime_type=client.guess_mime_type(mime_source) or default_mime,
                        attributes=[
                            *extra_attrs,
                            raw.types.DocumentAttributeFilename(file_name=file_name),
                        ],
                        **extra_kwargs,
                    ),
                )
            )
            media = _to_input_document(uploaded)
    else:
        raise ValueError(
            f"{media_obj.__class__.__name__}"
            " is not a supported type for send_media_group"
        )

    return raw.types.InputSingleMedia(
        media=media,
        random_id=client.rnd_id(),
        **await client.parser.parse(
            media_obj.caption
            if media_obj.caption and media_obj.caption != "None"
            else ""
        ),
    )


# pylint: disable = R0913
async def send_media_group_v2(
    client: pyrogram.Client,
    chat_id: Union[int, str],
    multi_media: List[raw.types.InputSingleMedia],
    disable_notification: bool = None,
    schedule_date: datetime = None,
    quote_text: str = None,
    parse_mode: Optional["pyrogram.enums.ParseMode"] = None,
    message_thread_id: int = None,
    reply_to_message_id: int = None,
    reply_to_chat_id: Union[int, str] = None,
    reply_to_story_id: int = None,
    quote_entities: List["types.MessageEntity"] = None,
    quote_offset: int = None,
    show_above_text: bool = None,
):
    """
    see pyrogram
    """
    r = await client.invoke(
        raw.functions.messages.SendMultiMedia(
            peer=await client.resolve_peer(chat_id),
            multi_media=multi_media,
            silent=disable_notification or None,
            reply_to=await utils.get_reply_to(
                client,
                reply_parameters=types.ReplyParameters(
                    message_id=reply_to_message_id,
                    chat_id=reply_to_chat_id,
                    story_id=reply_to_story_id,
                    quote=quote_text,
                    quote_parse_mode=parse_mode,
                    quote_entities=quote_entities,
                    quote_position=quote_offset,
                ),
                message_thread_id=message_thread_id,
            ),
            schedule_date=utils.datetime_to_timestamp(schedule_date),
            invert_media=show_above_text,
        ),
        sleep_threshold=60,
    )

    return await utils.parse_messages(
        client,
        raw.types.messages.Messages(
            messages=[
                m.message
                for m in filter(
                    lambda u: isinstance(
                        u,
                        (
                            raw.types.UpdateNewMessage,
                            raw.types.UpdateNewChannelMessage,
                            raw.types.UpdateNewScheduledMessage,
                        ),
                    ),
                    r.updates,
                )
            ],
            users=r.users,
            chats=r.chats,
            topics=getattr(r, "topics", []),
        ),
    )
