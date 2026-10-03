from collections import deque
import os
from pathlib import Path


class StoredImage:
    def __init__(
        self,
        attachment_id,
        filename,
        content_type,
        size,
        cache_path,
    ):
        self.attachment_id = attachment_id
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.cache_path = str(cache_path)


class StoredMessage:
    def __init__(
        self,
        id,
        attachments,
        author,
        channel,
        content,
        created_at,
        mentions,
        images=None,
        steps_dump=None,
    ):
        self.id = id
        self.attachments = attachments
        self.author = author
        self.channel = channel
        self.content = content
        self.created_at = created_at
        self.mentions = mentions
        self.images = images or []

        # nabemono internal state
        self.steps_dump = steps_dump or []

    @classmethod
    def from_discord(cls, message):
        return cls(
            id=message.id,
            attachments=list(message.attachments),
            author=message.author,
            channel=message.channel,
            content=message.content.replace("—", ""),
            created_at=message.created_at,
            mentions=list(message.mentions),
        )

class MemoryManager:
    def __init__(self, max_messages=10, image_cache_dir=None):
        self.max_messages = max_messages
        self.image_cache_dir = Path(
            image_cache_dir or os.getenv("IMAGE_CACHE_DIR", ".cache/images")
        )
        self._messages = {}

    """Add a new message and drop the oldest message when at capacity."""
    async def add_message(self, message):
        channel_id = message.channel.id
        if channel_id not in self._messages:
            self._messages[channel_id] = deque(maxlen=self.max_messages)
        channel_messages = self._messages.get(channel_id)
        if channel_messages and len(channel_messages) == self.max_messages:
            self._delete_cached_images(channel_messages[0])

        stored_message = await self._to_stored_message(message)
        channel_messages.append(stored_message)

    async def _to_stored_message(self, message):
        if isinstance(message, StoredMessage):
            return message
        stored_message = StoredMessage.from_discord(message)
        stored_message.images = await self._cache_images(message)
        return stored_message

    async def _cache_images(self, message):
        images = []
        for attachment in message.attachments:
            content_type = getattr(attachment, "content_type", None)
            if not content_type or not content_type.startswith("image/"):
                continue

            try:
                image_bytes = await attachment.read()
                cache_path = self._cache_path_for(message, attachment)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(image_bytes)
            except Exception as error:
                print(
                    f"[IMAGE CACHE ERROR]: skipped attachment "
                    f"{getattr(attachment, 'id', 'unknown')}: {error}"
                )
                continue

            images.append(
                StoredImage(
                    attachment_id=getattr(attachment, "id", None),
                    filename=getattr(attachment, "filename", ""),
                    content_type=content_type,
                    size=getattr(attachment, "size", len(image_bytes)),
                    cache_path=cache_path,
                )
            )
        return images

    def _cache_path_for(self, message, attachment):
        suffix = Path(getattr(attachment, "filename", "")).suffix
        return self.image_cache_dir / (
            f"{message.channel.id}-{message.id}-"
            f"{getattr(attachment, 'id', 'unknown')}{suffix}"
        )

    def _delete_cached_images(self, message):
        for image in getattr(message, "images", []):
            try:
                Path(image.cache_path).unlink(missing_ok=True)
            except Exception as error:
                print(
                    f"[IMAGE CACHE ERROR]: failed to delete "
                    f"{image.cache_path}: {error}"
                )

    def dump_steps(self, message, steps):
        if message.channel.id in self._messages:
            for stored_message in self._messages.get(message.channel.id):
                if stored_message.id == message.id:
                    for step in steps:
                        if hasattr(step, "model_dump"):
                            stored_message.steps_dump.append(step.model_dump())
                        else:
                            stored_message.steps_dump.append(step)
                    break

    """Return stored messages in chronological order.

    If exclude_last is True, the newest message is excluded.
     """
    def get_messages(self, channel_id, limit=None, exclude_last=False):
        messages = list(self._messages.get(channel_id, deque(maxlen=self.max_messages)))
        if limit is not None:
            messages = messages[-limit:]

        if exclude_last and messages:
            return messages[:-1]
        return messages
    
    """Edit the content of the most recent message."""
    def edit_last_message_content(self, channel_id, new_content):
        if self._messages.get(channel_id):
            last_message = self._messages.get(channel_id, deque(maxlen=self.max_messages))[-1]
            last_message.content = new_content

    """Remove and return the most recent message."""
    def pop(self, channel_id):
        if self._messages.get(channel_id):
            message = self._messages.get(channel_id, deque(maxlen=self.max_messages)).pop()
            self._delete_cached_images(message)
            return message
        return None

    """Remove all stored messages."""
    def clear(self):
        for messages in self._messages.values():
            for message in messages:
                self._delete_cached_images(message)
        self._messages.clear()

    def __len__(self):
        return len(self._messages)
