# Copyright 2025 Google LLC
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
import asyncio
import logging
import time
from typing import Any, Optional
import uuid

import google.auth
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events import Event
from google.adk.sessions import _session_util
from google.adk.sessions import Session
from google.adk.sessions import State
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.base_session_service import GetSessionConfig
from google.adk.sessions.base_session_service import ListSessionsResponse
from google.cloud import firestore
from typing_extensions import override

logger = logging.getLogger("google_adk." + __name__)


class FirestoreKeys:
    """Helper to generate composite keys for Firestore-backed storage."""

    @staticmethod
    def session(app_name: str, user_id: str, session_id: str) -> str:
        return f"session:{app_name}:{user_id}:{session_id}"

    @staticmethod
    def app_state(app_name: str) -> str:
        return f"{State.APP_PREFIX}{app_name}"

    @staticmethod
    def user_state(app_name: str, user_id: str) -> str:
        return f"{State.USER_PREFIX}{app_name}:{user_id}"


class FirestoreSessionService(BaseSessionService):
    """Session service backed by Google Cloud Firestore."""

    def __init__(
        self,
        client: Optional[firestore.AsyncClient] = None,
        project: Optional[str] = None,
        database: Optional[str] = "(default)",
        session_collection: str = "sessions",
        state_collection: str = "session_state",
        default_app_name: Optional[str] = "adk-firestore-session-service",
    ) -> None:
        if client:
            self._client = client
        else:
            credentials, project_id = google.auth.default()
            self._client = firestore.AsyncClient(
                credentials=credentials,
                project=project or project_id,
                database=database,
            )
        self._sessions = self._client.collection(session_collection)
        self._kv = self._client.collection(state_collection)
        self._default_app_name = default_app_name

    @override
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        app_name = self._resolve_app_name(app_name)

        session_id = (session_id or "").strip() or str(uuid.uuid4())
        doc_id = FirestoreKeys.session(app_name, user_id, session_id)

        state_deltas = _session_util.extract_state_delta(state or {})
        await self._apply_state_delta(
            app_name, user_id, state_deltas.get("app"), state_deltas.get("user")
        )

        session_doc = {
            "app_name": app_name,
            "user_id": user_id,
            "id": session_id,
            "state": state_deltas.get("session", {}),
            "events": [],
            "last_update_time": time.time(),
        }

        doc_ref = self._sessions.document(doc_id)
        try:
            # use create() to ensure it fails if document already exists
            await doc_ref.create(session_doc)
        except Exception as exc:
            # Firestore raises an error if the document already exists when using create()
            # The specific error depends on the gRPC backend, but we check if it's already there.
            if "Already exists" in str(exc) or (await doc_ref.get()).exists:
                raise AlreadyExistsError(
                    f"Session with id {session_id} already exists."
                ) from exc
            raise

        session = self._doc_to_session(session_id, app_name, user_id, session_doc)
        return await self._merge_state(session)

    @override
    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        app_name = self._resolve_app_name(app_name)

        doc_id = FirestoreKeys.session(app_name, user_id, session_id)
        doc_snapshot = await self._sessions.document(doc_id).get()
        doc = doc_snapshot.to_dict()

        if not doc_snapshot.exists or doc is None:
            return None

        session = self._doc_to_session(session_id, app_name, user_id, doc)
        session = self._apply_event_filters(session, config)
        return await self._merge_state(session)

    @override
    async def list_sessions(
        self,
        *,
        app_name: str,
        user_id: Optional[str] = None,
    ) -> ListSessionsResponse:
        app_name = self._resolve_app_name(app_name)

        query = self._sessions.where(
            filter=firestore.FieldFilter("app_name", "==", app_name)
        )
        if user_id is not None:
            query = query.where(filter=firestore.FieldFilter("user_id", "==", user_id))

        # Note: we can't easily project out 'events' in Firestore if we want the rest of the doc
        # unless we list all other fields. For simplicity, we fetch the whole doc.
        docs = [d async for d in query.stream()]

        if len(docs) > 1000:
            logger.warning(
                "Loading a large number of sessions (%d) into memory for app '%s'.",
                len(docs),
                app_name,
            )

        sessions: list[Session] = []
        for doc_snapshot in docs:
            doc = doc_snapshot.to_dict()
            if doc is None:
                continue

            session = self._doc_to_session(
                str(doc.get("id", "")),
                str(doc.get("app_name", "")),
                str(doc.get("user_id", "")),
                doc,
            )
            merged = await self._merge_state(session)
            sessions.append(merged)

        return ListSessionsResponse(sessions=sessions)

    @override
    async def delete_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
    ) -> None:
        app_name = self._resolve_app_name(app_name)
        doc_id = FirestoreKeys.session(app_name, user_id, session_id)
        await self._sessions.document(doc_id).delete()

    @override
    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial:
            return event

        event = await super().append_event(session, event)
        session.last_update_time = event.timestamp

        state_delta = event.actions.state_delta if event.actions else None
        state_deltas = _session_util.extract_state_delta(state_delta or {})

        await self._apply_state_delta(
            session.app_name,
            session.user_id,
            state_deltas.get("app"),
            state_deltas.get("user"),
        )

        doc_id = FirestoreKeys.session(session.app_name, session.user_id, session.id)
        doc_ref = self._sessions.document(doc_id)

        updates: dict[str, Any] = {
            "events": firestore.ArrayUnion(
                [event.model_dump(mode="json", exclude_none=True)]
            ),
            "last_update_time": event.timestamp,
        }

        for key, value in state_deltas["session"].items():
            if value is not None:
                updates[f"state.{key}"] = value
            else:
                updates[f"state.{key}"] = firestore.DELETE_FIELD

        try:
            await doc_ref.update(updates)
        except Exception as exc:
            if "NOT_FOUND" in str(exc):
                logger.warning(
                    "Failed to append event: session %s/%s/%s not found in storage",
                    session.app_name,
                    session.user_id,
                    session.id,
                )
            else:
                raise

        return event

    async def _merge_state(self, session: Session) -> Session:
        app_state_ref = self._kv.document(FirestoreKeys.app_state(session.app_name))
        user_state_ref = self._kv.document(
            FirestoreKeys.user_state(session.app_name, session.user_id)
        )

        app_doc_snapshot, user_doc_snapshot = await asyncio.gather(
            app_state_ref.get(), user_state_ref.get()
        )

        merged_state = dict(session.state)
        if app_doc_snapshot.exists:
            app_doc = app_doc_snapshot.to_dict()
            if app_doc:
                app_state = app_doc.get("state", {})
                for key, value in app_state.items():
                    merged_state[State.APP_PREFIX + key] = value

        if user_doc_snapshot.exists:
            user_doc = user_doc_snapshot.to_dict()
            if user_doc:
                user_state = user_doc.get("state", {})
                for key, value in user_state.items():
                    merged_state[State.USER_PREFIX + key] = value

        return session.model_copy(update={"state": merged_state})

    async def _apply_state_delta(
        self,
        app_name: str,
        user_id: str,
        app_state_delta: Optional[dict[str, Any]],
        user_state_delta: Optional[dict[str, Any]],
    ) -> None:
        tasks = []
        if app_state_delta:
            tasks.append(
                self._update_state_document(
                    FirestoreKeys.app_state(app_name), app_state_delta
                )
            )
        if user_state_delta:
            tasks.append(
                self._update_state_document(
                    FirestoreKeys.user_state(app_name, user_id), user_state_delta
                )
            )
        if tasks:
            await asyncio.gather(*tasks)

    async def _update_state_document(
        self,
        doc_id: str,
        delta: dict[str, Any],
    ) -> None:
        doc_ref = self._kv.document(doc_id)

        updates: dict[str, Any] = {}
        for k, v in delta.items():
            if v is not None:
                updates[f"state.{k}"] = v
            else:
                updates[f"state.{k}"] = firestore.DELETE_FIELD

        if not updates:
            return

        try:
            # Use update with upsert logic.
            # Firestore's set(updates, merge=True) is better for upserting nested fields.
            await doc_ref.set(updates, merge=True)
        except Exception as exc:
            logger.error("Failed to update state document %s: %s", doc_id, exc)
            raise

    def _apply_event_filters(
        self, session: Session, config: Optional[GetSessionConfig]
    ) -> Session:
        if not config:
            return session
        events = session.events

        if config.after_timestamp is not None:
            events = [e for e in events if e.timestamp > config.after_timestamp]
        if config.num_recent_events is not None:
            events = events[-config.num_recent_events :]

        return session.model_copy(update={"events": events})

    def _doc_to_session(
        self, session_id: str, app_name: str, user_id: str, doc: dict[str, Any]
    ) -> Session:
        events = [Event.model_validate(e) for e in doc.get("events", [])]
        return Session(
            id=session_id,
            app_name=app_name,
            user_id=user_id,
            state=doc.get("state", {}),
            events=events,
            last_update_time=doc.get("last_update_time", 0.0),
        )

    def _resolve_app_name(self, app_name: Optional[str]) -> str:
        resolved = app_name or self._default_app_name
        if not resolved:
            raise ValueError(
                "app_name must be provided either in the call or in default_app_name."
            )
        return resolved
