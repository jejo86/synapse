# Copyright 2014 - 2016 OpenMarket Ltd
# Copyright (C) The Matrix.org Foundation C.I.C. 2022
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
from typing import Collection, Dict, FrozenSet, List, Optional, Tuple

from typing_extensions import Final

from synapse.api.constants import EventTypes, HistoryVisibility, Membership
from synapse.events import EventBase
from synapse.events.utils import prune_event
from synapse.storage.controllers import StorageControllers
from synapse.storage.state import StateFilter
from synapse.types import RetentionPolicy, StateMap, get_domain_from_id

logger = logging.getLogger(__name__)


VISIBILITY_PRIORITY = (
    HistoryVisibility.WORLD_READABLE,
    HistoryVisibility.SHARED,
    HistoryVisibility.INVITED,
    HistoryVisibility.JOINED,
)


MEMBERSHIP_PRIORITY = (
    Membership.JOIN,
    Membership.INVITE,
    Membership.KNOCK,
    Membership.LEAVE,
    Membership.BAN,
)

_HISTORY_VIS_KEY: Final[Tuple[str, str]] = (EventTypes.RoomHistoryVisibility, "")


async def filter_events_for_client(
    storage: StorageControllers,
    user_id: str,
    events: List[EventBase],
    is_peeking: bool = False,
    always_include_ids: FrozenSet[str] = frozenset(),
    filter_send_to_client: bool = True,
) -> List[EventBase]:
    """
    Check which events a user is allowed to see. If the user can see the event but its
    sender asked for their data to be erased, prune the content of the event.

    Args:
        storage
        user_id: user id to be checked
        events: sequence of events to be checked
        is_peeking: should be True if:
          * the user is not currently a member of the room, and:
          * the user has not been a member of the room since the given
            events
        always_include_ids: set of event ids to specifically
            include (unless sender is ignored)
        filter_send_to_client: Whether we're checking an event that's going to be
            sent to a client. This might not always be the case since this function can
            also be called to check whether a user can see the state at a given point.

    Returns:
        The filtered events.
    """
    # Filter out events that have been soft failed so that we don't relay them
    # to clients.
    events = [e for e in events if not e.internal_metadata.is_soft_failed()]

    types = (_HISTORY_VIS_KEY, (EventTypes.Member, user_id))

    # we exclude outliers at this point, and then handle them separately later
    event_id_to_state = await storage.state.get_state_for_events(
        frozenset(e.event_id for e in events if not e.internal_metadata.outlier),
        state_filter=StateFilter.from_types(types),
    )

    # Get the users who are ignored by the requesting user.
    ignore_list = await storage.main.ignored_users(user_id)

    erased_senders = await storage.main.are_users_erased(e.sender for e in events)

    if filter_send_to_client:
        room_ids = {e.room_id for e in events}
        retention_policies: Dict[str, RetentionPolicy] = {}

        for room_id in room_ids:
            retention_policies[
                room_id
            ] = await storage.main.get_retention_policy_for_room(room_id)

    def allowed(event: EventBase) -> Optional[EventBase]:
        """
        Args:
            event: event to check

        Returns:
           None if the user cannot see this event at all

           a redacted copy of the event if they can only see a redacted
           version

           the original event if they can see it as normal.
        """
        # Only run some checks if these events aren't about to be sent to clients. This is
        # because, if this is not the case, we're probably only checking if the users can
        # see events in the room at that point in the DAG, and that shouldn't be decided
        # on those checks.
        if filter_send_to_client:
            if event.type == EventTypes.Dummy:
                return None

            if not event.is_state() and event.sender in ignore_list:
                return None

            # Until MSC2261 has landed we can't redact malicious alias events, so for
            # now we temporarily filter out m.room.aliases entirely to mitigate
            # abuse, while we spec a better solution to advertising aliases
            # on rooms.
            if event.type == EventTypes.Aliases:
                return None

            # Don't try to apply the room's retention policy if the event is a state
            # event, as MSC1763 states that retention is only considered for non-state
            # events.
            if not event.is_state():
                retention_policy = retention_policies[event.room_id]
                max_lifetime = retention_policy.max_lifetime

                if max_lifetime is not None:
                    oldest_allowed_ts = storage.main.clock.time_msec() - max_lifetime

                    if event.origin_server_ts < oldest_allowed_ts:
                        return None

        if event.event_id in always_include_ids:
            return event

        # we need to handle outliers separately, since we don't have the room state.
        if event.internal_metadata.outlier:
            # Normally these can't be seen by clients, but we make an exception for
            # for out-of-band membership events (eg, incoming invites, or rejections of
            # said invite) for the user themselves.
            if event.type == EventTypes.Member and event.state_key == user_id:
                logger.debug("Returning out-of-band-membership event %s", event)
                return event

            return None

        state = event_id_to_state[event.event_id]

        # get the room_visibility at the time of the event.
        visibility = get_effective_room_visibility_from_state(state)

        # Always allow history visibility events on boundaries. This is done
        # by setting the effective visibility to the least restrictive
        # of the old vs new.
        if event.type == EventTypes.RoomHistoryVisibility:
            prev_content = event.unsigned.get("prev_content", {})
            prev_visibility = prev_content.get("history_visibility", None)

            if prev_visibility not in VISIBILITY_PRIORITY:
                prev_visibility = HistoryVisibility.SHARED

            new_priority = VISIBILITY_PRIORITY.index(visibility)
            old_priority = VISIBILITY_PRIORITY.index(prev_visibility)
            if old_priority < new_priority:
                visibility = prev_visibility

        # likewise, if the event is the user's own membership event, use
        # the 'most joined' membership
        membership = None
        if event.type == EventTypes.Member and event.state_key == user_id:
            membership = event.content.get("membership", None)
            if membership not in MEMBERSHIP_PRIORITY:
                membership = "leave"

            prev_content = event.unsigned.get("prev_content", {})
            prev_membership = prev_content.get("membership", None)
            if prev_membership not in MEMBERSHIP_PRIORITY:
                prev_membership = "leave"

            # Always allow the user to see their own leave events, otherwise
            # they won't see the room disappear if they reject the invite
            #
            # (Note this doesn't work for out-of-band invite rejections, which don't
            # have prev_state populated. They are handled above in the outlier code.)
            if membership == "leave" and (
                prev_membership == "join" or prev_membership == "invite"
            ):
                return event

            new_priority = MEMBERSHIP_PRIORITY.index(membership)
            old_priority = MEMBERSHIP_PRIORITY.index(prev_membership)
            if old_priority < new_priority:
                membership = prev_membership

        # otherwise, get the user's membership at the time of the event.
        if membership is None:
            membership_event = state.get((EventTypes.Member, user_id), None)
            if membership_event:
                membership = membership_event.membership

        # if the user was a member of the room at the time of the event,
        # they can see it.
        if membership == Membership.JOIN:
            return event

        # otherwise, it depends on the room visibility.

        if visibility == HistoryVisibility.JOINED:
            # we weren't a member at the time of the event, so we can't
            # see this event.
            return None

        elif visibility == HistoryVisibility.INVITED:
            # user can also see the event if they were *invited* at the time
            # of the event.
            return event if membership == Membership.INVITE else None

        elif visibility == HistoryVisibility.SHARED and is_peeking:
            # if the visibility is shared, users cannot see the event unless
            # they have *subsequently* joined the room (or were members at the
            # time, of course)
            #
            # XXX: if the user has subsequently joined and then left again,
            # ideally we would share history up to the point they left. But
            # we don't know when they left. We just treat it as though they
            # never joined, and restrict access.
            return None

        # the visibility is either shared or world_readable, and the user was
        # not a member at the time. We allow it, provided the original sender
        # has not requested their data to be erased, in which case, we return
        # a redacted version.
        if erased_senders[event.sender]:
            return prune_event(event)

        return event

    # Check each event: gives an iterable of None or (a potentially modified)
    # EventBase.
    filtered_events = map(allowed, events)

    # Turn it into a list and remove None entries before returning.
    return [ev for ev in filtered_events if ev]


def get_effective_room_visibility_from_state(state: StateMap[EventBase]) -> str:
    """Get the actual history vis, from a state map including the history_visibility event

    Handles missing and invalid history visibility events.
    """
    visibility_event = state.get(_HISTORY_VIS_KEY, None)
    if not visibility_event:
        return HistoryVisibility.SHARED

    visibility = visibility_event.content.get(
        "history_visibility", HistoryVisibility.SHARED
    )
    if visibility not in VISIBILITY_PRIORITY:
        visibility = HistoryVisibility.SHARED
    return visibility


async def filter_events_for_server(
    storage: StorageControllers,
    server_name: str,
    events: List[EventBase],
    redact: bool = True,
    check_history_visibility_only: bool = False,
) -> List[EventBase]:
    """Filter a list of events based on whether given server is allowed to
    see them.

    Args:
        storage
        server_name
        events
        redact: Whether to return a redacted version of the event, or
            to filter them out entirely.
        check_history_visibility_only: Whether to only check the
            history visibility, rather than things like if the sender has been
            erased. This is used e.g. during pagination to decide whether to
            backfill or not.

    Returns
        The filtered events.
    """

    def is_sender_erased(event: EventBase, erased_senders: Dict[str, bool]) -> bool:
        if erased_senders and erased_senders[event.sender]:
            logger.info("Sender of %s has been erased, redacting", event.event_id)
            return True
        return False

    def check_event_is_visible(
        visibility: str, memberships: StateMap[EventBase]
    ) -> bool:
        if visibility not in (HistoryVisibility.INVITED, HistoryVisibility.JOINED):
            return True

        # We now loop through all membership events looking for
        # membership states for the requesting server to determine
        # if the server is either in the room or has been invited
        # into the room.
        for ev in memberships.values():
            assert get_domain_from_id(ev.state_key) == server_name

            memtype = ev.membership
            if memtype == Membership.JOIN:
                return True
            elif memtype == Membership.INVITE:
                if visibility == HistoryVisibility.INVITED:
                    return True

        # server has no users in the room: redact
        return False

    if not check_history_visibility_only:
        erased_senders = await storage.main.are_users_erased(e.sender for e in events)
    else:
        # We don't want to check whether users are erased, which is equivalent
        # to no users having been erased.
        erased_senders = {}

    # Let's check to see if all the events have a history visibility
    # of "shared" or "world_readable". If that's the case then we don't
    # need to check membership (as we know the server is in the room).
    event_to_history_vis = await _event_to_history_vis(storage, events)

    # for any with restricted vis, we also need the memberships
    event_to_memberships = await _event_to_memberships(
        storage,
        [
            e
            for e in events
            if event_to_history_vis[e.event_id]
            not in (HistoryVisibility.SHARED, HistoryVisibility.WORLD_READABLE)
        ],
        server_name,
    )

    to_return = []
    for e in events:
        erased = is_sender_erased(e, erased_senders)
        visible = check_event_is_visible(
            event_to_history_vis[e.event_id], event_to_memberships.get(e.event_id, {})
        )
        if visible and not erased:
            to_return.append(e)
        elif redact:
            to_return.append(prune_event(e))

    return to_return


async def _event_to_history_vis(
    storage: StorageControllers, events: Collection[EventBase]
) -> Dict[str, str]:
    """Get the history visibility at each of the given events

    Returns a map from event id to history_visibility setting
    """

    # outliers get special treatment here. We don't have the state at that point in the
    # room (and attempting to look it up will raise an exception), so all we can really
    # do is assume that the requesting server is allowed to see the event. That's
    # equivalent to there not being a history_visibility event, so we just exclude
    # any outliers from the query.
    event_to_state_ids = await storage.state.get_state_ids_for_events(
        frozenset(e.event_id for e in events if not e.internal_metadata.is_outlier()),
        state_filter=StateFilter.from_types(types=(_HISTORY_VIS_KEY,)),
    )

    visibility_ids = {
        vis_event_id
        for vis_event_id in (
            state_ids.get(_HISTORY_VIS_KEY) for state_ids in event_to_state_ids.values()
        )
        if vis_event_id
    }
    vis_events = await storage.main.get_events(visibility_ids)

    result: Dict[str, str] = {}
    for event in events:
        vis = HistoryVisibility.SHARED
        state_ids = event_to_state_ids.get(event.event_id)

        # if we didn't find any state for this event, it's an outlier, and we assume
        # it's open
        visibility_id = None
        if state_ids:
            visibility_id = state_ids.get(_HISTORY_VIS_KEY)

        if visibility_id:
            vis_event = vis_events[visibility_id]
            vis = vis_event.content.get("history_visibility", HistoryVisibility.SHARED)
            assert isinstance(vis, str)

        result[event.event_id] = vis
    return result


async def _event_to_memberships(
    storage: StorageControllers, events: Collection[EventBase], server_name: str
) -> Dict[str, StateMap[EventBase]]:
    """Get the remote membership list at each of the given events

    Returns a map from event id to state map, which will contain only membership events
    for the given server.
    """

    if not events:
        return {}

    # for each event, get the event_ids of the membership state at those events.
    #
    # TODO: this means that we request the entire membership list. If there  are only
    #   one or two users on this server, and the room is huge, this is very wasteful
    #   (it means more db work, and churns the *stateGroupMembersCache*).
    #   It might be that we could extend StateFilter to specify "give me keys matching
    #   *:<server_name>", to avoid this.

    event_to_state_ids = await storage.state.get_state_ids_for_events(
        frozenset(e.event_id for e in events),
        state_filter=StateFilter.from_types(types=((EventTypes.Member, None),)),
    )

    # We only want to pull out member events that correspond to the
    # server's domain.
    #
    # event_to_state_ids contains lots of duplicates, so it turns out to be
    # cheaper to build a complete event_id => (type, state_key) dict, and then
    # filter out the ones we don't want
    #
    event_id_to_state_key = {
        event_id: key
        for key_to_eid in event_to_state_ids.values()
        for key, event_id in key_to_eid.items()
    }

    def include(state_key: str) -> bool:
        # we avoid using get_domain_from_id here for efficiency.
        idx = state_key.find(":")
        if idx == -1:
            return False
        return state_key[idx + 1 :] == server_name

    event_map = await storage.main.get_events(
        [
            e_id
            for e_id, (_, state_key) in event_id_to_state_key.items()
            if include(state_key)
        ]
    )

    return {
        e_id: {
            key: event_map[inner_e_id]
            for key, inner_e_id in key_to_eid.items()
            if inner_e_id in event_map
        }
        for e_id, key_to_eid in event_to_state_ids.items()
    }
