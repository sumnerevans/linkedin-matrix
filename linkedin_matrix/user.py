import asyncio
import time
from typing import (
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    cast,
    Dict,
    List,
    Optional,
    TYPE_CHECKING,
)

from linkedin_messaging import LinkedInMessaging, URN
from linkedin_messaging.api_objects import (
    Conversation,
    ConversationEvent,
    ReactionSummary,
    RealTimeEventStreamEvent,
)
from mautrix.bridge import async_getter_lock, BaseUser
from mautrix.errors import MNotFound
from mautrix.types import (
    PushActionType,
    PushRuleKind,
    PushRuleScope,
    RoomID,
    UserID,
)
from mautrix.util.opt_prometheus import async_time, Gauge, Summary
from mautrix.util.simple_lock import SimpleLock

from . import portal as po, puppet as pu
from .config import Config
from .db import User as DBUser

if TYPE_CHECKING:
    from .__main__ import LinkedInBridge

METRIC_LOGGED_IN = Gauge("bridge_logged_in", "Users logged into the bridge")
METRIC_SYNC_THREADS = Summary("bridge_sync_threads", "calls to sync_threads")


class User(DBUser, BaseUser):
    shutdown: bool = False
    config: Config

    by_mxid: Dict[UserID, "User"] = {}
    by_li_member_urn: Dict[URN, "User"] = {}

    listen_task: Optional[asyncio.Task]

    _is_connected: Optional[bool]
    _is_logged_in: Optional[bool]
    _is_refreshing: bool
    _notice_room_lock: asyncio.Lock
    _notice_send_lock: asyncio.Lock
    _sync_lock: SimpleLock
    is_admin: bool

    def __init__(
        self,
        mxid: UserID,
        li_member_urn: Optional[URN] = None,
        client: Optional[LinkedInMessaging] = None,
        notice_room: Optional[RoomID] = None,
    ):
        super().__init__(mxid, li_member_urn, notice_room, client)
        BaseUser.__init__(self)
        self.notice_room = notice_room
        self._notice_room_lock = asyncio.Lock()
        self._notice_send_lock = asyncio.Lock()

        self.command_status = None
        (
            self.is_whitelisted,
            self.is_admin,
            self.permission_level,
        ) = self.config.get_permissions(mxid)
        self._is_logged_in = None
        self._is_connected = None
        self._connection_time = time.monotonic()
        self._prev_thread_sync = -10
        self._prev_reconnect_fail_refresh = time.monotonic()
        self._community_id = None
        self._sync_lock = SimpleLock(
            "Waiting for thread sync to finish before handling %s", log=self.log
        )
        self._is_refreshing = False

        self.log = self.log.getChild(self.mxid)

        self.listen_task = None

    @classmethod
    def init_cls(cls, bridge: "LinkedInBridge") -> AsyncIterable[Awaitable[bool]]:
        cls.bridge = bridge
        cls.config = bridge.config
        cls.az = bridge.az
        cls.loop = bridge.loop
        cls.temp_disconnect_notices = bridge.config[
            "bridge.temporary_disconnect_notices"
        ]
        return (user.load_session() async for user in cls.all_logged_in())

    @property
    def is_connected(self) -> Optional[bool]:
        return self._is_connected

    @is_connected.setter
    def is_connected(self, val: Optional[bool]):
        if self._is_connected != val:
            self._is_connected = val
            self._connection_time = time.monotonic()

    # region Database getters

    def _add_to_cache(self) -> None:
        self.by_mxid[self.mxid] = self
        if self.li_member_urn:
            self.by_li_member_urn[self.li_member_urn] = self

    @classmethod
    async def all_logged_in(cls) -> AsyncGenerator["User", None]:
        users = await super().all_logged_in()
        for user in cast(List["User"], users):
            try:
                yield cls.by_mxid[user.mxid]
            except KeyError:
                user._add_to_cache()
                yield user

    @classmethod
    @async_getter_lock
    async def get_by_mxid(
        cls,
        mxid: UserID,
        *,
        create: bool = True,
    ) -> Optional["User"]:
        if pu.Puppet.get_id_from_mxid(mxid) or mxid == cls.az.bot_mxid:
            return None
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        user = cast("User", await super().get_by_mxid(mxid))
        if user is not None:
            user._add_to_cache()
            return user

        if create:
            cls.log.debug(f"Creating user instance for {mxid}")
            user = cls(mxid)
            await user.insert()
            user._add_to_cache()
            return user

        return None

    @classmethod
    @async_getter_lock
    async def get_by_li_member_urn(cls, li_member_urn: URN) -> Optional["User"]:
        try:
            return cls.by_li_member_urn[li_member_urn]
        except KeyError:
            pass

        user = cast("User", await super().get_by_li_member_urn(li_member_urn))
        if user is not None:
            user._add_to_cache()
            return user

        return None

    # endregion

    # region Session Management

    async def load_session(
        self,
        _override: bool = False,
        _raise_errors: bool = False,
    ) -> bool:
        if self._is_logged_in and not _override:
            return True
        if not self.client or not await self.client.logged_in():
            return False

        self.log.info("Loaded session successfully")
        self.li_member_urn = (
            await self.client.get_user_profile()
        ).mini_profile.entity_urn
        # TODO (#51)
        # self._track_metric(METRIC_LOGGED_IN, True)
        self._is_logged_in = True
        self.is_connected = None
        self.stop_listen()
        asyncio.create_task(self.post_login())
        return True

    async def reconnect(self) -> None:
        assert self.listen_task
        self._is_refreshing = True
        await self.listen_task
        self.listen_task = None
        self.start_listen()
        self._is_refreshing = False

    async def is_logged_in(self, _override: bool = False) -> bool:
        if not self.client:
            return False
        if self._is_logged_in is None or _override:
            try:
                self._is_logged_in = await self.client.logged_in()
            except Exception:
                self.log.exception("Exception checking login status")
                self._is_logged_in = False
        return self._is_logged_in or False

    async def on_logged_in(self, client: LinkedInMessaging):
        self.client = client
        self.li_member_urn = (
            await self.client.get_user_profile()
        ).mini_profile.entity_urn
        await self.save()
        self.stop_listen()
        asyncio.create_task(self.post_login())

    async def post_login(self):
        self.log.info("Running post-login actions")
        self._add_to_cache()

        try:
            puppet = await pu.Puppet.get_by_li_member_urn(self.li_member_urn)

            if puppet.custom_mxid != self.mxid and puppet.can_auto_login(self.mxid):
                self.log.info("Automatically enabling custom puppet")
                await puppet.switch_mxid(access_token="auto", mxid=self.mxid)
        except Exception:
            self.log.exception("Failed to automatically enable custom puppet")
        await self.sync_threads()
        self.start_listen()

    async def logout(self):
        if self.listen_task:
            self.listen_task.cancel()
        if self.client:
            await self.client.logout()
        puppet = await pu.Puppet.get_by_li_member_urn(self.li_member_urn, create=False)
        if puppet and puppet.is_real_user:
            await puppet.switch_mxid(None, None)
        if self.li_member_urn:
            try:
                del self.by_li_member_urn[self.li_member_urn]
            except KeyError:
                pass
        self._is_logged_in = False
        self.client = None
        self.li_member_urn = None
        self.notice_room = None
        await self.save()

    # endregion

    # region Thread Syncing

    async def get_direct_chats(self) -> Dict[UserID, List[RoomID]]:
        assert self.li_member_urn
        return {
            pu.Puppet.get_mxid_from_id(portal.li_other_user_urn): [portal.mxid]
            async for portal in po.Portal.get_all_by_li_receiver_urn(self.li_member_urn)
            if portal.mxid
        }

    @async_time(METRIC_SYNC_THREADS)
    async def sync_threads(self):
        if self._prev_thread_sync + 10 > time.monotonic():
            self.log.debug(
                "Previous thread sync was less than 10 seconds ago, not re-syncing"
            )
            return
        self._prev_thread_sync = time.monotonic()
        try:
            await self._sync_threads()
        except Exception:
            self.log.exception("Failed to sync threads")

    async def _sync_threads(self) -> None:
        assert self.client
        sync_count = self.config["bridge.initial_chat_sync"]
        if sync_count <= 0:
            return

        self.log.debug("Fetching threads...")
        # user_portals = await UserPortal.all(self.li_member_urn)

        async for conversation in self.client.get_all_conversations():
            try:
                await self._sync_thread(conversation)
            except Exception:
                self.log.exception(f"Failed to sync thread {conversation.entity_urn}")

        await self.update_direct_chats()

    async def _sync_thread(self, conversation: Conversation):
        self.log.debug(f"Syncing thread {conversation.entity_urn}")

        li_other_user_urn = None
        if not conversation.group_chat:
            other_user = conversation.participants[0]
            li_other_user_urn = other_user.messaging_member.mini_profile.entity_urn

        portal = await po.Portal.get_by_li_thread_urn(
            conversation.entity_urn,
            li_receiver_urn=self.li_member_urn,
            li_is_group_chat=conversation.group_chat,
            li_other_user_urn=li_other_user_urn,
        )
        assert portal
        portal = cast(po.Portal, portal)

        was_created = False
        if not portal.mxid:
            await portal.create_matrix_room(self, conversation)
            was_created = True
        else:
            await portal.update_matrix_room(self, conversation)
            await portal.backfill(self, conversation, is_initial=False)
        if was_created or not self.config["bridge.tag_only_on_create"]:
            await self._mute_room(portal, conversation.muted)

    async def _mute_room(self, portal: po.Portal, muted: bool):
        if not self.config["bridge.mute_bridging"] or not portal or not portal.mxid:
            return
        puppet = await pu.Puppet.get_by_custom_mxid(self.mxid)
        if not puppet or not puppet.is_real_user:
            return
        if muted:
            await puppet.intent.set_push_rule(
                PushRuleScope.GLOBAL,
                PushRuleKind.ROOM,
                portal.mxid,
                actions=[PushActionType.DONT_NOTIFY],
            )
        else:
            try:
                await puppet.intent.remove_push_rule(
                    PushRuleScope.GLOBAL, PushRuleKind.ROOM, portal.mxid
                )
            except MNotFound:
                pass

    # endregion

    # region Listener Management

    def stop_listen(self):
        if self.listen_task:
            self.listen_task.cancel()
        self.listen_task = None

    def start_listen(self):
        self.listen_task = asyncio.create_task(self._try_listen())

    async def _try_listen(self):
        assert self.client
        self.client.add_event_listener("event", self.handle_linkedin_event)
        self.client.add_event_listener(
            "reactionAdded", self.handle_linkedin_reaction_added
        )
        await self.client.start_listener()

    async def handle_linkedin_event(self, event: RealTimeEventStreamEvent):
        assert self.client
        assert isinstance(event.event, ConversationEvent)

        thread_urn, message_urn = map(URN, event.event.entity_urn.id_parts)
        sender_urn = event.event.from_.messaging_member.mini_profile.entity_urn

        portal = await po.Portal.get_by_li_thread_urn(
            thread_urn,
            li_receiver_urn=self.li_member_urn,
            create=False,
        )
        if not portal:
            # Force a thread sync for all of the recent conversations. This should be a
            # noop for most of them except the newly created conversation.
            conversations = await self.client.get_conversations()
            for conversation in conversations.elements:
                await self._sync_thread(conversation)

            # Nothing more to do, since the backfill should handle the message coming
            # in.
            return

        puppet = await pu.Puppet.get_by_li_member_urn(sender_urn)

        await portal.backfill_lock.wait(message_urn)
        await portal.handle_linkedin_message(self, puppet, event.event)

    async def handle_linkedin_reaction_added(self, event: RealTimeEventStreamEvent):
        assert isinstance(event.reaction_summary, ReactionSummary)
        assert isinstance(event.reaction_added, bool)
        assert isinstance(event.actor_mini_profile_urn, URN)

        self.log.info("reaction added", event)

        # TODO (#31) actually handle this
        # event_entity_urn = event.get("eventUrn", "")
        # match = self.event_urn_re.match(event_entity_urn)
        # if not match:
        #     return
        # thread_urn, message_urn = match.groups()

        # sender_urn = event.get("actorMiniProfileUrn", "").split(":")[-1]

        # portal = await po.Portal.get_by_li_thread_urn(
        #     thread_urn, li_receiver_urn=self.li_member_urn
        # )
        # puppet = await pu.Puppet.get_by_li_member_urn(sender_urn)

        # await portal.backfill_lock.wait(message_urn)
        # await portal.handle_linkedin_reaction_summary(self, puppet, event)

    # endregion
