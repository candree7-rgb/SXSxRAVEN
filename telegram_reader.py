"""
Telegram Reader using Telethon (event-based, real-time)
Adapted from user's working implementation
"""

import asyncio
import logging
from typing import Callable, Optional, Set
from datetime import datetime

from telethon import events
from telethon.sync import TelegramClient
from telethon.sessions import StringSession


class TelegramReader:
    """
    Real-time Telegram message reader using Telethon.

    No polling needed - messages are delivered via events as they arrive.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        channels: str,
        session_string: str = "",
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize Telegram reader.

        Args:
            api_id: Telegram API ID (from https://my.telegram.org)
            api_hash: Telegram API Hash
            channels: Comma-separated channel usernames or IDs (e.g., "@ravenpro,@channel2" or "1234567890")
            session_string: Persistent session string (empty for first run)
            logger: Optional logger instance
        """
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = StringSession(session_string)
        self.log = logger or logging.getLogger(__name__)

        # Parse channels
        self.channel_usernames = [c.strip() for c in channels.split(",") if c.strip()]
        self._chat_ids: Set[int] = set()

        self.client: Optional[TelegramClient] = None
        self._message_handler: Optional[Callable] = None
        self._running = False

    def set_message_handler(self, handler: Callable[[int, str, float], None]) -> None:
        """
        Set callback for new messages.

        Args:
            handler: Callback function(msg_id: int, text: str, timestamp: float)
        """
        self._message_handler = handler

    def get_session_string(self) -> str:
        """Get current session string for persistence."""
        if self.client and self.client.session:
            return self.client.session.save()
        return ""

    def _match_chat(self, event) -> bool:
        """Check if event is from one of our target channels."""
        if not self._chat_ids:
            return False

        chat_id = None
        if hasattr(event, 'chat_id'):
            chat_id = event.chat_id
        elif hasattr(event, 'peer_id'):
            chat_id = event.peer_id.channel_id if hasattr(event.peer_id, 'channel_id') else None

        return chat_id in self._chat_ids if chat_id else False

    async def _resolve_channels(self) -> None:
        """Resolve channel usernames to chat IDs."""
        self._chat_ids.clear()

        for channel in self.channel_usernames:
            try:
                # Handle @username - needs resolution
                if channel.startswith('@'):
                    entity = await self.client.get_entity(channel)
                    chat_id = entity.id
                    self._chat_ids.add(chat_id)
                    self.log.info(f"✅ Resolved channel {channel} → {chat_id}")

                # Handle numeric IDs (including negative ones like -1002025091313)
                else:
                    try:
                        chat_id = int(channel)
                        # Use ID directly without resolution (works for private channels)
                        self._chat_ids.add(chat_id)
                        self.log.info(f"✅ Using channel ID directly: {chat_id}")
                    except ValueError:
                        # Not a number, try as username without @
                        entity = await self.client.get_entity(channel)
                        chat_id = entity.id
                        self._chat_ids.add(chat_id)
                        self.log.info(f"✅ Resolved channel {channel} → {chat_id}")

            except Exception as e:
                self.log.error(f"❌ Failed to resolve channel {channel}: {e}")

        if not self._chat_ids:
            raise RuntimeError("No channels could be resolved")

    def start(self) -> None:
        """
        Start Telegram client and begin listening for messages.

        This method blocks until stop() is called.
        """
        if self._running:
            self.log.warning("Telegram reader already running")
            return

        self.log.info("🚀 Starting Telegram reader...")

        self.client = TelegramClient(self.session, self.api_id, self.api_hash)

        @self.client.on(events.NewMessage())
        async def handler(event):
            """Handle incoming messages."""
            if not self._match_chat(event):
                return

            try:
                text = event.raw_text or ""
                msg_id = event.id
                timestamp = event.date.timestamp() if event.date else datetime.now().timestamp()

                self.log.debug(f"📨 New message {msg_id} from chat {event.chat_id}")

                if self._message_handler:
                    # Call handler in sync context
                    self._message_handler(msg_id, text, timestamp)

            except Exception as e:
                self.log.error(f"Error handling message: {e}")

        # Start client
        self.client.start()
        self.log.info("✅ Telegram client started")

        # Resolve channels to IDs
        self.client.loop.run_until_complete(self._resolve_channels())

        # Save session string for next run
        session_str = self.get_session_string()
        if session_str:
            self.log.info(f"💾 Session string (save to TELEGRAM_SESSION_STRING): {session_str[:50]}...")

        self._running = True
        self.log.info(f"👂 Listening to {len(self._chat_ids)} channel(s)...")

        # Run until disconnected
        self.client.run_until_disconnected()

    def stop(self) -> None:
        """Stop Telegram client."""
        if not self._running:
            return

        self.log.info("🛑 Stopping Telegram reader...")

        if self.client:
            self.client.disconnect()

        self._running = False

    def fetch_message(self, msg_id: int) -> Optional[dict]:
        """
        Fetch a single message by ID (for signal updates).

        Args:
            msg_id: Message ID to fetch

        Returns:
            dict with 'id' and 'text' keys, or None if not found
        """
        if not self.client or not self._running:
            self.log.warning("Client not running - cannot fetch message")
            return None

        try:
            # Fetch from all known channels
            for chat_id in self._chat_ids:
                try:
                    async def _fetch():
                        msg = await self.client.get_messages(chat_id, ids=msg_id)
                        return msg

                    msg = self.client.loop.run_until_complete(_fetch())

                    if msg and msg.text:
                        return {
                            'id': msg.id,
                            'text': msg.text,
                            'timestamp': msg.date.timestamp() if msg.date else None
                        }

                except Exception as e:
                    self.log.debug(f"Message {msg_id} not found in chat {chat_id}: {e}")
                    continue

        except Exception as e:
            self.log.error(f"Error fetching message {msg_id}: {e}")

        return None


# Example usage for testing
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger(__name__)

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    channels = os.getenv("TELEGRAM_CHANNEL", "")
    session_string = os.getenv("TELEGRAM_SESSION_STRING", "")

    if not api_id or not api_hash or not channels:
        raise SystemExit("Missing TELEGRAM_API_ID, TELEGRAM_API_HASH, or TELEGRAM_CHANNEL")

    def on_message(msg_id: int, text: str, timestamp: float):
        log.info(f"📨 Message {msg_id}: {text[:100]}...")

    reader = TelegramReader(api_id, api_hash, channels, session_string)
    reader.set_message_handler(on_message)

    try:
        reader.start()
    except KeyboardInterrupt:
        log.info("Bye")
        reader.stop()
